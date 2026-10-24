from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import soundfile as sf

import generate_voiceover as generator
from audio_workflow import (
    approve_preview, binding, load_approved_plan, plan_chunks, validate_approval,
)
from voiceover_plan import DEFAULT_RULES, build_plan, file_sha256, validate_plan


class AudioWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.text = self.root / "script.txt"
        self.text.write_bytes(("第一步，打开今天的练习。\r\n\r\n"
                              "做完以后，先检查每一道题的思路。\r\n\r\n"
                              "最后，把需要复习的内容记下来。\r\n").encode("utf-8"))
        self.plan_file = self.root / "plan.json"
        self.plan = build_plan(self.text, DEFAULT_RULES, 90)
        self.plan["status"] = "approved"
        self.save_plan()
        self.profile = generator.load_profile(generator.EXAMPLE_PROFILE_PATH)
        self.settings = {"style": "short-video", "steps": 20}

    def save_plan(self):
        self.plan_file.write_text(json.dumps(self.plan, ensure_ascii=False), encoding="utf-8")

    def make_preview(self):
        audio = self.root / "preview.wav"
        sf.write(audio, np.zeros(1000), 16000)
        manifest = self.root / "preview.manifest.json"
        manifest.write_text(json.dumps({
            "delivery_kind": "preview", "output": str(audio),
            "source_file": str(self.text), "plan_file": str(self.plan_file),
            "audio_sha256": file_sha256(audio),
            **binding(self.text, self.plan_file, self.profile, self.settings),
        }), encoding="utf-8")
        return audio, manifest

    def test_plan_preserves_source_bytes_and_exact_unit_text(self):
        before = self.text.read_bytes()
        plan = load_approved_plan(self.text, self.plan_file)
        chunks = plan_chunks(plan, "final")
        self.assertEqual([c["text"] for c in chunks], [u["text"] for u in plan["units"]])
        self.assertEqual(before, self.text.read_bytes())

    def test_stale_or_unreviewed_plan_is_blocked(self):
        self.plan["status"] = "generated_needs_review"
        self.save_plan()
        with self.assertRaises(ValueError):
            load_approved_plan(self.text, self.plan_file)
        self.plan["status"] = "approved"
        self.save_plan()
        self.text.write_text("修改后的正文。", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            load_approved_plan(self.text, self.plan_file)

    def test_reordered_paragraphs_and_negative_pauses_are_blocked(self):
        self.plan["units"].reverse()
        for index, unit in enumerate(self.plan["units"], 1):
            unit["unit_id"] = f"u{index:03d}"
        self.save_plan()
        self.assertTrue(any("out of order" in e for e in validate_plan(self.text, self.plan_file)))
        self.plan["units"][0]["pause_after_ms"] = {"min": -10, "target": -5, "max": 0}
        self.save_plan()
        self.assertTrue(any("pause" in e for e in validate_plan(self.text, self.plan_file)))

    def test_preview_uses_whole_prefix_units(self):
        units = self.plan["units"] * 20
        chunks = plan_chunks({"units": units}, "preview", 15)
        self.assertLess(len(chunks), len(units))
        self.assertEqual([c["text"] for c in chunks], [u["text"] for u in units[:len(chunks)]])

    def test_recording_approval_requires_explicit_confirmation(self):
        _, manifest = self.make_preview()
        with self.assertRaises(ValueError):
            approve_preview(manifest, self.root / "approval.json", False)

    def test_approval_binds_actual_preview_bytes_and_settings(self):
        audio, manifest = self.make_preview()
        approval = self.root / "approval.json"
        approve_preview(manifest, approval, True)
        expected = binding(self.text, self.plan_file, self.profile, self.settings)
        validate_approval(approval, expected)
        changed = binding(self.text, self.plan_file, self.profile, {"steps": 30})
        with self.assertRaisesRegex(ValueError, "settings_sha256"):
            validate_approval(approval, changed)
        audio.write_bytes(b"replaced audio")
        with self.assertRaisesRegex(ValueError, "preview_audio"):
            validate_approval(approval, expected)

    def test_changed_script_or_voice_invalidates_approval(self):
        _, manifest = self.make_preview()
        approval = self.root / "approval.json"
        approve_preview(manifest, approval, True)
        changed_voice = dict(self.profile, reference_audio="different.wav")
        with self.assertRaisesRegex(ValueError, "profile_sha256"):
            validate_approval(approval, binding(self.text, self.plan_file, changed_voice, self.settings))
        self.text.write_text("改过的正文。", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "source_sha256"):
            validate_approval(approval, binding(self.text, self.plan_file, self.profile, self.settings))
        with self.assertRaisesRegex(ValueError, "stale"):
            approve_preview(manifest, approval, True)

    def test_profile_relative_paths_and_no_missing_profile_fallback(self):
        config = self.root / "own.json"
        config.write_text(json.dumps({"path_base": "profile", "reference_audio": "own.wav",
                                     "model_dir": "model"}), encoding="utf-8")
        self.assertEqual(generator.load_profile(config)["reference_audio"], str(self.root / "own.wav"))
        with self.assertRaises(FileNotFoundError):
            generator.load_profile(self.root / "missing.json")

    def test_formal_preview_rejects_missing_word_alignment(self):
        # Synthetic audio exercises assembly; this is not a voice-quality test.
        output = self.root / "preview.wav"
        seen_text = []
        rate = 16000
        reference = self.root / "reference.wav"
        sf.write(reference, np.zeros(rate), rate)
        self.profile["reference_audio"] = str(reference)
        self.profile["reference_sha256"] = file_sha256(reference)

        def synthetic(model, text, **kwargs):
            seen_text.append(text)
            return np.full(rate, 0.1, dtype=np.float32)

        model = types.SimpleNamespace(tts_model=types.SimpleNamespace(sample_rate=rate))
        fake_voxcpm = types.SimpleNamespace(VoxCPM=types.SimpleNamespace(from_pretrained=lambda *a, **k: model))
        fake_torch = types.SimpleNamespace(cuda=types.SimpleNamespace(is_available=lambda: True))
        argv = ["generate_voiceover.py", "--text-file", str(self.text), "--plan", str(self.plan_file),
                "--output", str(output), "--style", "short-video", "--keep-parts", "--quality-retries", "0"]
        with patch.object(sys, "argv", argv), patch.object(generator, "load_profile", return_value=self.profile), \
             patch.object(generator, "validate_environment", return_value={}), \
             patch.object(generator, "configure_model_paths"), \
             patch.object(generator, "load_or_build_prompt_cache", return_value=({}, "test")), \
             patch.object(generator, "synthesize_chunk", side_effect=synthetic), \
             patch.dict(sys.modules, {"torch": fake_torch, "voxcpm": fake_voxcpm}), \
             contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaisesRegex(ValueError, "No acceptable candidate path"):
                generator.main()
        self.assertFalse(output.exists())

    def test_full_generation_requires_approval_before_model_load(self):
        argv = ["generate_voiceover.py", "--text-file", str(self.text), "--plan", str(self.plan_file),
                "--output", str(self.root / "final.wav"), "--stage", "final"]
        with patch.object(sys, "argv", argv), patch.object(generator, "load_profile", return_value=self.profile), \
             patch.object(generator, "validate_environment") as check_environment:
            with self.assertRaisesRegex(ValueError, "--approval"):
                generator.main()
        check_environment.assert_not_called()

    def test_alignment_accepts_voxcpm_chunks_manifest(self):
        from qa_plan_alignment import run
        manifest = self.root / 'preview.manifest.json'
        manifest.write_text(json.dumps({'chunks': [{'unit_id': 'u001'}]}), encoding='utf-8')
        alignment = self.root / 'alignment'
        alignment.mkdir()
        result = run(self.plan_file, alignment, 0.24, manifest)
        self.assertEqual([u['unit_id'] for u in result['units']], ['u001'])
        self.assertEqual(result['status'], 'needs_review')


if __name__ == "__main__":
    unittest.main()
