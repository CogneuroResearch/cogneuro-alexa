"""Speech in, speech out — the two ends of the pipeline.

Whisper and Piper are used as libraries/processes on this machine rather
than as Wyoming services: the orchestrator is conversation.py, which can
import them directly, so a socket hop would buy nothing.

Both models are expensive to load (Whisper base.en takes ~12s on a Pi 5)
and cheap to reuse, so they load once at import of the server and are held
for the life of the process.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import subprocess
import tempfile
import wave
from pathlib import Path

log = logging.getLogger("speech")

DEFAULT_WHISPER_MODEL = "base.en"
TARGET_SAMPLE_RATE = 16000

# The codec on the BOX-3 emits a full-scale spike when it opens. It is in
# every capture and it is not speech.
CODEC_TRANSIENT_MS = 50


# --------------------------------------------------------------------------
# audio helpers
# --------------------------------------------------------------------------

def pcm_to_wav(pcm: bytes, sample_rate: int = TARGET_SAMPLE_RATE, channels: int = 1) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(channels)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


def looks_like_wav(data: bytes) -> bool:
    return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


def trim_leading_ms(wav_bytes: bytes, milliseconds: int = CODEC_TRANSIENT_MS) -> bytes:
    """Drop the first N ms — the codec-open transient."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            params = handle.getparams()
            frames = handle.readframes(handle.getnframes())
    except wave.Error:
        return wav_bytes

    skip = int(params.framerate * milliseconds / 1000) * params.sampwidth * params.nchannels
    if skip >= len(frames):
        return wav_bytes

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(params.nchannels)
        handle.setsampwidth(params.sampwidth)
        handle.setframerate(params.framerate)
        handle.writeframes(frames[skip:])
    return buffer.getvalue()


def wav_duration(wav_bytes: bytes) -> float:
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate())
    except wave.Error:
        return 0.0


# --------------------------------------------------------------------------
# speech to text
# --------------------------------------------------------------------------

class Transcriber:
    def __init__(
        self,
        model_name: str | None = None,
        compute_type: str = "int8",
        threads: int | None = None,
    ):
        from faster_whisper import WhisperModel

        self.model_name = model_name or os.environ.get("WHISPER_MODEL", DEFAULT_WHISPER_MODEL)
        log.info("loading whisper %s (this takes a few seconds)", self.model_name)
        self._model = WhisperModel(
            self.model_name,
            device="cpu",
            compute_type=compute_type,
            cpu_threads=threads or (os.cpu_count() or 4),
        )
        log.info("whisper ready")

    def transcribe(self, wav_bytes: bytes) -> str:
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=True) as handle:
            handle.write(wav_bytes)
            handle.flush()
            segments, _info = self._model.transcribe(
                handle.name,
                beam_size=1,
                language="en",
                vad_filter=True,          # drops the silence either side of the utterance
                vad_parameters={"min_silence_duration_ms": 300},
            )
            return " ".join(segment.text.strip() for segment in segments).strip()


# --------------------------------------------------------------------------
# text to speech
# --------------------------------------------------------------------------

class Speaker:
    """Piper, with the voice model loaded once.

    The obvious implementation shells out to the `piper` binary per call.
    That works, but every call reloads the voice — which is invisible until
    you start streaming clause by clause, at which point a two-chunk reply
    pays the load twice and total time goes UP. Measured on a Pi 5: 7.4s to
    9.6s.

    So: load the voice through the Python API once and keep it. The CLI
    remains as a fallback, because Piper's Python API has moved between
    releases while its command line has not.

    Piper voices are 22.05kHz; output is resampled to 16kHz mono to match
    what the board records and plays.
    """

    def __init__(
        self,
        model_path: str | None = None,
        binary: str | None = None,
        sample_rate: int = TARGET_SAMPLE_RATE,
        length_scale: float | None = None,
        speaker: int | None = None,
    ):
        self.binary = binary or os.environ.get("PIPER_BIN", "piper")
        model = model_path or os.environ.get("PIPER_MODEL", "")
        if not model:
            raise RuntimeError("PIPER_MODEL is not set — point it at a .onnx voice")
        self.model_path = Path(model).expanduser()
        if not self.model_path.exists():
            raise RuntimeError(f"Piper voice not found: {self.model_path}")
        if shutil.which(self.binary) is None:
            raise RuntimeError(f"Piper binary not on PATH: {self.binary}")
        self.sample_rate = sample_rate

        # Delivery controls. length_scale is duration: >1 slower, <1 faster.
        # speaker selects a voice within a multi-speaker model (libritts has
        # over 900); it is ignored by single-speaker models.
        env_length = os.environ.get("PIPER_LENGTH_SCALE", "").strip()
        self.length_scale = length_scale if length_scale is not None else (
            float(env_length) if env_length else None
        )
        env_speaker = os.environ.get("PIPER_SPEAKER", "").strip()
        self.speaker = speaker if speaker is not None else (
            int(env_speaker) if env_speaker else None
        )

        self._ffmpeg = shutil.which("ffmpeg")
        if not self._ffmpeg:
            log.warning("ffmpeg not found — sending Piper's native rate, board must cope")

        self._voice = None
        self._native_rate: int | None = None
        if os.environ.get("PIPER_USE_CLI", "0") != "1":
            self._load_voice()

    def _load_voice(self) -> None:
        """Load the voice once, if this Piper build exposes a usable API."""
        try:
            from piper import PiperVoice
        except ImportError:
            log.info("piper Python API unavailable — using the CLI per call")
            return
        try:
            self._voice = PiperVoice.load(str(self.model_path))
            config = getattr(self._voice, "config", None)
            self._native_rate = getattr(config, "sample_rate", None)
            log.info(
                "piper voice loaded in-process (%s Hz native)", self._native_rate or "?"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("could not load piper voice in-process (%s) — using CLI", exc)
            self._voice = None

    def _synth_kwargs(self) -> dict:
        kwargs = {}
        if self.length_scale is not None:
            kwargs["length_scale"] = self.length_scale
        if self.speaker is not None:
            kwargs["speaker_id"] = self.speaker
        return kwargs

    @staticmethod
    def _chunk_bytes(obj) -> bytes | None:
        """Get raw PCM out of whatever Piper handed back.

        Across releases this has been: raw bytes; a generator of bytes; and
        a generator of AudioChunk objects carrying the samples on an
        attribute. Rather than pin a version, accept all three.
        """
        if isinstance(obj, (bytes, bytearray)):
            return bytes(obj)
        for attr in ("audio_int16_bytes", "audio_bytes", "audio_int16_array", "audio"):
            value = getattr(obj, attr, None)
            if isinstance(value, (bytes, bytearray)):
                return bytes(value)
            if value is not None and hasattr(value, "tobytes"):
                return value.tobytes()
        return None

    def _pcm_in_process(self, text: str) -> bytes | None:
        """Raw PCM at the voice's native rate, or None if the API differs."""
        if self._voice is None:
            return None

        for attr in ("synthesize", "synthesize_stream_raw", "synthesize_raw"):
            method = getattr(self._voice, attr, None)
            if method is None:
                continue

            try:
                result = method(text, **self._synth_kwargs())
            except TypeError:
                try:
                    result = method(text)
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue

            direct = self._chunk_bytes(result)
            if direct:
                return direct

            try:
                parts = []
                for chunk in result:
                    raw = self._chunk_bytes(chunk)
                    if raw is None:
                        parts = []
                        break
                    parts.append(raw)
                    rate = getattr(chunk, "sample_rate", None)
                    if rate:
                        self._native_rate = rate
            except TypeError:
                continue

            if parts:
                return b"".join(parts)

        log.warning(
            "piper Python API shape not recognised — falling back to CLI. "
            "Run tools/inspect_piper.py and send me the output."
        )
        self._voice = None
        return None

    def _resample_pcm(self, pcm: bytes, from_rate: int) -> bytes:
        if not self._ffmpeg or from_rate == self.sample_rate:
            return pcm
        result = subprocess.run(
            [
                self._ffmpeg, "-hide_banner", "-loglevel", "error",
                "-f", "s16le", "-ar", str(from_rate), "-ac", "1", "-i", "pipe:0",
                "-f", "s16le", "-ar", str(self.sample_rate), "-ac", "1", "pipe:1",
            ],
            input=pcm, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60,
        )
        return result.stdout if result.returncode == 0 else pcm

    def synthesize_pcm(self, text: str) -> bytes:
        """Synthesize and return raw 16-bit PCM at self.sample_rate.

        Streaming a chunk at a time means the client starts playing while
        later chunks are still being synthesized, so there is no place for a
        WAV header with a length in it.
        """
        pcm = self._pcm_in_process(text)
        if pcm is not None:
            return self._resample_pcm(pcm, self._native_rate or self.sample_rate)

        wav_bytes = self.synthesize(text)
        try:
            with wave.open(io.BytesIO(wav_bytes), "rb") as handle:
                return handle.readframes(handle.getnframes())
        except wave.Error:
            return wav_bytes

    def synthesize(self, text: str) -> bytes:
        command = [self.binary, "--model", str(self.model_path), "--output_file", "-"]
        if self.length_scale is not None:
            command += ["--length_scale", str(self.length_scale)]
        if self.speaker is not None:
            command += ["--speaker", str(self.speaker)]

        result = subprocess.run(
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(f"piper failed: {result.stderr.decode('utf-8', 'replace')[:300]}")
        wav_bytes = result.stdout
        return self._resample(wav_bytes) if self._ffmpeg else wav_bytes

    def _resample(self, wav_bytes: bytes) -> bytes:
        result = subprocess.run(
            [
                self._ffmpeg, "-hide_banner", "-loglevel", "error",
                "-i", "pipe:0",
                "-ar", str(self.sample_rate), "-ac", "1", "-f", "wav", "pipe:1",
            ],
            input=wav_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        if result.returncode != 0:
            log.warning("resample failed, passing Piper output through")
            return wav_bytes
        return result.stdout
