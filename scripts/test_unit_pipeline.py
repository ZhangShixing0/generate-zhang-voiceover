from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import numpy as np
import soundfile as sf

from audio_delivery import assemble_delivery
from audio_quality import normalized
from recording_import import split_whole_recording
from unit_store import approve_unit, load_cached, render_key, save_cached, valid_approval
from voiceover_plan import DEFAULT_RULES, build_plan


class UnitPipelineTests(unittest.TestCase):
    def setUp(self):
        tmp_root = Path(__file__).parents[2] / "tmp"
        tmp_root.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=tmp_root))
        self.text = self.root / "script.txt"
        self.text.write_text("第一句先说清楚。\n\n第二句继续说明。", encoding="utf-8")
        self.plan = build_plan(self.text, DEFAULT_RULES, 90)

    def test_revision_reuses_old_ids_and_hashes_only_changed_unit(self):
        changed = self.root / "changed.txt"
        changed.write_text("第一句先说清楚。\n\n第二句改成新的说明。", encoding="utf-8")
        revised = build_plan(changed, DEFAULT_RULES, 90, self.plan)
        self.assertEqual(revised["units"][0]["unit_id"], self.plan["units"][0]["unit_id"])
        self.assertEqual(revised["units"][1]["unit_id"], self.plan["units"][1]["unit_id"])
        self.assertNotEqual(revised["units"][1]["text_sha256"], self.plan["units"][1]["text_sha256"])

    def test_corrupt_cache_is_ignored(self):
        cache = self.root / "cache"
        wav = self.root / "u001.wav"
        sf.write(wav, np.zeros(1000, dtype=np.float32), 16000)
        key = render_key(self.plan["units"][0], profile={"p": 1}, settings={}, engine="test", code_version="1")
        output, metadata = save_cached(cache, "u001", key, wav, {"quality": {"status": "pass"}})
        self.assertIsNotNone(load_cached(cache, "u001", key))
        output.write_bytes(b"changed")
        self.assertIsNone(load_cached(cache, "u001", key))

    def test_unit_approval_binds_text_key_and_audio_bytes(self):
        cache = self.root / "cache"
        wav = self.root / "u001.wav"
        sf.write(wav, np.zeros(1000, dtype=np.float32), 16000)
        unit = self.plan["units"][0]
        key = render_key(unit, profile={"p": 1}, settings={}, engine="test", code_version="1")
        save_cached(cache, unit["unit_id"], key, wav, {"quality": {"status": "pass"}})
        quality = {"status": "pass", "words": [], "phrases": [], "speech_intervals": []}
        approved = approve_unit(cache, unit=unit, render_key_value=key, audio_path=wav, quality=quality, confirmed_by_user=True)
        self.assertEqual(valid_approval(cache, unit, key)["decision"], "approved")
        unit["text_sha256"] = "changed"
        self.assertIsNone(valid_approval(cache, unit, key))

    def test_delivery_exports_parts_timeline_srt_and_safe_intervals(self):
        unit = self.plan["units"][0]
        wav = self.root / "candidate.wav"
        audio = np.zeros(16000, dtype=np.float32)
        audio[3000:12000] = .1
        sf.write(wav, audio, 16000)
        quality = {"status": "pass", "words": [{"word": "第一句", "start": .2, "end": .7, "conf": .99}],
                   "phrases": [{"text": unit["text"], "start": .2, "end": .7, "char_count": 6}],
                   "speech_intervals": [[.2, .7]]}
        out = self.root / "final.wav"
        result = assemble_delivery(plan=self.plan, unit_records=[{"unit": unit, "audio": str(wav), "quality": quality}], output=out, sample_rate=16000, stage="draft")
        self.assertTrue(Path(result["timeline"]).is_file())
        self.assertTrue(Path(result["srt"]).is_file())
        timeline = json.loads(Path(result["timeline"]).read_text(encoding="utf-8"))
        self.assertEqual(timeline["units"][0]["unit_id"], unit["unit_id"])
        self.assertTrue(timeline["sfx_safe_intervals"])
        self.assertTrue((Path(result["parts_dir"]) / f"{unit['unit_id']}.wav").is_file())

    def test_whole_recording_cuts_at_alignment_boundaries(self):
        audio = np.arange(16000, dtype=np.float32) / 16000
        plan = {"units": [{"unit_id": "u001", "text": "甲乙", "pause_after_ms": {"target": 0}}, {"unit_id": "u002", "text": "丙丁", "pause_after_ms": {"target": 0}}]}
        words = [
            {"word": "甲", "start": .1, "end": .2, "conf": .99}, {"word": "乙", "start": .21, "end": .3, "conf": .99},
            {"word": "丙", "start": .5, "end": .6, "conf": .99}, {"word": "丁", "start": .61, "end": .7, "conf": .99},
        ]
        pieces = split_whole_recording(audio, 16000, plan, words, normalized)
        self.assertEqual(len(pieces), 2)
        with self.assertRaises(ValueError):
            split_whole_recording(audio, 16000, plan, words[:-1], normalized)


if __name__ == "__main__":
    unittest.main()

