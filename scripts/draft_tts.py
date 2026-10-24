"""Temporary draft voice using edge-tts; never treated as approval for private voice."""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
from pathlib import Path


async def _stream(text: str, voice: str) -> tuple[bytes, list[dict]]:
    import edge_tts
    communicate = edge_tts.Communicate(text, voice=voice, boundary="WordBoundary")
    audio = bytearray()
    words: list[dict] = []
    async for item in communicate.stream():
        if item["type"] == "audio":
            audio.extend(item["data"])
        elif item["type"] == "WordBoundary":
            words.append({
                "word": str(item.get("text", "")),
                "start": float(item.get("offset", 0)) / 10_000_000,
                "end": (float(item.get("offset", 0)) + float(item.get("duration", 0))) / 10_000_000,
                "conf": 1.0,
            })
    return bytes(audio), words


def synthesize_draft(text: str, output: Path, *, voice: str = "zh-CN-XiaoxiaoNeural", timeout: float = 120.0) -> list[dict]:
    """Return word boundary evidence. Network errors are surfaced; no infinite retry."""
    try:
        encoded, words = asyncio.run(asyncio.wait_for(_stream(text, voice), timeout=timeout))
    except asyncio.TimeoutError as exc:
        raise TimeoutError("edge-tts draft timed out") from exc
    output.parent.mkdir(parents=True, exist_ok=True)
    # edge-tts emits MP3; convert format only, without atempo or pitch filters.
    try:
        import imageio_ffmpeg
        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("Draft stage needs edge-tts and imageio-ffmpeg") from exc
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0", "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", str(output)]
    subprocess.run(command, input=encoded, check=True)
    return words

