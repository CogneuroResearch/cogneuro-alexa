# brain — the assistant

Speech in, speech out. Whisper, a tool-calling conversation loop, and Piper,
all in one process on the Raspberry Pi 5.

It can also be run as a plain terminal chat with no audio at all, which is
how the tool calling was developed and is still the fastest way to debug it.

## Files

```
server.py          the Pi: HTTP audio in, streamed speech out
chat.py            terminal REPL — the assistant without the voice
conversation.py    history, tool-call round trip, chunk streaming
speech.py          Whisper (faster-whisper) and Piper wrappers
tools.py           tool schemas and implementations
linear_client.py   thin Linear GraphQL client
llm/
  base.py          the provider interface — one streaming method
  __init__.py      registry; LLM_PROVIDER picks the adapter
  anthropic_provider.py
  openai_provider.py

setup_pi.sh        one-shot Pi provisioning (idempotent)
install_service.sh install as a systemd unit so it survives a closed terminal
verify.py          pre-flight checks, one layer at a time
smoke_test.py      16 offline tests — no keys, no network
bench_whisper.py   benchmark Whisper models on real captured audio
inspect_piper.py   report this Piper build's Python API shape
```

## Which machine runs what

- **Mac** — edit code here; the repo lives at `~/Documents/cogneuro-alexa`.
  Also flashes the board and runs `chat.py` for quick iteration.
- **Pi 5** (`cogneuro-pi.local`, user `john`, key-only SSH) — runs
  `server.py`. Code is deployed by `scp`; venv at `~/alexa-venv`; Piper
  voices in `~/piper`.
- **ESP32-S3-BOX-3** — records and (soon) plays.

## Running it on the Pi

```bash
# from the Mac
scp -r ~/Documents/cogneuro-alexa/brain john@cogneuro-pi.local:~/

# on the Pi, once
~/brain/setup_pi.sh          # apt packages, venv, deps, Piper voice
nano ~/brain/.env            # ANTHROPIC_API_KEY, optionally LINEAR_API_KEY

# every time
source ~/alexa-venv/bin/activate && cd ~/brain && python server.py
```

Startup takes ~15s the first time (Whisper loads), ~3s after. You want to
see `whisper ready`, `piper voice loaded in-process`, `acknowledgements
ready`, then `listening on 0.0.0.0:8080`.

### Run it as a service

Running it by hand means it dies with the terminal, which is a poor property
for something in a kitchen.

```bash
~/brain/install_service.sh
```

Then `journalctl -u alexa -f` to watch it, and
`sudo systemctl restart alexa` after deploying new code. It starts at boot
and restarts on failure, with a burst limit so a crash loop cannot hammer
the LLM API.

> **The server window is not a shell.** Typing `python server.py` into a
> running server just echoes it. Press `Ctrl-C`, wait for the prompt, *then*
> restart — and confirm `loading whisper base.en` scrolls past. Three rounds
> of latency measurement were once wasted on a process that had never
> restarted and was quietly serving old code. Same shape as the
> `idf.py monitor` port-busy trap in the firmware.

## Endpoints

| Route | Purpose |
|---|---|
| `POST /talk` | WAV or raw PCM in, complete `audio/wav` reply out |
| `POST /talk?stream=1` | chunked raw 16-bit PCM @16kHz — **what the firmware should use** |
| `POST /text` | `{"text": "..."}` in, JSON out; no audio, for testing |
| `POST /reset` | clear conversation history |
| `GET /health` | live model, voice, tools, turn count |

Streaming responses carry `X-Heard`, `X-Sample-Rate`, `X-Channels`, `X-Bits`.

## Testing without hardware

Piper can speak a question and feed it straight back in — the whole pipeline
from one command:

```bash
echo "what is the weather in ottawa tomorrow" \
  | piper --model ~/piper/en_US-lessac-medium.onnx --output_file /tmp/question.wav

curl -s -X POST http://localhost:8080/reset
curl -s -D - -X POST --data-binary @/tmp/question.wav \
  -H 'Content-Type: audio/wav' \
  'http://localhost:8080/talk?stream=1' -o /tmp/reply.pcm

ffmpeg -y -f s16le -ar 16000 -ac 1 -i /tmp/reply.pcm /tmp/reply.wav
```

**Reset before every timing run.** History persists, so asking the same
question twice means the second answer comes from history with no tool call
— which looks like a speedup and is not.

## Design

**The provider is swappable.** Everything above `llm/base.py` speaks one
neutral history format; each adapter translates to its own wire shape.
Adding Gemini or a local model is one file plus one line in
`llm/__init__.py` — no changes to the loop, the tools, or the prompt.

**There is no intent parser.** Text plus tool schemas go to the model and it
decides that "bump that up" means `set_priority`. This is the whole reason
for not using Home Assistant's Assist pipeline, where every new phrasing is
a config problem.

**Adding a capability is one function**, a `ToolSpec`, and a line in
`Toolbox.HANDLERS`. `get_weather` (Open-Meteo, no API key) is there partly
to demonstrate that.

**Tools are withheld when unconfigured.** With no `LINEAR_API_KEY` the
Linear tools are not offered at all — advertising a tool guaranteed to fail
only teaches the model to apologise. Verified: it says plainly that it
cannot see a to-do list rather than inventing one.

**Issues are addressed by identifier, never UUID.** `ENG-142` is something a
person can say out loud. `linear_client` resolves identifiers internally.

**Tool errors are returned to the model, not raised**, so the assistant can
say what went wrong instead of the process dying mid-sentence.

## Speech, measured

### Whisper model choice (Pi 5, int8, real captured audio)

| Model | Realtime factor | Result |
|---|---|---|
| `tiny.en` | 0.15–0.27x | dropped a word on one clip |
| **`base.en`** | **0.32–0.33x** | **correct every time — chosen** |
| `small.en` | 0.87–0.93x | three times slower and *no better* |

Re-run with `bench_whisper.py --url https://cogneuro-alexa.vercel.app`.

**Whisper pads internally to a 30-second window, so STT cost is roughly
constant (~1.75s) regardless of utterance length.** Short questions are not
faster. That is the floor for `base.en` on this CPU.

### Latency: 8.94s → 1.76s to first sound

Final: `stt 1.75 | first sound 1.76s (ack) | total 5.49s`.

What got it there, and two traps that made things *worse* first:

1. **Sentence-level chunking is useless for a terse assistant.** The system
   prompt asks for one-sentence answers, so a streamer breaking only at `.`
   emits nothing until the end — `first sound` equalled `total` exactly.
   `SentenceStreamer` therefore also breaks at clause boundaries (commas,
   semicolons) once a chunk exceeds `MIN_CLAUSE_CHARS`. Subtle bug to avoid:
   take the first clause break *past* the minimum length, not the first one
   found, or an early short clause aborts the search.

2. **Piper reloads the voice on every CLI invocation.** Chunked streaming
   paid that load once per chunk and total *rose* from 7.4s to 9.6s. The
   voice is now loaded in-process once. Piper's Python API has changed shape
   across releases (raw bytes / generator of bytes / generator of
   `AudioChunk`), so `speech.py` accepts all three and keeps the CLI as a
   fallback. If the log says `piper Python API shape not recognised`, run
   `inspect_piper.py` and extend `_chunk_bytes`.

Also: Haiku over Sonnet, a terser prompt, cached geocoding (1.06s → 0.17s),
and prompt caching on the system + tool-schema prefix.

**The acknowledgement.** A pre-synthesized "Okay." / "Sure." / "Right.",
rotated, is emitted the instant Whisper finishes. It makes nothing faster —
it fills the silence the LLM round trips create, which is how commercial
assistants hide the same gap. Synthesized at startup, because doing it per
turn would cost exactly the time it exists to hide. `ACK_PHRASES=` disables.

**Tool-backed questions cost double**: two LLM round trips, one to decide
the tool is needed and one to answer with its result.

## Voice

Change `PIPER_MODEL` to any voice from
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices).
`medium` is the sweet spot on a Pi 5; `high` sounds better but costs real
CPU per sentence. `PIPER_SPEAKER` selects within multi-speaker models,
`PIPER_LENGTH_SCALE` sets pace (>1 slower; 1.05–1.15 often reads better at a
distance). Personality is the system prompt in `conversation.py`, not TTS.

A custom or cloned voice is out of reach locally — training Piper needs a
dataset and a GPU, and zero-shot cloning cannot run in real time on a Pi
CPU. A hosted TTS API is the only realistic route, at the cost of the
self-hosted premise.

## Configuration

See `.env.example`; every variable is documented there. The essentials are
`LLM_PROVIDER`, the matching API key, `PIPER_MODEL`, and optionally
`LINEAR_API_KEY`.

An **empty** value counts as unset and fails loudly — an earlier version
treated `ANTHROPIC_API_KEY=` as configured and failed confusingly later.

## Next

1. **Firmware: playback and the new endpoint.** Point the board at
   `http://cogneuro-pi.local:8080/talk?stream=1` and play the returned
   16kHz mono PCM through the ES8311. **Fix the codec teardown first** —
   see the firmware README; playback adds a second owner of a codec handle
   whose teardown is already asymmetric.
2. **Endpointing (VAD)** so a turn ends when you stop talking rather than at
   a fixed 5-second window. That, plus keeping the mic open briefly after a
   reply so follow-ups need no trigger, is what makes it feel conversational
   — more than raw speed does.
3. **Enable Linear** — add `LINEAR_API_KEY` and the tools appear.
4. **Speaker recognition** to tell one person from another: enrol 30–60s per
   voice, embed, compare by cosine similarity with an "unknown" bucket.
   `Resemblyzer` is light enough for the Pi; SpeechBrain ECAPA is more
   accurate but drags in PyTorch. Not a novelty — the assistant writes to
   Linear, so without it anyone at the table can reprioritise John's issues.
5. **On-device wake word** (esp-sr), after the codec fix.
