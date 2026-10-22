from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path


def normalized(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", text).lower()


def normalized_with_positions(text: str) -> tuple[str, list[int]]:
    characters = []
    positions = []
    for index, character in enumerate(text):
        if re.match(r"[0-9A-Za-z\u3400-\u9fff]", character):
            characters.append(character.lower())
            positions.append(index)
    return "".join(characters), positions


def check_phrase(
    words: list[dict],
    phrase: str,
    threshold: float,
    punctuation_threshold: float = 0.75,
) -> dict:
    tokens = [normalized(str(word.get("word", ""))) for word in words]
    joined = "".join(tokens)
    target, target_positions = normalized_with_positions(phrase)
    start = joined.find(target)
    if start < 0:
        return {"phrase": phrase, "status": "not_aligned", "max_internal_gap": None}
    end = start + len(target)
    spans = []
    cursor = 0
    for word, token in zip(words, tokens, strict=True):
        token_start = cursor
        token_end = cursor + len(token)
        cursor = token_end
        if token and token_end > start and token_start < end:
            spans.append(
                {
                    "word": word,
                    "target_start": max(token_start, start) - start,
                    "target_end": min(token_end, end) - start,
                }
            )
    if len(spans) < 2:
        return {"phrase": phrase, "status": "single_token_manual_check", "max_internal_gap": None}
    gap_records = []
    for left, right in zip(spans, spans[1:]):
        gap = max(
            0.0,
            float(right["word"]["start"]) - float(left["word"]["end"]),
        )
        boundary = int(left["target_end"])
        punctuation_between = False
        if 0 < boundary < len(target_positions):
            original_gap = phrase[
                target_positions[boundary - 1] + 1 : target_positions[boundary]
            ]
            punctuation_between = bool(re.search(r"[，、：；。！？,:;!?]", original_gap))
        allowed = punctuation_threshold if punctuation_between else threshold
        gap_records.append(
            {
                "gap": gap,
                "allowed": allowed,
                "punctuation_boundary": punctuation_between,
            }
        )
    max_gap = max((record["gap"] for record in gap_records), default=0.0)
    unexpected = [record for record in gap_records if record["gap"] > record["allowed"]]
    return {
        "phrase": phrase,
        "status": "pass" if not unexpected else "internal_pause_warning",
        "max_internal_gap": round(max_gap, 3),
        "max_unexpected_gap": round(
            max((record["gap"] for record in unexpected), default=0.0), 3
        ),
    }


def run(
    plan_path: Path,
    alignment_dir: Path,
    threshold: float,
    manifest_path: Path | None = None,
) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    allowed_unit_ids = None
    if manifest_path is not None:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        allowed_unit_ids = {unit["unit_id"] for unit in manifest.get("units", manifest.get("chunks", []))}
        if not allowed_unit_ids:
            raise ValueError("manifest does not contain generated units")
    results = []
    cps_values = []
    for unit in plan["units"]:
        if allowed_unit_ids is not None and unit["unit_id"] not in allowed_unit_ids:
            continue
        path = alignment_dir / f"{unit['unit_id']}.json"
        if not path.is_file():
            results.append({"unit_id": unit["unit_id"], "status": "missing_alignment"})
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        words = payload["words"] if isinstance(payload, dict) else payload
        phrase_results = [
            check_phrase(words, phrase, threshold)
            for phrase in unit.get("protected_phrases", [])
        ]
        if words:
            duration = max(float(words[-1]["end"]) - float(words[0]["start"]), 0.001)
            cps_values.append(len(normalized(unit["text"])) / duration)
        status = "pass"
        if any(item["status"] in {"not_aligned", "internal_pause_warning"} for item in phrase_results):
            status = "needs_review"
        elif any(item["status"] == "single_token_manual_check" for item in phrase_results):
            status = "manual_check"
        results.append({"unit_id": unit["unit_id"], "status": status, "phrases": phrase_results})
    baseline = statistics.median(cps_values) if cps_values else None
    overall = "pass" if results and all(item["status"] == "pass" for item in results) else "needs_review"
    return {
        "status": overall,
        "internal_pause_threshold_seconds": threshold,
        "median_aligned_chars_per_second": round(baseline, 3) if baseline else None,
        "units": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Screen protected phrases with per-unit word alignment.")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--alignment-dir", required=True)
    parser.add_argument("--threshold-seconds", type=float, default=0.24)
    parser.add_argument("--manifest")
    parser.add_argument("--output")
    args = parser.parse_args()
    report = run(
        Path(args.plan).resolve(),
        Path(args.alignment_dir).resolve(),
        args.threshold_seconds,
        Path(args.manifest).resolve() if args.manifest else None,
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).resolve().write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
