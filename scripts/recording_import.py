"""Import numbered human recordings without time stretching or guessed cuts."""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np


def write_recording_script(plan: dict, output: Path) -> Path:
    lines = ["按编号录音；编号只看不读。每个单元录完停一下，不要加速。", ""]
    for index, unit in enumerate(plan["units"], 1):
        lines.append(f"[{index:03d} | {unit['unit_id']}] {unit['text']}")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return output


def _load_words(alignment_file: Path) -> list[dict]:
    payload = json.loads(alignment_file.read_text(encoding="utf-8"))
    return payload.get("words", payload if isinstance(payload, list) else [])


def split_whole_recording(audio: np.ndarray, sample_rate: int, plan: dict, words: list[dict], normalized) -> list[np.ndarray]:
    """Cut only at recognized word boundaries; fail closed on mismatch or ambiguity."""
    expected = [normalized(unit["text"]) for unit in plan["units"]]
    observed = [normalized(str(word.get("word", ""))) for word in words]
    if "".join(observed) != "".join(expected):
        raise ValueError("Whole-take transcript does not exactly match the approved plan; no cuts were guessed.")
    offsets, cursor = [], 0
    for target in expected:
        if not target:
            raise ValueError("Empty recording unit")
        span, assembled = [], ""
        while assembled != target and cursor < len(words):
            word = words[cursor]
            token = normalized(str(word.get("word", "")))
            if not token or not target.startswith(assembled + token):
                raise ValueError(f"Word boundary does not match unit {plan['units'][len(offsets)]['unit_id']}")
            span.append(word)
            assembled += token
            cursor += 1
        if assembled != target or any(float(w.get("conf", 0)) < .75 for w in span):
            raise ValueError(f"Low-confidence boundary for unit {plan['units'][len(offsets)]['unit_id']}")
        start = float(span[0]["start"])
        end = float(span[-1]["end"])
        offsets.append((start, end))
    result = []
    for index, (start, end) in enumerate(offsets):
        left = round(max(0.0, start - .05) * sample_rate)
        right = round(min(len(audio) / sample_rate, end + .05) * sample_rate)
        if right <= left:
            raise ValueError(f"Invalid cut for unit {plan['units'][index]['unit_id']}")
        result.append(np.asarray(audio[left:right], dtype=np.float32))
    return result

