#!/usr/bin/env python3
"""The Pi-side server: audio in, audio out.

One process holds everything — Whisper, the conversation loop, Piper — so a
turn is three in-process steps rather than three network hops. The board
POSTs a recording and gets spoken audio back in the response body.

    POST /talk   WAV or raw 16-bit PCM  ->  audio/wav reply
    POST /text   {"text": "..."}        ->  {"reply": "..."}   (no audio, for testing)
    GET  /health

Run:
    export PIPER_MODEL=~/piper/en_US-lessac-medium.onnx
    python server.py            # listens on 0.0.0.0:8080

Models load at startup, not per request — Whisper base.en takes ~12s to
load and ~0.3x realtime to run, so loading it per turn would dominate.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, stream_with_context

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from conversation import Conversation
from speech import (
    Speaker,
    Transcriber,
    looks_like_wav,
    pcm_to_wav,
    trim_leading_ms,
    wav_duration,
)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("server")

# Reset the conversation if nobody has spoken for this long. Without it,
# tomorrow's breakfast continues yesterday's argument.
IDLE_RESET_SECONDS = int(os.environ.get("IDLE_RESET_SECONDS", "600"))

MAX_BODY_BYTES = 8 * 1024 * 1024


# Spoken the instant Whisper finishes, while the model is still thinking.
# This does not make anything faster — it fills the silence the LLM round
# trips create, which is how commercial assistants hide the same gap.
# Set ACK_PHRASES="" to turn it off.
DEFAULT_ACKS = "Okay.|Sure.|Right."


class Assistant:
    def __init__(self):
        self.transcriber = Transcriber()
        self.conversation = Conversation()
        self.speaker: Speaker | None
        try:
            self.speaker = Speaker()
            log.info("piper ready: %s", self.speaker.model_path.name)
        except RuntimeError as exc:
            self.speaker = None
            log.warning("TTS disabled: %s", exc)
        self._last_turn = 0.0
        self._acks: list[bytes] = self._prepare_acks()
        self._ack_index = 0
        provider = self.conversation.provider
        log.info(
            "llm: %s / %s", provider.name, getattr(provider, "model", "?")
        )
        log.info(
            "tools offered: %s",
            ", ".join(s.name for s in self.conversation.specs) or "(none)",
        )

    def _prepare_acks(self) -> list[bytes]:
        """Synthesize the acknowledgements once, at startup.

        Doing it per turn would cost exactly the time it exists to hide.
        """
        raw = os.environ.get("ACK_PHRASES", DEFAULT_ACKS).strip()
        if not raw or self.speaker is None:
            return []
        phrases = [p.strip() for p in raw.split("|") if p.strip()]
        clips: list[bytes] = []
        for phrase in phrases:
            try:
                clips.append(self.speaker.synthesize_pcm(phrase))
            except RuntimeError as exc:
                log.warning("could not pre-synthesize %r: %s", phrase, exc)
        if clips:
            log.info("acknowledgements ready: %s", ", ".join(phrases[: len(clips)]))
        return clips

    def next_ack(self) -> bytes | None:
        """Rotate, so it does not say the same word every single time."""
        if not self._acks:
            return None
        clip = self._acks[self._ack_index % len(self._acks)]
        self._ack_index += 1
        return clip

    def _maybe_reset(self) -> None:
        now = time.monotonic()
        if self._last_turn and (now - self._last_turn) > IDLE_RESET_SECONDS:
            log.info("idle %.0fs — starting a fresh conversation", now - self._last_turn)
            self.conversation.reset()
        self._last_turn = now

    def reply_to_text(self, text: str, on_sentence=None) -> str:
        self._maybe_reset()
        return self.conversation.ask(text, on_sentence=on_sentence)


app = Flask(__name__)
assistant: Assistant | None = None


def _get_assistant() -> Assistant:
    global assistant
    if assistant is None:
        assistant = Assistant()
    return assistant


def _authorised() -> bool:
    expected = (os.environ.get("DEVICE_KEY") or "").strip()
    if not expected:
        return True  # no key configured; LAN-only deployment
    return request.headers.get("X-Device-Key", "") == expected


@app.get("/health")
def health() -> Response:
    current = _get_assistant()
    provider = current.conversation.provider
    return jsonify(
        {
            "ok": True,
            "whisper": current.transcriber.model_name,
            "provider": provider.name,
            "model": getattr(provider, "model", "?"),
            "voice": current.speaker.model_path.name if current.speaker else None,
            "tts": bool(current.speaker),
            "turns": len(current.conversation.history),
            "tools": [s.name for s in current.conversation.specs],
        }
    )


@app.post("/reset")
def reset() -> Response:
    """Clear conversation history.

    Needed for honest latency measurement: ask the same question twice and
    the second answer comes from history without a tool call, which looks
    like a speedup and is not.
    """
    current = _get_assistant()
    turns = len(current.conversation.history)
    current.conversation.reset()
    return jsonify({"ok": True, "cleared": turns})


@app.post("/text")
def talk_text() -> Response:
    if not _authorised():
        return jsonify({"error": "bad device key"}), 401
    payload = request.get_json(silent=True) or {}
    text = (payload.get("text") or "").strip()
    if not text:
        return jsonify({"error": "no text"}), 400

    started = time.perf_counter()
    reply = _get_assistant().reply_to_text(text)
    log.info("text turn %.2fs | %r -> %r", time.perf_counter() - started, text, reply)
    return jsonify({"heard": text, "reply": reply})


@app.post("/talk")
def talk_audio() -> Response:
    if not _authorised():
        return jsonify({"error": "bad device key"}), 401

    body = request.get_data(cache=False)
    if not body:
        return jsonify({"error": "empty body"}), 400
    if len(body) > MAX_BODY_BYTES:
        return jsonify({"error": "body too large"}), 413

    current = _get_assistant()
    timings: dict[str, float] = {}

    # -- decode ------------------------------------------------------------
    rate = int(request.headers.get("X-Sample-Rate", "16000"))
    wav_bytes = body if looks_like_wav(body) else pcm_to_wav(body, sample_rate=rate)
    wav_bytes = trim_leading_ms(wav_bytes)
    seconds = wav_duration(wav_bytes)

    # -- transcribe --------------------------------------------------------
    mark = time.perf_counter()
    heard = current.transcriber.transcribe(wav_bytes)
    timings["stt"] = time.perf_counter() - mark

    if heard and request.args.get("stream") == "1" and current.speaker is not None:
        return _stream_reply(current, heard, seconds, timings["stt"])

    if not heard:
        log.info("nothing transcribed from %.1fs of audio", seconds)
        return jsonify({"heard": "", "reply": "", "note": "no speech detected"}), 200

    # -- think -------------------------------------------------------------
    mark = time.perf_counter()
    reply = current.reply_to_text(heard)
    timings["llm"] = time.perf_counter() - mark

    # -- speak -------------------------------------------------------------
    if current.speaker is None:
        log.info(
            "%.1fs audio | stt %.2fs | llm %.2fs | %r -> %r (no TTS)",
            seconds, timings["stt"], timings["llm"], heard, reply,
        )
        return jsonify({"heard": heard, "reply": reply, "note": "TTS not configured"})

    mark = time.perf_counter()
    try:
        spoken = current.speaker.synthesize(reply)
    except RuntimeError as exc:
        log.error("TTS failed: %s", exc)
        return jsonify({"heard": heard, "reply": reply, "error": "tts failed"}), 500
    timings["tts"] = time.perf_counter() - mark

    total = sum(timings.values())
    log.info(
        "%.1fs audio | stt %.2f | llm %.2f | tts %.2f | total %.2fs",
        seconds, timings["stt"], timings["llm"], timings["tts"], total,
    )
    log.info("  heard: %s", heard)
    log.info("  said:  %s", reply)

    return Response(
        spoken,
        mimetype="audio/wav",
        headers={
            "X-Heard": heard[:500],
            "X-Reply": reply[:500],
            "X-Timing-Total": f"{total:.2f}",
        },
    )


def _stream_reply(current: "Assistant", heard: str, seconds: float, stt: float) -> Response:
    """Speak each sentence as soon as it exists.

    Without this, nothing is audible until the model has finished writing
    and Piper has finished synthesizing the whole reply. With it, the first
    sentence is on its way while the rest is still being generated — the
    total work is the same, the wait is not.

    Raw 16-bit PCM, chunked. A WAV header would need a length nobody knows
    yet; the client already knows the format from the headers below.
    """
    import queue
    import threading

    sentences: "queue.Queue[str | None]" = queue.Queue()
    outcome: dict[str, Any] = {}
    started = time.perf_counter()

    def think() -> None:
        try:
            outcome["reply"] = current.reply_to_text(heard, on_sentence=sentences.put)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = exc
            log.exception("conversation failed")
        finally:
            sentences.put(None)

    worker = threading.Thread(target=think, daemon=True)
    worker.start()

    def audio():
        first_audio_at: float | None = None
        spoken = 0

        # Straight out, before the model has even been called.
        ack = current.next_ack()
        if ack:
            first_audio_at = time.perf_counter() - started
            yield ack

        while True:
            sentence = sentences.get()
            if sentence is None:
                break
            try:
                pcm = current.speaker.synthesize_pcm(sentence)
            except RuntimeError as exc:
                log.error("TTS failed mid-stream: %s", exc)
                break
            if first_audio_at is None:
                first_audio_at = time.perf_counter() - started
            spoken += 1
            yield pcm

        worker.join(timeout=5)
        total = stt + (time.perf_counter() - started)
        log.info(
            "%.1fs audio | stt %.2f | first sound %.2fs%s | total %.2fs | %d chunk(s)",
            seconds, stt, (first_audio_at or 0.0) + stt,
            " (ack)" if ack else "", total, spoken,
        )
        log.info("  heard: %s", heard)
        log.info("  said:  %s", outcome.get("reply", ""))

    return Response(
        stream_with_context(audio()),
        mimetype="audio/L16",
        headers={
            "X-Heard": heard[:500],
            "X-Sample-Rate": str(current.speaker.sample_rate),
            "X-Channels": "1",
            "X-Bits": "16",
        },
    )


def main() -> None:
    if load_dotenv:
        load_dotenv(Path(__file__).resolve().parent / ".env")
    _get_assistant()  # load models before accepting requests
    port = int(os.environ.get("PORT", "8080"))
    log.info("listening on 0.0.0.0:%d", port)
    from waitress import serve

    serve(app, host="0.0.0.0", port=port, threads=4)


if __name__ == "__main__":
    main()
