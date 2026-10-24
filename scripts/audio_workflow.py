"""Content-locked VoxCPM2 delivery and preview approval; no model imports."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from voiceover_plan import DEFAULT_RULES, file_sha256, validate_plan
from unit_store import approve_unit


def object_sha256(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def load_approved_plan(text_file: Path, plan_file: Path, rules_file: Path = DEFAULT_RULES) -> dict:
    errors = validate_plan(text_file, plan_file, rules_file)
    if errors:
        raise ValueError("; ".join(errors))
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if plan.get("status") != "approved":
        raise ValueError("Review the plan's semantics before generating a preview.")
    return plan


def plan_chunks(plan: dict, stage: str, preview_seconds: float = 28) -> list[dict]:
    if stage not in {"preview", "final"}:
        raise ValueError("stage must be preview or final")
    if not 15 <= preview_seconds <= 35:
        raise ValueError("preview_seconds must be between 15 and 35")
    chunks = []
    estimate = 0.0
    for unit in plan["units"]:
        chars = sum(c.isalnum() for c in unit["text"])
        chunks.append({
            "index": len(chunks) + 1,
            "unit_id": unit["unit_id"],
            "paragraph_index": unit["paragraph_index"],
            "text": unit["text"],
            "char_count": chars,
            "pause_after_ms": unit["pause_after_ms"],
            "protected_phrases": unit["protected_phrases"],
            "intent": unit["intent"],
        })
        estimate += chars / 4.5 + unit["pause_after_ms"]["target"] / 1000
        if stage == "preview" and estimate >= preview_seconds:
            break
    return chunks


def binding(text_file: Path, plan_file: Path, profile: dict, settings: dict) -> dict:
    return {
        "source_sha256": file_sha256(text_file),
        "plan_sha256": file_sha256(plan_file),
        "profile_sha256": object_sha256(profile),
        "settings_sha256": object_sha256(settings),
    }


def approve_preview(manifest_file: Path, approval_file: Path, confirmed_by_user: bool) -> dict:
    if not confirmed_by_user:
        raise ValueError("Record approval only after the user has listened and explicitly accepted.")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("delivery_kind") != "preview":
        raise ValueError("Only a preview manifest can be approved.")
    audio_file = Path(manifest["output"])
    if file_sha256(audio_file) != manifest.get("audio_sha256"):
        raise ValueError("Preview audio has changed since generation.")
    for field, path_field in (("source_sha256", "source_file"), ("plan_sha256", "plan_file")):
        if file_sha256(Path(manifest[path_field])) != manifest.get(field):
            raise ValueError(f"Preview is stale: {field}")
    approval = {
        "decision": "approved",
        **{key: manifest[key] for key in (
            "source_sha256", "plan_sha256", "profile_sha256", "settings_sha256"
        )},
        "preview_manifest": str(manifest_file.resolve()),
        "preview_manifest_sha256": file_sha256(manifest_file),
        "preview_audio": str(audio_file.resolve()),
        "preview_audio_sha256": file_sha256(audio_file),
        "approved_at": datetime.now(timezone.utc).isoformat(),
    }
    approval_file.parent.mkdir(parents=True, exist_ok=True)
    approval_file.write_text(json.dumps(approval, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return approval


def validate_approval(approval_file: Path, expected: dict) -> None:
    approval = json.loads(approval_file.read_text(encoding="utf-8"))
    for key, value in {"decision": "approved", **expected}.items():
        if approval.get(key) != value:
            raise ValueError(f"Preview approval is stale or mismatched: {key}")
    for path_key, hash_key in (
        ("preview_audio", "preview_audio_sha256"),
        ("preview_manifest", "preview_manifest_sha256"),
    ):
        if file_sha256(Path(approval[path_key])) != approval.get(hash_key):
            raise ValueError(f"Approved preview has changed: {path_key}")
    preview = json.loads(Path(approval["preview_manifest"]).read_text(encoding="utf-8"))
    if preview.get("delivery_kind") != "preview":
        raise ValueError("Approved manifest is not a preview.")
    for key, value in expected.items():
        if preview.get(key) != value:
            raise ValueError(f"Preview manifest mismatch: {key}")
    if Path(preview["output"]).resolve() != Path(approval["preview_audio"]).resolve():
        raise ValueError("Approval refers to a different preview audio file.")
    if preview.get("audio_sha256") != approval["preview_audio_sha256"]:
        raise ValueError("Preview audio hash does not match the manifest.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record the user's acceptance of a generated preview.")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approval", type=Path)
    parser.add_argument("--confirmed-by-user", action="store_true")
    parser.add_argument("--unit-id", help="Approve one unit from a v3 manifest")
    parser.add_argument("--approval-root", help="Cache root containing units/ and approvals/")
    args = parser.parse_args()
    if args.unit_id:
        manifest = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
        row = next((item for item in manifest.get("units", []) if item.get("unit_id") == args.unit_id), None)
        if not row:
            raise ValueError(f"Unit not found: {args.unit_id}")
        plan_data = json.loads(Path(manifest["plan_file"]).read_text(encoding="utf-8"))
        unit = next(unit for unit in plan_data["units"] if unit["unit_id"] == args.unit_id)
        result = approve_unit(Path(args.approval_root or manifest["cache_dir"]), unit=unit, render_key_value=row["render_key"], audio_path=Path(row["audio"]), quality=row.get("quality", {}), confirmed_by_user=args.confirmed_by_user)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    if not args.approval:
        raise ValueError("--approval is required when recording a whole-preview approval")
    approve_preview(args.manifest, args.approval, args.confirmed_by_user)
    print(json.dumps({"approval": str(args.approval), "decision": "approved"}))


if __name__ == "__main__":
    main()
