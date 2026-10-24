"""Lossless unit assembly and video-facing timing artifacts."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from audio_quality import QualityPolicy
from unit_store import file_sha256


def _srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _merge_intervals(intervals: list[list[float]], padding: float = 0.0) -> list[list[float]]:
    expanded = sorted([[max(0.0, start - padding), end + padding] for start, end in intervals])
    merged: list[list[float]] = []
    for start, end in expanded:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _complement(intervals: list[list[float]], duration: float) -> list[list[float]]:
    result, cursor = [], 0.0
    for start, end in _merge_intervals(intervals):
        if start > cursor:
            result.append([cursor, start])
        cursor = max(cursor, end)
    if cursor < duration:
        result.append([cursor, duration])
    return result


def _offset_words(words: list[dict], offset: float) -> list[dict]:
    return [dict(word, start=round(word["start"] + offset, 4), end=round(word["end"] + offset, 4)) for word in words]


def assemble_delivery(*, plan: dict, unit_records: list[dict], output: Path,
                      sample_rate: int, stage: str, source_file: Path | None = None,
                      plan_file: Path | None = None, quality_policy: QualityPolicy | None = None) -> dict:
    """Assemble without gain normalization or time stretching; generate all video artifacts."""
    import soundfile as sf

    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    parts_dir = output.parent / f"{output.stem}-parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    master_parts: list[np.ndarray] = []
    timeline_units, subtitles, speech_masks = [], [], []
    cursor = 0.0
    unit_index = 0
    for record in unit_records:
        unit = record["unit"]
        audio_path = Path(record["audio"])
        audio, rate = sf.read(audio_path, dtype="float32", always_2d=False)
        if audio.ndim != 1 or rate != sample_rate:
            raise ValueError(f"Unit {unit['unit_id']} must be mono at {sample_rate} Hz")
        target = parts_dir / f"{unit['unit_id']}.wav"
        target.write_bytes(audio_path.read_bytes())
        duration = len(audio) / rate
        start, end = cursor, cursor + duration
        quality = record.get("quality", {})
        words = _offset_words(quality.get("words", []), start)
        phrases = []
        for phrase in quality.get("phrases", []):
            phrases.append(dict(phrase, start=round(phrase["start"] + start, 4), end=round(phrase["end"] + start, 4)))
        if words:
            speech = [[round(a + start, 4), round(b + start, 4)] for a, b in quality.get("speech_intervals", [])]
            uncertain = False
        else:
            # Without alignment evidence, mask the whole unit conservatively.
            speech = [[start, end]]
            uncertain = True
        speech_masks.extend(speech)
        timeline_units.append({
            "index": unit_index + 1, "unit_id": unit["unit_id"], "text": unit["text"],
            "text_sha256": unit.get("text_sha256"), "start": round(start, 4), "end": round(end, 4),
            "audio": str(target), "audio_sha256": file_sha256(target), "speech_intervals": speech,
            "phrases": phrases, "words": words, "alignment_uncertain": uncertain,
            "pause_after_ms": unit["pause_after_ms"],
        })
        subtitles.append((start, end, unit["text"]))
        master_parts.append(audio)
        cursor = end
        unit_index += 1
        pause_ms = unit["pause_after_ms"]["target"] if unit_index < len(unit_records) else 0
        if pause_ms:
            silence = np.zeros(round(sample_rate * pause_ms / 1000), dtype=np.float32)
            master_parts.append(silence)
            cursor += len(silence) / sample_rate
    if not master_parts:
        raise ValueError("No unit audio to assemble")
    master = np.concatenate(master_parts)
    sf.write(output, master, sample_rate, subtype="PCM_16")
    duration = len(master) / sample_rate
    padded_speech = _merge_intervals(speech_masks, padding=0.08)
    timeline = {
        "timeline_version": "1.0", "stage": stage, "sample_rate": sample_rate,
        "duration_seconds": round(duration, 4), "units": timeline_units,
        "speech_intervals": padded_speech,
        "sfx_safe_intervals": _complement(padded_speech, duration),
        "alignment_policy": {"word_timestamps": True, "unknown_units_masked_whole": True},
        "source_file": str(source_file.resolve()) if source_file else None,
        "plan_file": str(plan_file.resolve()) if plan_file else None,
        "audio_sha256": file_sha256(output),
    }
    timeline_path = output.with_name("timeline.json")
    timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    srt_path = output.with_suffix(".srt")
    srt_path.write_text("\n\n".join(f"{i}\n{_srt_time(start)} --> {_srt_time(end)}\n{text}" for i, (start, end, text) in enumerate(subtitles, 1)) + "\n", encoding="utf-8")
    return {"output": str(output), "timeline": str(timeline_path), "srt": str(srt_path), "parts_dir": str(parts_dir), "duration_seconds": round(duration, 4), "audio_sha256": timeline["audio_sha256"]}

