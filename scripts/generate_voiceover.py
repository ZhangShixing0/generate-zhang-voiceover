from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
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
    # Compatibility report for manifests produced before word alignment. New
    # synthesis uses audio_quality.assess_audio; these explicit signals prevent
    # the old helper from calling a missing alignment pass.
    if len(records) == 1 and not records[0].get("phrase_rates") and not records[0].get("alignment"):
        return {"status": "needs-review", "warning_count": 1, "warnings": ["missing-alignment"], "baseline_chars_per_second": None}
    phrase_warnings = []
    for record in records:
        rates = record.get("phrase_rates") or []
        for left, right in zip(rates, rates[1:]):
            if min(left, right) <= 0 or max(left, right) / min(left, right) - 1 > .10:
                phrase_warnings.append("phrase-rate-jump")
    if phrase_warnings:
        return {"status": "needs-review", "warning_count": len(phrase_warnings), "warnings": phrase_warnings, "baseline_chars_per_second": None}
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
    parser = argparse.ArgumentParser(description="Generate measured draft or VoxCPM2 voiceover units.")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--text")
    source.add_argument("--text-file")
    parser.add_argument("--output")
    parser.add_argument("--profile")
    parser.add_argument("--plan")
    parser.add_argument("--rules", default=str(SKILL_ROOT / "references" / "protected-phrases.json"))
    parser.add_argument("--stage", choices=("draft", "preview", "final", "recording-script", "recording"), default="preview")
    parser.add_argument("--preview-seconds", type=float, default=28)
    parser.add_argument("--approval")
    parser.add_argument("--style", choices=sorted(STYLE_PAUSES), default="natural")
    parser.add_argument("--steps", type=int)
    parser.add_argument("--cfg-value", type=float)
    parser.add_argument("--quality-retries", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--chunk-min-chars", type=int)
    parser.add_argument("--chunk-max-chars", type=int)
    parser.add_argument("--manifest")
    parser.add_argument("--prompt-cache")
    parser.add_argument("--inspect-chunks", action="store_true")
    parser.add_argument("--keep-parts", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--cache-dir")
    parser.add_argument("--vosk-model-dir")
    parser.add_argument("--draft-voice", default="zh-CN-XiaoxiaoNeural")
    parser.add_argument("--recording-script")
    parser.add_argument("--recording-audio")
    parser.add_argument("--recording-alignment")
    args = parser.parse_args()

    if not args.validate_only and args.text is None and args.text_file is None:
        parser.error("one of --text or --text-file is required")
    if not args.validate_only and not args.inspect_chunks and not args.output:
        parser.error("--output is required")
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
    if args.plan and (args.chunk_min_chars is not None or args.chunk_max_chars is not None):
        parser.error("A reading plan controls units; chunk overrides are not allowed")
    if not 15 <= args.preview_seconds <= 35:
        parser.error("--preview-seconds must be between 15 and 35")
    return args


def main() -> int:
    args = parse_args()
    if args.validate_only:
        profile = load_profile(args.profile)
        print(json.dumps(validate_environment(profile, require_ffmpeg=False), ensure_ascii=False, indent=2))
        return 0
    from voiceover_runner import run
    return run(args, globals_module())


def globals_module():
    """Small explicit adapter so the runner can use patched helpers in tests."""
    import sys
    return sys.modules[__name__]


if __name__ == '__main__':
    raise SystemExit(main())
