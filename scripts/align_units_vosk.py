from __future__ import annotations

import argparse
import json
import wave
from pathlib import Path

from vosk import KaldiRecognizer, Model, SetLogLevel


def align_file(model: Model, wav_path: Path) -> dict:
    with wave.open(str(wav_path), "rb") as wav:
        if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
            raise ValueError(f"WAV must be mono 16-bit PCM: {wav_path}")
        recognizer = KaldiRecognizer(model, wav.getframerate())
        recognizer.SetWords(True)
        results = []
        while True:
            data = wav.readframes(4000)
            if not data:
                break
            if recognizer.AcceptWaveform(data):
                results.append(json.loads(recognizer.Result()))
        results.append(json.loads(recognizer.FinalResult()))
    words = []
    transcripts = []
    for result in results:
        if result.get("text"):
            transcripts.append(result["text"])
        for word in result.get("result", []):
            words.append(
                {
                    "word": str(word.get("word", "")),
                    "start": round(float(word.get("start", 0.0)), 3),
                    "end": round(float(word.get("end", 0.0)), 3),
                    "conf": round(float(word.get("conf", 0.0)), 3),
                }
            )
    return {"unit_id": wav_path.stem, "transcript": "".join(transcripts), "words": words}


def main() -> int:
    parser = argparse.ArgumentParser(description="Align per-unit PCM WAV files with one Vosk model load.")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--wav-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    model_dir = Path(args.model_dir).resolve()
    wav_dir = Path(args.wav_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    wav_files = sorted(wav_dir.glob("u*.wav"))
    if not wav_files:
        raise FileNotFoundError(f"no unit WAV files found: {wav_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    SetLogLevel(-1)
    model = Model(str(model_dir))
    summary = []
    for wav_path in wav_files:
        result = align_file(model, wav_path)
        output_path = output_dir / f"{wav_path.stem}.json"
        output_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        summary.append(
            {
                "unit_id": wav_path.stem,
                "word_count": len(result["words"]),
                "transcript": result["transcript"],
            }
        )
    print(json.dumps({"aligned_units": len(summary), "units": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
