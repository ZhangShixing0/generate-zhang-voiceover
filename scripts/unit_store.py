"""Content-addressed unit audio cache and explicit per-unit approvals."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def render_key(unit: dict, *, profile: dict, settings: dict, engine: str, code_version: str) -> str:
    payload = {
        "unit_id": unit["unit_id"],
        "text": unit["text"],
        "text_sha256": unit.get("text_sha256"),
        "profile": profile,
        "settings": settings,
        "engine": engine,
        "code_version": code_version,
    }
    return canonical_sha256(payload)


def cache_paths(root: Path, unit_id: str, key: str) -> tuple[Path, Path]:
    safe = "".join(char for char in unit_id if char.isalnum() or char in "-_")
    directory = root / "units"
    return directory / f"{safe}-{key[:16]}.wav", directory / f"{safe}-{key[:16]}.json"


def load_cached(root: Path, unit_id: str, key: str) -> tuple[Path, dict] | None:
    audio, metadata = cache_paths(root, unit_id, key)
    if not audio.is_file() or not metadata.is_file():
        return None
    payload = json.loads(metadata.read_text(encoding="utf-8"))
    if payload.get("render_key") != key or payload.get("audio_sha256") != file_sha256(audio):
        return None
    return audio, payload


def save_cached(root: Path, unit_id: str, key: str, audio_path: Path, payload: dict) -> tuple[Path, Path]:
    output, metadata = cache_paths(root, unit_id, key)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(audio_path.read_bytes())
    record = dict(payload, render_key=key, audio_sha256=file_sha256(output), unit_id=unit_id)
    metadata.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output, metadata


def approval_path(root: Path, unit_id: str) -> Path:
    safe = "".join(char for char in unit_id if char.isalnum() or char in "-_")
    return root / "approvals" / f"{safe}.json"


def approve_unit(root: Path, *, unit: dict, render_key_value: str, audio_path: Path,
                 quality: dict, confirmed_by_user: bool) -> dict:
    if not confirmed_by_user:
        raise ValueError("Per-unit approval requires an explicit user confirmation.")
    if quality.get("status") != "pass":
        raise ValueError("Only a unit that passed measured audio QA can be approved.")
    payload = {
        "decision": "approved",
        "unit_id": unit["unit_id"],
        "text_sha256": unit.get("text_sha256"),
        "render_key": render_key_value,
        "audio": str(audio_path.resolve()),
        "audio_sha256": file_sha256(audio_path),
        "quality_sha256": canonical_sha256(quality),
    }
    path = approval_path(root, unit["unit_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def valid_approval(root: Path, unit: dict, key: str) -> dict | None:
    path = approval_path(root, unit["unit_id"])
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    audio = Path(payload.get("audio", ""))
    if (payload.get("decision") != "approved" or payload.get("render_key") != key
            or payload.get("text_sha256") != unit.get("text_sha256")
            or not audio.is_file() or payload.get("audio_sha256") != file_sha256(audio)):
        return None
    return payload

