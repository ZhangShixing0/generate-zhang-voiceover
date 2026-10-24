"""Stage runner for draft, measured VoxCPM candidates, cache reuse and delivery."""
from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

import numpy as np

from audio_delivery import assemble_delivery
from audio_quality import CandidateFailure, QualityPolicy, assess_audio, choose_candidates, transition
from audio_workflow import load_approved_plan
from draft_tts import synthesize_draft
from recording_import import write_recording_script, split_whole_recording
from unit_store import load_cached, render_key, save_cached, valid_approval


def _unit_list(plan: dict, stage: str, preview_seconds: float) -> list[dict]:
    if stage in {"final", "draft", "recording-script", "recording"}:
        return list(plan["units"])
    result, estimate = [], 0.0
    for unit in plan["units"]:
        result.append(unit)
        estimate += sum(ch.isalnum() for ch in unit["text"]) / 4.5 + unit["pause_after_ms"]["target"] / 1000
        if estimate >= preview_seconds:
            break
    return result


def _load_alignment(model, wav_path: Path, unit_id: str) -> list[dict]:
    if model is None:
        return []
    result = model(wav_path)
    return result.get("words", [])


def _quality(unit: dict, wav_path: Path, words: list[dict], policy: QualityPolicy) -> dict:
    import soundfile as sf
    audio, rate = sf.read(wav_path, dtype="float32", always_2d=False)
    return assess_audio(unit["text"], audio, rate, words, policy)


def _write_review(path: Path, units: list[dict], diagnostics: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"status": "needs_review", "units": diagnostics}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args, gv) -> int:
    import soundfile as sf
    stage = args.stage
    source_file = Path(args.text_file).resolve()
    plan_file = Path(args.plan).resolve()
    plan = load_approved_plan(source_file, plan_file, Path(args.rules).resolve())
    units = _unit_list(plan, stage, args.preview_seconds)
    if args.inspect_chunks:
        print(json.dumps({"stage": stage, "units": units}, ensure_ascii=False, indent=2))
        return 0
    if stage == "recording-script":
        write_recording_script(plan, Path(args.recording_script or (Path(args.output).with_suffix(".txt"))))
        return 0
    if stage == "recording":
        if not args.recording_audio:
            raise ValueError("--recording-audio is required for recording stage")
        audio, rate = sf.read(Path(args.recording_audio), dtype="float32", always_2d=False)
        if audio.ndim != 1:
            audio = np.mean(audio, axis=1).astype(np.float32)
        if args.recording_alignment:
            payload = json.loads(Path(args.recording_alignment).read_text(encoding="utf-8"))
            words = payload.get("words", payload if isinstance(payload, list) else [])
        else:
            if not args.vosk_model_dir:
                raise ValueError("--vosk-model-dir is required when --recording-alignment is omitted")
            from align_units_vosk import VoskAligner
            with tempfile.TemporaryDirectory(prefix="recording-align-", dir=output.parent) as align_temp:
                normalized_audio = Path(align_temp) / "recording.wav"
                sf.write(normalized_audio, audio, rate, subtype="PCM_16")
                words = VoskAligner(Path(args.vosk_model_dir).resolve())(normalized_audio).get("words", [])
        from audio_quality import normalized
        parts = split_whole_recording(audio, rate, plan, words, normalized)
        root = output.parent / f"{output.stem}-parts"
        root.mkdir(parents=True, exist_ok=True)
        records = []
        for unit, part in zip(units, parts):
            path = root / f"{unit['unit_id']}.wav"
            sf.write(path, part, rate, subtype="PCM_16")
            records.append({"unit": unit, "audio": str(path), "quality": {"words": [], "phrases": [], "speech_intervals": [[0.0, len(part)/rate]], "status": "needs_review", "reasons": ["recording_import_requires_unit_review"]}, "render_key": "recording"})
        delivery = assemble_delivery(plan=plan, unit_records=records, output=output, sample_rate=rate, stage=stage, source_file=source_file, plan_file=plan_file)
        print(json.dumps(delivery, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        raise ValueError("--output is required")
    output = Path(args.output).resolve()
    if output.exists():
        raise FileExistsError("Choose a new output name; existing audio and manifests are preserved.")
    cache_root = Path(args.cache_dir).resolve() if args.cache_dir else output.parent / ".voiceover-cache"
    cache_root.mkdir(parents=True, exist_ok=True)
    profile = gv.load_profile(args.profile) if stage != "draft" else {"engine": "edge-tts-draft"}
    generation = profile.get("generation", {})
    settings = {
        "style": args.style, "steps": args.steps or int(generation.get("inference_timesteps", 20)),
        "cfg_value": args.cfg_value or float(generation.get("cfg_value", 2.0)),
        "candidate_count": 3, "quality_policy": vars(QualityPolicy()), "stage": "formal" if stage in {"preview", "final"} else stage,
    }
    if stage == "final" and args.approval and not Path(args.approval).is_file():
        raise ValueError("The supplied whole-preview approval file does not exist")
    if stage == "final" and not args.approval:
        raise ValueError("--approval is required for final generation.")
    model = None
    prompt_cache = None
    sample_rate = 24000
    if stage == "preview":
        if profile.get("engine") != "VoxCPM2":
            raise ValueError("Formal stages require a VoxCPM2 profile")
        gv.validate_environment(profile, require_ffmpeg=False)
        gv.configure_model_paths(Path(profile["model_dir"]).parents[1])
        import torch
        from voxcpm import VoxCPM
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available in the VoxCPM2 environment.")
        model = VoxCPM.from_pretrained(str(Path(profile["model_dir"])), device="cuda", optimize=False, load_denoiser=False)
        sample_rate = model.tts_model.sample_rate
        reference = gv.resolve_reference(profile)
        cache_path = Path(args.prompt_cache).resolve() if args.prompt_cache else gv.resolve_prompt_cache(profile) or gv.default_prompt_cache_path(reference, output)
        prompt_cache, _ = gv.load_or_build_prompt_cache(model, reference, cache_path)
    aligner = None
    model_dir = args.vosk_model_dir or profile.get("vosk_model_dir")
    if model_dir:
        from align_units_vosk import VoskAligner
        aligner = VoskAligner(Path(model_dir).resolve())
    diagnostics, chosen_records = [], []
    rng_candidates: list[list[dict]] = []
    fresh_units: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="voiceover-candidates-", dir=output.parent) as temp_name:
        temp = Path(temp_name)
        for unit in units:
            key = render_key(unit, profile=profile, settings=settings, engine=profile.get("engine", stage), code_version="unit-pipeline-1")
            cached = load_cached(cache_root, unit["unit_id"], key)
            if stage == "final":
                if not cached:
                    raise ValueError(f"Unit {unit['unit_id']} has no rendered candidate; run preview first")
                approval = valid_approval(cache_root, unit, key)
                if not approval:
                    raise ValueError(f"Unit {unit['unit_id']} has changed or lacks explicit per-unit approval")
                audio_path, meta = cached
                chosen_records.append({"unit": unit, "audio": str(audio_path), "quality": meta.get("quality", {}), "render_key": key})
                continue
            if cached and cached[1].get("quality", {}).get("status") == "pass":
                audio_path, meta = cached
                chosen_records.append({"unit": unit, "audio": str(audio_path), "quality": meta["quality"], "render_key": key})
                continue
            candidate_group = []
            count = 1 if stage == "draft" else 3
            for candidate_index in range(count):
                path = temp / f"{unit['unit_id']}-candidate-{candidate_index + 1}.wav"
                if stage == "draft":
                    words = synthesize_draft(unit["text"], path, voice=args.draft_voice)
                else:
                    audio = gv.synthesize_chunk(model=model, text=unit["text"], prompt_cache=prompt_cache, profile=profile,
                                                cfg_value=settings["cfg_value"], steps=settings["steps"], max_audio_tokens=gv.resolve_max_audio_tokens(profile, args.style))
                    sf.write(path, gv.edge_fade(audio, sample_rate), sample_rate, subtype="PCM_16")
                    words = _load_alignment(aligner, path, unit["unit_id"])
                quality = _quality(unit, path, words, QualityPolicy())
                candidate_group.append({"audio": str(path), "quality": quality, "candidate": candidate_index + 1})
            diagnostics.append({"unit_id": unit["unit_id"], "candidates": [{"candidate": c["candidate"], "status": c["quality"]["status"], "reasons": c["quality"]["reasons"]} for c in candidate_group]})
            if stage == "draft":
                chosen = candidate_group[0]
            else:
                rng_candidates.append(candidate_group)
                fresh_units.append(unit)
                chosen = None
            if chosen:
                saved, _ = save_cached(cache_root, unit["unit_id"], key, Path(chosen["audio"]), {"quality": chosen["quality"], "engine": stage, "text": unit["text"]})
                chosen_records.append({"unit": unit, "audio": str(saved), "quality": chosen["quality"], "render_key": key})
        if rng_candidates:
            try:
                selected = choose_candidates(rng_candidates, QualityPolicy())
            except CandidateFailure:
                review = output.with_name(output.stem + ".review.json")
                _write_review(review, units, diagnostics)
                raise
            offset = 0
            for group, unit in zip(rng_candidates, fresh_units):
                chosen = selected[offset]
                key = render_key(unit, profile=profile, settings=settings, engine=profile.get("engine", stage), code_version="unit-pipeline-1")
                saved, _ = save_cached(cache_root, unit["unit_id"], key, Path(chosen["audio"]), {"quality": chosen["quality"], "engine": stage, "text": unit["text"]})
                chosen_records.append({"unit": unit, "audio": str(saved), "quality": chosen["quality"], "render_key": key})
                offset += 1
    chosen_records.sort(key=lambda r: units.index(r["unit"]))
    for left, right in zip(chosen_records, chosen_records[1:]):
        if left["quality"].get("phrases") and right["quality"].get("phrases"):
            join = transition(left["quality"], right["quality"], QualityPolicy())
            if join["status"] != "pass":
                review = output.with_name(output.stem + ".review.json")
                _write_review(review, units, diagnostics + [{"join": join, "left": left["unit"]["unit_id"], "right": right["unit"]["unit_id"]}])
                raise CandidateFailure(f"Adjacent unit join failed between {left['unit']['unit_id']} and {right['unit']['unit_id']}")
    if stage == "draft":
        # Draft timings and IDs are deliberately the same schema, but duration is measured from draft bytes.
        pass
    if stage == "final" and len(chosen_records) != len(units):
        raise ValueError("Final output is missing one or more approved units")
    delivery = assemble_delivery(plan=plan, unit_records=chosen_records, output=output, sample_rate=sample_rate,
                                 stage=stage, source_file=source_file, plan_file=plan_file)
    manifest = {"manifest_version": "3.0", "delivery_kind": stage, "source_file": str(source_file), "plan_file": str(plan_file),
                "source_sha256": gv.file_sha256(source_file).lower(), "plan_sha256": gv.file_sha256(plan_file).lower(),
                "profile_sha256": __import__('hashlib').sha256(json.dumps(profile, ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                "settings": settings, "cache_dir": str(cache_root), "units": [{"unit_id": r["unit"]["unit_id"], "render_key": r["render_key"], "audio": r["audio"], "quality": r["quality"]} for r in chosen_records], **delivery}
    manifest_path = Path(args.manifest).resolve() if args.manifest else output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"stage": stage, "manifest": str(manifest_path), **delivery}, ensure_ascii=False, indent=2))
    return 0

