#!/usr/bin/env python3
"""Benchmark faster-whisper on this machine, using real captured audio.

Run this on the Pi 5. It answers two questions at once: which model is
accurate enough on *your* table-distance audio, and how long each takes on
this CPU. Clean sample audio would answer neither.

    pip install faster-whisper
    python bench_whisper.py --url https://cogneuro-alexa.vercel.app
    python bench_whisper.py clip1.wav clip2.wav

Reports transcript, wall time, and realtime factor (elapsed / audio
duration). Under 1.0 means faster than the audio itself.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path

DEFAULT_MODELS = ["tiny.en", "base.en", "small.en"]


def fetch_clips(base_url: str, limit: int, into: Path) -> list[Path]:
    into.mkdir(parents=True, exist_ok=True)
    listing = urllib.parse.urljoin(base_url.rstrip("/") + "/", "api/captures")
    with urllib.request.urlopen(listing, timeout=30) as response:
        import json

        payload = json.load(response)

    items = payload if isinstance(payload, list) else payload.get("captures", payload.get("items", []))
    if not items:
        print(f"No captures listed at {listing}", file=sys.stderr)
        return []

    paths: list[Path] = []
    for item in items[:limit]:
        clip_id = item.get("id") or item.get("key")
        if not clip_id:
            continue
        url = urllib.parse.urljoin(
            base_url.rstrip("/") + "/", "api/audio?id=" + urllib.parse.quote(clip_id)
        )
        target = into / f"{clip_id}.wav"
        if not target.exists():
            print(f"  downloading {clip_id}")
            with urllib.request.urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
        paths.append(target)
    return paths


def duration_seconds(path: Path) -> float:
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except Exception:
        return 0.0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("clips", nargs="*", help="local WAV files")
    parser.add_argument("--url", help="review page base URL to pull recent clips from")
    parser.add_argument("--limit", type=int, default=3, help="how many clips to pull")
    parser.add_argument("--models", default=",".join(DEFAULT_MODELS))
    parser.add_argument("--compute-type", default="int8")
    parser.add_argument("--threads", type=int, default=os.cpu_count() or 4)
    args = parser.parse_args()

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("pip install faster-whisper", file=sys.stderr)
        return 1

    paths = [Path(c) for c in args.clips]
    if args.url:
        paths += fetch_clips(args.url, args.limit, Path("clips"))
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("No clips to test. Pass WAV paths or --url.", file=sys.stderr)
        return 1

    print(f"\n{len(paths)} clip(s), {args.threads} threads, compute_type={args.compute_type}\n")

    for name in [m.strip() for m in args.models.split(",") if m.strip()]:
        print(f"=== {name} ===")
        load_start = time.perf_counter()
        try:
            model = WhisperModel(
                name, device="cpu", compute_type=args.compute_type, cpu_threads=args.threads
            )
        except Exception as exc:  # noqa: BLE001
            print(f"  could not load: {exc}\n")
            continue
        print(f"  load: {time.perf_counter() - load_start:.1f}s")

        for path in paths:
            audio_seconds = duration_seconds(path)
            start = time.perf_counter()
            segments, _info = model.transcribe(str(path), beam_size=1, language="en")
            text = " ".join(segment.text.strip() for segment in segments).strip()
            elapsed = time.perf_counter() - start
            factor = (elapsed / audio_seconds) if audio_seconds else float("nan")
            print(f"  {path.name[:34]:<34} {elapsed:5.2f}s  ({factor:.2f}x realtime)")
            print(f"      {text or '(nothing transcribed)'}")
        print()

    print("Rule of thumb: you want the whole turn — STT + LLM + TTS — under a")
    print("couple of seconds. Whisper should be well under 1.0x realtime here.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
