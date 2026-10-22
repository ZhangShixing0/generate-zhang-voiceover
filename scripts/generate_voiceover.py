from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from statistics import median

import numpy as np


SKILL_ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = SKILL_ROOT / "references" / "voice-profile.json"
LOCAL_PROFILE_PATH = SKILL_ROOT / "references" / "voice-profile.local.json"
EXAMPLE_PROFILE_PATH = SKILL_ROOT / "references" / "voice-profile.example.json"
STYLE_PAUSES = {
    "natural": 0.35,
    "course": 0.45,
    "short-video": 0.24,
}
DEFAULT_CHUNKING = {
    "natural": {"min_chars": 18, "max_chars": 60},
    "course": {"min_chars": 20, "max_chars": 70},
    "short-video": {"min_chars": 18, "max_chars": 45},
}
DEFAULT_MAX_AUDIO_TOKENS = {
    "natural": 100,
    "course": 120,
    "short-video": 90,
}


def load_profile(path: str | Path | None = None) -> dict:
    configured = path or os.environ.get("ZHANG_VOICE_PROFILE")
    source = Path(configured).expanduser().resolve() if configured else (
        LOCAL_PROFILE_PATH if LOCAL_PROFILE_PATH.is_file() else PROFILE_PATH
    )
    if not source.is_file():
        raise FileNotFoundError(
            "Configure your own voice first: copy references/voice-profile.example.json "
            "to references/voice-profile.local.json, or pass --profile."
        )
    profile = json.loads(source.read_text(encoding="utf-8"))
    # Existing accepted profiles resolve assets from the skill root. New profiles
    # can choose paths relative to their JSON by declaring path_base: profile.
    base = source.parent if profile.get("path_base") == "profile" else SKILL_ROOT
    for key in ("model_dir", "reference_audio", "prompt_cache_asset"):
        if profile.get(key):
            profile[key] = str((base / Path(profile[key]).expanduser()).resolve())
    return profile


def resolve_reference(profile: dict) -> Path:
    return (SKILL_ROOT / profile["reference_audio"]).resolve()


def resolve_prompt_cache(profile: dict) -> Path | None:
    configured = profile.get("prompt_cache_asset")
    return (SKILL_ROOT / configured).resolve() if configured else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def configure_model_paths(model_root: Path) -> None:
    paths = {
        "HF_HOME": model_root / "cache" / "huggingface",
        "HUGGINGFACE_HUB_CACHE": model_root / "cache" / "huggingface" / "hub",
        "MODELSCOPE_CACHE": model_root / "cache" / "modelscope",
        "TORCH_HOME": model_root / "cache" / "torch",
        "TEMP": model_root / "temp",
        "TMP": model_root / "temp",
    }
    for key, value in paths.items():
        value.mkdir(parents=True, exist_ok=True)
        os.environ[key] = str(value)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def spoken_char_count(text: str) -> int:
    return len(re.findall(r"[\u3400-\u9fffA-Za-z0-9]", text))


def split_oversized(piece: str, max_chars: int) -> list[str]:
    if len(piece) <= max_chars:
        return [piece]

    clauses = [part for part in re.split(r"(?<=[，、：,:])", piece) if part]
    result: list[str] = []
    current = ""
    for clause in clauses:
        if len(clause) > max_chars:
            if current:
                result.append(current)
                current = ""
            result.extend(clause[i : i + max_chars] for i in range(0, len(clause), max_chars))
        elif current and len(current) + len(clause) > max_chars:
            result.append(current)
            current = clause
        else:
            current += clause
    if current:
        result.append(current)
    return result


def chunk_paragraph(paragraph: str, min_chars: int, max_chars: int) -> list[str]:
    paragraph = re.sub(r"\n+", "", paragraph).strip()
    if not paragraph:
        return []

    raw_sentences = [
        part.strip()
        for part in re.split(r"(?<=[。！？!?；;])", paragraph)
        if part.strip()
    ]
    pieces: list[str] = []
    for sentence in raw_sentences:
        pieces.extend(split_oversized(sentence, max_chars))

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if not current:
            current = piece
        elif len(current) < min_chars and len(current) + len(piece) <= max_chars:
            current += piece
        else:
            chunks.append(current)
            current = piece
    if current:
        chunks.append(current)

    if len(chunks) > 1 and len(chunks[-1]) < min_chars:
        tail = chunks[-1]
        if len(chunks[-2]) + len(tail) <= max_chars:
            chunks[-2] += tail
            chunks.pop()
    return chunks


def build_chunk_manifest(text: str, min_chars: int, max_chars: int) -> list[dict]:
    text = clean_text(text)
    if not text:
        raise ValueError("The input script is empty.")

    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    manifest: list[dict] = []
    for paragraph_index, paragraph in enumerate(paragraphs, start=1):
        for chunk in chunk_paragraph(paragraph, min_chars, max_chars):
            manifest.append(
                {
                    "index": len(manifest) + 1,
                    "paragraph_index": paragraph_index,
                    "text": chunk,
                    "char_count": spoken_char_count(chunk),
                }
            )
    return manifest


def chunk_text(text: str, min_chars: int, max_chars: int) -> list[str]:
    return [
        chunk["text"]
        for chunk in build_chunk_manifest(text, min_chars=min_chars, max_chars=max_chars)
    ]


def resolve_chunk_limits(profile: dict, style: str) -> tuple[int, int]:
    generation = profile["generation"]
    configured = generation.get("chunking", {}).get(style, {})
    defaults = DEFAULT_CHUNKING[style]
    min_chars = int(
        configured.get(
            "min_chars",
            generation.get("chunk_min_chars", defaults["min_chars"]),
        )
    )
    max_chars = int(
        configured.get(
            "max_chars",
            generation.get("chunk_max_chars", defaults["max_chars"]),
        )
    )
    if min_chars < 1 or max_chars < min_chars:
        raise ValueError(
            f"Invalid chunk limits for {style}: min={min_chars}, max={max_chars}"
        )
    return min_chars, max_chars


def apply_chunk_limit_overrides(
    min_chars: int,
    max_chars: int,
    min_override: int | None,
    max_override: int | None,
) -> tuple[int, int]:
    resolved_min = min_override if min_override is not None else min_chars
    resolved_max = max_override if max_override is not None else max_chars
    if resolved_min < 1 or resolved_max < resolved_min:
        raise ValueError(
            f"Invalid chunk overrides: min={resolved_min}, max={resolved_max}"
        )
    return resolved_min, resolved_max


def analyze_pacing(records: list[dict], profile: dict) -> dict:
    pacing = profile["generation"].get("pacing", {})
    eligible = [
        record["chars_per_second"]
        for record in records
        if record["char_count"] >= int(pacing.get("min_char_count", 12))
        and record["duration_seconds"] > 0
    ]
    baseline = median(eligible) if eligible else None
    fast_ratio = float(pacing.get("fast_ratio", 1.25))
    slow_ratio = float(pacing.get("slow_ratio", 0.70))
    absolute_fast = float(pacing.get("absolute_fast_cps", 6.0))

    for record in records:
        warnings: list[str] = []
        cps = record["chars_per_second"]
        if record["duration_seconds"] <= 0:
            warnings.append("empty-audio")
        elif record["char_count"] >= int(pacing.get("min_char_count", 12)):
            if cps > absolute_fast or (baseline and cps > baseline * fast_ratio):
                warnings.append("pacing-fast")
            if baseline and cps < baseline * slow_ratio:
                warnings.append("pacing-slow")
        record["warnings"] = warnings

    return {
        "baseline_chars_per_second": round(baseline, 3) if baseline else None,
        "warning_count": sum(bool(record["warnings"]) for record in records),
        "status": "needs-review"
        if any(record["warnings"] for record in records)
        else "pass",
    }


def edge_fade(audio: np.ndarray, sample_rate: int) -> np.ndarray:
    fade_samples = min(round(sample_rate * 0.005), len(audio) // 2)
    if fade_samples <= 0:
        return audio
    result = audio.copy()
    result[:fade_samples] *= np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    result[-fade_samples:] *= np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
    return result


def rms_dbfs(audio: np.ndarray) -> float:
    rms = float(np.sqrt(np.mean(np.square(audio), dtype=np.float64)))
    return 20.0 * math.log10(max(rms, 1e-9))


def resolve_max_audio_tokens(profile: dict, style: str) -> int:
    configured = profile["generation"].get("max_audio_tokens", {})
    value = int(configured.get(style, DEFAULT_MAX_AUDIO_TOKENS[style]))
    if value < 1:
        raise ValueError(f"Invalid max_audio_tokens for {style}: {value}")
    return value


def default_prompt_cache_path(reference: Path, output: Path) -> Path:
    reference_hash = file_sha256(reference)[:12].lower()
    return output.parent / f".{reference.stem}-{reference_hash}-cache.pt"


def load_or_build_prompt_cache(model, reference: Path, cache_path: Path) -> tuple[dict, str]:
    import torch

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.exists():
        return (
            torch.load(cache_path, map_location="cpu", weights_only=True),
            "loaded",
        )

    prompt_cache = model.tts_model.build_prompt_cache(
        reference_wav_path=str(reference),
    )
    torch.save(prompt_cache, cache_path)
    return prompt_cache, "built"


def locate_ffmpeg(profile: dict) -> Path | None:
    executable = shutil.which("ffmpeg")
    if executable:
        return Path(executable)
    for candidate in profile.get("ffmpeg_candidates", []):
        path = Path(candidate)
        if path.exists():
            return path
    return None


def validate_environment(profile: dict, require_ffmpeg: bool) -> dict:
    model_dir = Path(profile["model_dir"])
    reference = resolve_reference(profile)
    prompt_cache = resolve_prompt_cache(profile)
    problems: list[str] = []

    if not model_dir.exists():
        problems.append(f"Model directory not found: {model_dir}")
    if not reference.exists():
        problems.append(f"Voice reference not found: {reference}")
    elif file_sha256(reference) != str(profile["reference_sha256"]).upper():
        problems.append("Voice reference checksum does not match the accepted profile.")

    if prompt_cache is not None:
        if not prompt_cache.exists():
            problems.append(f"Prompt cache not found: {prompt_cache}")
        elif file_sha256(prompt_cache) != str(profile["prompt_cache_sha256"]).upper():
            problems.append("Prompt cache checksum does not match the accepted profile.")

    ffmpeg = locate_ffmpeg(profile)
    if require_ffmpeg and ffmpeg is None:
        problems.append("FFmpeg is required for speed adjustment but was not found.")

    result = {
        "ok": not problems,
        "model_dir": str(model_dir),
        "reference_audio": str(reference),
        "prompt_cache": str(prompt_cache) if prompt_cache else None,
        "ffmpeg": str(ffmpeg) if ffmpeg else None,
        "problems": problems,
    }
    if problems:
        raise RuntimeError(json.dumps(result, ensure_ascii=False))
    return result


def read_input(args: argparse.Namespace) -> str:
    if args.text is not None:
        return args.text
    return Path(args.text_file).read_text(encoding="utf-8-sig")


def run_speed_adjustment(ffmpeg: Path, source: Path, output: Path, speed: float) -> None:
    subprocess.run(
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-filter:a",
            f"atempo={speed:.4f}",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        check=True,
    )


def synthesize_chunk(
    model,
    text: str,
    prompt_cache: dict,
    profile: dict,
    cfg_value: float,
    steps: int,
    max_audio_tokens: int,
) -> np.ndarray:
    generation = profile["generation"]
    normalized_text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    if bool(generation["normalize"]):
        if model.text_normalizer is None:
            from voxcpm.utils.text_normalize import TextNormalizer

            model.text_normalizer = TextNormalizer()
        normalized_text = model.text_normalizer.normalize(normalized_text)

    wav, _, _ = model.tts_model.generate_with_prompt_cache(
        target_text=normalized_text,
        prompt_cache=prompt_cache,
        cfg_value=cfg_value,
        inference_timesteps=steps,
        min_len=2,
        max_len=max_audio_tokens,
        retry_badcase=False,
        retry_badcase_max_times=1,
    )
    return wav.squeeze(0).detach().cpu().numpy().astype(np.float32, copy=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a local VoxCPM2 voiceover.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--text-file")
    parser.add_argument("--output")
    parser.add_argument("--profile")
    parser.add_argument("--plan")
    parser.add_argument("--rules", default=str(SKILL_ROOT / "references" / "protected-phrases.json"))
    parser.add_argument("--stage", choices=("preview", "final"), default="preview")
    parser.add_argument("--preview-seconds", type=float, default=28)
    parser.add_argument("--approval")
    parser.add_argument("--style", choices=sorted(STYLE_PAUSES), default="natural")
    parser.add_argument("--speed", type=float, default=1.0)
    parser.add_argument("--pause", type=float)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--cfg-value", type=float)
    parser.add_argument("--quality-retries", type=int)
    parser.add_argument("--chunk-min-chars", type=int)
    parser.add_argument("--chunk-max-chars", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--prompt-cache")
    parser.add_argument("--inspect-chunks", action="store_true")
    parser.add_argument("--keep-parts", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    args = parser.parse_args()

    if not args.validate_only and args.text is None and args.text_file is None:
        parser.error("one of --text or --text-file is required")
    if not args.validate_only and not args.inspect_chunks and not args.output:
        parser.error("--output is required")
    if not 0.8 <= args.speed <= 1.2:
        parser.error("--speed must be between 0.8 and 1.2")
    if args.quality_retries is not None and args.quality_retries < 0:
        parser.error("--quality-retries must be zero or greater")
    if args.chunk_min_chars is not None and args.chunk_min_chars < 1:
        parser.error("--chunk-min-chars must be greater than zero")
    if args.chunk_max_chars is not None and args.chunk_max_chars < 1:
        parser.error("--chunk-max-chars must be greater than zero")
    if not args.validate_only and not args.inspect_chunks and not args.plan:
        parser.error("--plan is required for synthesis; build and review it with voiceover_plan.py")
    if args.plan and not args.text_file:
        parser.error("--plan requires --text-file")
    if args.plan and (args.speed != 1.0 or args.pause is not None or args.prompt_cache
                      or args.chunk_min_chars is not None or args.chunk_max_chars is not None):
        parser.error("A reading plan controls units and pauses; speed/cache/chunk overrides are not allowed")
    if not 15 <= args.preview_seconds <= 35:
        parser.error("--preview-seconds must be between 15 and 35")
    return args


def main() -> int:
    args = parse_args()
    profile = load_profile(args.profile)

    if args.validate_only:
        environment = validate_environment(profile, require_ffmpeg=False)
        print(json.dumps(environment, ensure_ascii=False, indent=2))
        return 0

    generation = profile["generation"]
    source_text = read_input(args)
    min_chars, max_chars = resolve_chunk_limits(profile, args.style)
    min_chars, max_chars = apply_chunk_limit_overrides(
        min_chars,
        max_chars,
        args.chunk_min_chars,
        args.chunk_max_chars,
    )
    from audio_workflow import binding, load_approved_plan, plan_chunks, validate_approval
    delivery_binding = {}
    plan = None
    settings = {
        "style": args.style,
        "steps": args.steps if args.steps is not None else int(generation["inference_timesteps"]),
        "cfg_value": args.cfg_value if args.cfg_value is not None else float(generation["cfg_value"]),
        "quality_retries": args.quality_retries if args.quality_retries is not None else int(generation.get("pacing", {}).get("quality_retries", 1)),
        "max_audio_tokens": resolve_max_audio_tokens(profile, args.style),
        "engine_script_sha256": file_sha256(Path(__file__)).lower(),
    }
    if args.plan:
        plan = load_approved_plan(Path(args.text_file), Path(args.plan), Path(args.rules))
        if generation.get("normalize"):
            raise ValueError("Model-side rewriting is disabled for approved scripts; set normalize to false.")
        if str(profile.get("status", "")).startswith("rejected"):
            raise ValueError("The selected voice profile has been rejected.")
        if profile.get("engine") != "VoxCPM2":
            raise ValueError("This adapter requires a VoxCPM2 voice profile.")
        chunks = plan_chunks(plan, args.stage, args.preview_seconds)
        delivery_binding = binding(Path(args.text_file), Path(args.plan), profile, settings)
        if args.stage == "final" and not args.inspect_chunks:
            if not args.approval:
                raise ValueError("--approval is required for final generation.")
            validate_approval(Path(args.approval), delivery_binding)
    else:
        # Compatibility inspection only. Synthesis always requires a reviewed plan.
        chunks = build_chunk_manifest(source_text, min_chars=min_chars, max_chars=max_chars)
    if args.inspect_chunks:
        inspection = {
            "style": args.style,
            "min_chars": min_chars,
            "max_chars": max_chars,
            "source_sha256": hashlib.sha256(
                source_text.encode("utf-8")
            ).hexdigest().upper(),
            "chunks": chunks,
        }
        print(json.dumps(inspection, ensure_ascii=False, indent=2))
        return 0

    environment = validate_environment(profile, require_ffmpeg=args.speed != 1.0)
    model_dir = Path(profile["model_dir"])
    model_root = model_dir.parents[1]
    reference = resolve_reference(profile)
    output = Path(args.output).resolve()
    if output.exists() or (Path(args.manifest) if args.manifest else output.with_suffix(".manifest.json")).exists():
        raise FileExistsError("Choose a new output name; existing audio and manifests are preserved.")
    if output in {Path(args.text_file).resolve(), Path(args.plan).resolve(), reference}:
        raise ValueError("Output must not overwrite input or reference assets.")
    output.parent.mkdir(parents=True, exist_ok=True)
    configure_model_paths(model_root)

    pause_seconds = args.pause if args.pause is not None else STYLE_PAUSES[args.style]
    steps = args.steps if args.steps is not None else int(generation["inference_timesteps"])
    cfg_value = args.cfg_value if args.cfg_value is not None else float(generation["cfg_value"])
    max_audio_tokens = resolve_max_audio_tokens(profile, args.style)
    quality_retries = (
        args.quality_retries
        if args.quality_retries is not None
        else int(generation.get("pacing", {}).get("quality_retries", 1))
    )

    import soundfile as sf
    import torch
    from voxcpm import VoxCPM

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is not available in the VoxCPM2 environment.")

    started = time.perf_counter()
    model = VoxCPM.from_pretrained(
        str(model_dir),
        device="cuda",
        optimize=False,
        load_denoiser=False,
    )
    load_seconds = time.perf_counter() - started
    sample_rate = model.tts_model.sample_rate
    print(
        f"MODEL_READY seconds={load_seconds:.2f} sample_rate={sample_rate}",
        flush=True,
    )
    prompt_cache_started = time.perf_counter()
    if args.prompt_cache:
        prompt_cache_path = Path(args.prompt_cache).resolve()
    else:
        prompt_cache_path = resolve_prompt_cache(profile) or default_prompt_cache_path(
            reference,
            output,
        )
    prompt_cache, prompt_cache_source = load_or_build_prompt_cache(
        model=model,
        reference=reference,
        cache_path=prompt_cache_path,
    )
    prompt_cache_seconds = time.perf_counter() - prompt_cache_started
    print(
        f"PROMPT_CACHE source={prompt_cache_source} "
        f"seconds={prompt_cache_seconds:.2f}",
        flush=True,
    )
    audio_parts: list[np.ndarray] = []
    records: list[dict] = []

    with tempfile.TemporaryDirectory(prefix="voiceover-", dir=output.parent) as temp_name:
        temp_dir = Path(temp_name)
        parts_dir = output.parent / f"{output.stem}-parts" if args.keep_parts else temp_dir
        parts_dir.mkdir(parents=True, exist_ok=True)

        generation_started = time.perf_counter()
        for chunk in chunks:
            segment_started = time.perf_counter()
            audio = synthesize_chunk(
                model=model,
                text=chunk["text"],
                prompt_cache=prompt_cache,
                profile=profile,
                cfg_value=cfg_value,
                steps=steps,
                max_audio_tokens=max_audio_tokens,
            )
            audio = edge_fade(audio, sample_rate)
            duration = len(audio) / sample_rate
            record = dict(chunk)
            record.update(
                {
                    "duration_seconds": round(duration, 3),
                    "chars_per_second": round(
                        chunk["char_count"] / duration if duration else 0.0,
                        3,
                    ),
                    "quality_retries": 0,
                }
            )
            audio_parts.append(audio)
            records.append(record)
            print(
                f"SEGMENT {chunk['index']:02d}/{len(chunks)} "
                f"audio={duration:.2f}s "
                f"elapsed={time.perf_counter() - segment_started:.2f}s",
                flush=True,
            )

        quality = analyze_pacing(records, profile)
        baseline_cps = quality["baseline_chars_per_second"]
        if baseline_cps and quality_retries:
            for part_index, record in enumerate(records):
                if "pacing-fast" not in record["warnings"]:
                    continue
                best_audio = audio_parts[part_index]
                best_cps = record["chars_per_second"]
                best_distance = abs(best_cps - baseline_cps)
                for retry_index in range(1, quality_retries + 1):
                    candidate = synthesize_chunk(
                        model=model,
                        text=record["text"],
                        prompt_cache=prompt_cache,
                        profile=profile,
                        cfg_value=cfg_value,
                        steps=steps,
                        max_audio_tokens=max_audio_tokens,
                    )
                    candidate = edge_fade(candidate, sample_rate)
                    candidate_duration = len(candidate) / sample_rate
                    candidate_cps = (
                        record["char_count"] / candidate_duration
                        if candidate_duration
                        else float("inf")
                    )
                    candidate_distance = abs(candidate_cps - baseline_cps)
                    record["quality_retries"] = retry_index
                    if candidate_distance < best_distance:
                        best_audio = candidate
                        best_cps = candidate_cps
                        best_distance = candidate_distance
                audio_parts[part_index] = best_audio
                record["duration_seconds"] = round(len(best_audio) / sample_rate, 3)
                record["chars_per_second"] = round(best_cps, 3)
            quality = analyze_pacing(records, profile)

        target_rms = median(rms_dbfs(audio) for audio in audio_parts)
        for part_index, audio in enumerate(audio_parts):
            source_rms = rms_dbfs(audio)
            gain_db = float(np.clip(target_rms - source_rms, -2.5, 2.5))
            audio_parts[part_index] = audio * (10.0 ** (gain_db / 20.0))
            records[part_index]["rms_dbfs"] = round(source_rms, 3)
            records[part_index]["gain_db"] = round(gain_db, 3)

        assembled: list[np.ndarray] = []
        for index, audio in enumerate(audio_parts, start=1):
            part_name = chunks[index - 1].get("unit_id", f"part-{index:03d}")
            sf.write(parts_dir / f"{part_name}.wav", audio, sample_rate)
            assembled.append(audio)
            if index < len(audio_parts):
                assembled.append(
                    np.zeros(round(sample_rate * (
                        chunks[index - 1]["pause_after_ms"]["target"] / 1000.0
                        if plan else pause_seconds
                    )), dtype=np.float32)
                )
        master = np.concatenate(assembled)
        if args.speed == 1.0:
            sf.write(output, master, sample_rate)
        else:
            master_path = temp_dir / "master.wav"
            sf.write(master_path, master, sample_rate)
            ffmpeg = Path(environment["ffmpeg"])
            run_speed_adjustment(ffmpeg, master_path, output, args.speed)
        generation_seconds = time.perf_counter() - generation_started

    info = sf.info(output)
    manifest_path = (
        Path(args.manifest).resolve()
        if args.manifest
        else output.with_suffix(".manifest.json")
    )
    manifest = {
        "manifest_version": "2.0",
        "delivery_kind": args.stage,
        "source_file": str(Path(args.text_file).resolve()),
        "plan_file": str(Path(args.plan).resolve()),
        "audio_sha256": file_sha256(output).lower(),
        "settings": settings,
        "output": str(output),
        "source_sha256": hashlib.sha256(
            source_text.encode("utf-8")
        ).hexdigest().upper(),
        "normalized_text_sha256": hashlib.sha256(
            clean_text(source_text).encode("utf-8")
        ).hexdigest().upper(),
        "style": args.style,
        "speed": args.speed,
        "pause_seconds": None if plan else pause_seconds,
        "chunking": {
            "min_chars": min_chars,
            "max_chars": max_chars,
            "hard_boundary": "reading-plan" if plan else "blank-line",
            "max_audio_tokens": max_audio_tokens,
        },
        "prompt_cache": {
            "mode": "reference-only",
            "path": str(prompt_cache_path),
            "source": prompt_cache_source,
        },
        "quality": quality,
        "chunks": records,
    }
    manifest.update(delivery_binding)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    summary = {
        "output": str(output),
        "manifest": str(manifest_path),
        "duration_seconds": round(info.duration, 2),
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "chunks": len(chunks),
        "style": args.style,
        "speed": args.speed,
        "quality_status": quality["status"],
        "quality_warnings": quality["warning_count"],
        "model_load_seconds": round(load_seconds, 2),
        "prompt_cache_seconds": round(prompt_cache_seconds, 2),
        "prompt_cache_source": prompt_cache_source,
        "generation_seconds": round(generation_seconds, 2),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
