from __future__ import annotations

import argparse
import hashlib
import json
import re
from difflib import SequenceMatcher
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")


SKILL_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULES = SKILL_ROOT / "references" / "protected-phrases.json"
SCHEMA_VERSION = 1
PLAN_STATUS = "generated_needs_review"
MAX_UNIT_CHARS = 90


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().lower()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest().lower()


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def lexical_text(text: str) -> str:
    return re.sub(r"\s+", "", clean_text(text))


def paragraphs(text: str) -> list[str]:
    return [part.strip() for part in re.split(r"\n{2,}", clean_text(text)) if part.strip()]


def load_rules(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "protected_phrases": [str(item) for item in data.get("protected_phrases", [])],
        "pronunciation_map": {
            str(key): str(value) for key, value in data.get("pronunciation_map", {}).items()
        },
    }


def split_long_paragraph(paragraph: str, max_chars: int) -> list[str]:
    if len(paragraph) <= max_chars:
        return [paragraph]
    sentences = [part for part in re.split(r"(?<=[。！？!?；;])", paragraph) if part]
    if len(sentences) == 1:
        raise ValueError(
            f"paragraph has {len(paragraph)} characters but no safe sentence boundary: {paragraph}"
        )
    units: list[str] = []
    current = ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            raise ValueError(
                f"sentence has {len(sentence)} characters and must be revised visibly: {sentence}"
            )
        if current and len(current) + len(sentence) > max_chars:
            units.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        units.append(current)
    return units


def infer_intent(text: str) -> tuple[str, str]:
    if "下一条" in text or "下期" in text or "下一期" in text or text.startswith("给大家看"):
        return "next-preview", "friendly-advice"
    if (
        "这个问题，我们直接改了" in text
        or "真正重要" in text
        or text.startswith("原因不同")
        or text.startswith("所以")
        or text.startswith("而是先")
        or "只解决强化阶段" in text
    ):
        return "firm-resolution", "direct-judgment"
    if "今天正式发布" in text or "这就是我们的" in text:
        return "product-release", "natural-opening"
    if (
        "不用注册" in text
        or "打开页面就能使用" in text
        or "不会再设置账号和密码" in text
        or text.startswith("它会告诉你")
    ):
        return "clear-benefit", "direct-judgment"
    if "成本，由我们自己承担" in text or "成本由我们自己承担" in text:
        return "firm-resolution", "direct-judgment"
    if any(word in text for word in ("服务器压力", "网络安全防护", "成本也会", "成本，也会")):
        return "pain-explanation", "pain-explanation"
    if "有同学提到" in text or "有同学问" in text:
        return "user-feedback", "natural-opening"
    if (
        "我们想做" in text
        or "相信我们" in text
        or "评论区告诉我们" in text
        or "会陆续整合进小程序" in text
    ):
        return "sincere-close", "friendly-advice"
    if any(word in text for word in ("选择", "点击", "提交", "标记", "打开", "生成 PDF")):
        return "operation-guide", "friendly-advice"
    if any(word in text for word in ("真题", "多少题次", "涉及多少分", "报告", "问题分成")):
        return "evidence-explanation", "pain-explanation"
    if "？" in text or "?" in text or any(word in text for word in ("问题", "麻烦", "错题", "红叉")):
        return "pain-explanation", "pain-explanation"
    return "neutral-explanation", "natural-opening"


def pause_for(intent: str, text: str, hard_boundary: bool) -> dict[str, int]:
    target = 260
    if intent in {"firm-resolution", "product-release", "sincere-close"}:
        target = 380
    elif intent == "next-preview":
        target = 420
    elif text.endswith(("？", "?")):
        target = 340
    if not hard_boundary:
        target = 180
    return {
        "min": max(100, target - 120),
        "target": target,
        "max": target + 180,
    }


def build_plan(text_file: Path, rules_file: Path, max_unit_chars: int, previous_plan: dict | None = None) -> dict:
    source_text = text_file.read_text(encoding="utf-8-sig")
    cleaned = clean_text(source_text)
    source_paragraphs = paragraphs(cleaned)
    if not source_paragraphs:
        raise ValueError("final voiceover script is empty")
    rules = load_rules(rules_file)
    units: list[dict] = []
    for paragraph_index, paragraph in enumerate(source_paragraphs, start=1):
        parts = split_long_paragraph(paragraph, max_unit_chars)
        for part_index, part in enumerate(parts, start=1):
            protected = [phrase for phrase in rules["protected_phrases"] if phrase in part]
            intent, reference = infer_intent(part)
            hard_boundary = part_index == len(parts)
            units.append(
                {
                    "unit_id": f"u{len(units) + 1:03d}",
                    "paragraph_index": paragraph_index,
                    "text": part,
                    "protected_phrases": protected,
                    "intent": intent,
                    "reference_profile": reference,
                    "hard_boundary_after": hard_boundary,
                    "pause_after_ms": pause_for(intent, part, hard_boundary),
                }
            )
    previous = (previous_plan or {}).get('units', [])
    old_ids = [u['unit_id'] for u in previous]
    if any(not re.fullmatch(r'u\d{3,}', value) for value in old_ids) or len(set(old_ids)) != len(old_ids):
        raise ValueError('Previous plan has unsafe or duplicate unit IDs')
    counter = max([int((previous_plan or {}).get('last_unit_number', 0))] + [int(v[1:]) for v in old_ids])
    assigned = {}
    for tag, a, b, c, d in SequenceMatcher(None, [u['text'] for u in previous], [u['text'] for u in units], autojunk=False).get_opcodes():
        if tag in {'equal', 'replace'}:
            for old, new in zip(range(a, b), range(c, d)):
                assigned[new] = previous[old]['unit_id']
    for index, unit in enumerate(units):
        if index not in assigned:
            counter += 1
            assigned[index] = f'u{counter:03d}'
        unit['unit_id'] = assigned[index]
        unit['text_sha256'] = text_sha256(unit['text'])
    pronunciation_map = {
        written: spoken
        for written, spoken in rules["pronunciation_map"].items()
        if written in cleaned or spoken in cleaned
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "status": PLAN_STATUS,
        "source_file": str(text_file.resolve()),
        "source_sha256": file_sha256(text_file),
        "rules_sha256": file_sha256(rules_file),
        "lexical_sha256": text_sha256(lexical_text(cleaned)),
        "source_paragraph_count": len(source_paragraphs),
        "unit_count": len(units),
        "last_unit_number": counter,
        "pronunciation_map": pronunciation_map,
        "units": units,
    }


def validate_plan(text_file: Path, plan_file: Path, rules_file: Path = DEFAULT_RULES) -> list[str]:
    errors: list[str] = []
    source_text = clean_text(text_file.read_text(encoding="utf-8-sig"))
    source_paragraphs = paragraphs(source_text)
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if plan.get("schema_version") != SCHEMA_VERSION:
        errors.append("unsupported schema_version")
    if plan.get("source_sha256") != file_sha256(text_file):
        errors.append("source_sha256 does not match the current final script")
    if plan.get("lexical_sha256") != text_sha256(lexical_text(source_text)):
        errors.append("lexical_sha256 does not match the current final script")
    if plan.get("rules_sha256") != file_sha256(rules_file):
        errors.append("rules_sha256 does not match the current protection rules")
    rules = load_rules(rules_file)
    units = plan.get("units")
    if not isinstance(units, list) or not units:
        errors.append("units must be a non-empty list")
        return errors
    reconstructed: dict[int, str] = {}
    seen_ids: set[str] = set()
    last_paragraph_index = 1
    for index, unit in enumerate(units, start=1):
        unit_id = unit.get("unit_id")
        if not isinstance(unit_id, str) or not re.fullmatch(r'u\d{3,}', unit_id) or unit_id in seen_ids:
            errors.append(f"invalid or duplicate unit_id at unit {index}")
        seen_ids.add(str(unit_id))
        paragraph_index = unit.get("paragraph_index")
        if not isinstance(paragraph_index, int) or not 1 <= paragraph_index <= len(source_paragraphs):
            errors.append(f"invalid paragraph_index at unit {index}")
            continue
        if paragraph_index < last_paragraph_index or paragraph_index > last_paragraph_index + 1:
            errors.append(f"paragraphs are out of order at unit {index}")
        last_paragraph_index = paragraph_index
        unit_text = unit.get("text")
        if not isinstance(unit_text, str) or not unit_text:
            errors.append(f"empty text at unit {index}")
            continue
        if unit.get('text_sha256', text_sha256(unit_text)) != text_sha256(unit_text):
            errors.append(f'text checksum mismatch at unit {index}')
        reconstructed[paragraph_index] = reconstructed.get(paragraph_index, "") + unit_text
        for phrase in unit.get("protected_phrases", []):
            if phrase not in unit_text:
                errors.append(f"protected phrase is not wholly inside unit {index}: {phrase}")
        pause = unit.get("pause_after_ms", {})
        values = [pause.get("min"), pause.get("target"), pause.get("max")]
        if not all(isinstance(value, int) for value in values) or not 0 <= values[0] <= values[1] <= values[2] <= 10000:
            errors.append(f"invalid pause range at unit {index}")
    for paragraph_index, paragraph in enumerate(source_paragraphs, start=1):
        if reconstructed.get(paragraph_index, "") != paragraph:
            errors.append(f"unit text does not reconstruct paragraph {paragraph_index}")
        matching = [unit for unit in units if unit.get("paragraph_index") == paragraph_index]
        if matching and not matching[-1].get("hard_boundary_after"):
            errors.append(f"paragraph {paragraph_index} does not end with a hard boundary")
        if any(unit.get("hard_boundary_after") for unit in matching[:-1]):
            errors.append(f"paragraph {paragraph_index} has an early hard boundary")
    for phrase in rules["protected_phrases"]:
        if phrase not in source_text:
            continue
        matching = [unit for unit in units if phrase in unit.get("text", "")]
        if not matching or any(
            phrase not in unit.get("protected_phrases", []) for unit in matching
        ):
            errors.append(f"required protected phrase is missing or split: {phrase}")
    if plan.get("unit_count") != len(units):
        errors.append("unit_count does not match units")
    return errors


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build or validate a machine voiceover plan.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--text-file", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--rules", default=str(DEFAULT_RULES))
    build.add_argument("--max-unit-chars", type=int, default=MAX_UNIT_CHARS)
    build.add_argument("--previous-plan", help="Reuse stable IDs when revising an existing script")
    validate = subparsers.add_parser("validate")
    validate.add_argument("--text-file", required=True)
    validate.add_argument("--plan", required=True)
    validate.add_argument("--rules", default=str(DEFAULT_RULES))
    approve = subparsers.add_parser("approve")
    approve.add_argument("--text-file", required=True)
    approve.add_argument("--plan", required=True)
    approve.add_argument("--rules", default=str(DEFAULT_RULES))
    approve.add_argument("--reviewed-by", default="codex-semantic-review")
    approve.add_argument("--note", default="content, intent, pauses, and protected phrases reviewed")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        text_file = Path(args.text_file).resolve()
        if args.command == "build":
            output = Path(args.output).resolve()
            previous = json.loads(Path(args.previous_plan).read_text(encoding='utf-8')) if args.previous_plan else None
            plan = build_plan(text_file, Path(args.rules).resolve(), args.max_unit_chars, previous)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"plan": str(output), "units": plan["unit_count"], "status": plan["status"]}, ensure_ascii=False))
            return 0
        plan_path = Path(args.plan).resolve()
        errors = validate_plan(text_file, plan_path, Path(args.rules).resolve())
        if args.command == "approve":
            if errors:
                print(json.dumps({"ok": False, "errors": errors}, ensure_ascii=False, indent=2))
                return 1
            plan = json.loads(plan_path.read_text(encoding="utf-8"))
            plan["status"] = "approved"
            plan["approval"] = {
                "reviewed_by": args.reviewed_by,
                "note": args.note,
                "approved_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            }
            plan_path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps({"ok": True, "status": "approved", "plan": str(plan_path)}, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    except (FileNotFoundError, ValueError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
