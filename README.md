# cogneuro-alexa

A self-hosted voice assistant that supports real spoken dialog with an LLM,
built to discuss and update a Linear to-do list over breakfast. Not a
command-and-response box: it keeps conversation history across turns and
calls tools to do real work.

## What is in this repo

| Path | What it is | Runs on |
|---|---|---|
| `brain/` | Conversation loop, tools, and the speech server | Raspberry Pi 5 |
| `firmware/box3-capture/` | ESP-IDF firmware for the ESP32-S3-BOX-3 | the board |
| `app/`, `lib/`, `scripts/` | Capture review page (Next.js, documented below) | Vercel |

**Start at [`brain/README.md`](brain/README.md)** — it covers the running
system, how to set the Pi up, the measurements behind the design choices,
and the traps.

## How it fits together

```
ESP32-S3-BOX-3                     Raspberry Pi 5 (one process)
  button (wake word later)  --HTTP-->  Whisper base.en
  records audio                        conversation loop + tool calling
  plays the reply           <--PCM---  Piper
                                          |
                                       LLM API (provider-abstracted)
                                       Linear GraphQL, Open-Meteo
```

**Wyoming is deliberately not used.** It exists so Home Assistant can talk to
pluggable speech services; this project has its own orchestrator, which
imports faster-whisper and Piper directly. Likewise ESPHome was rejected for
the board: its voice-assistant component targets Home Assistant, so adopting
it would mean running HA purely as a transport.

## Status

Working: capture firmware, mic calibration, the conversation loop with tool
calling, and the full audio pipeline on the Pi (speech in, speech out).

Next: point the firmware at the Pi and add playback, then endpointing (VAD)
so a turn ends when you stop talking, then the on-device wake word. The
codec-ownership bug in the firmware should be fixed *before* playback is
added, since playback introduces a second owner of the codec handle.

---

## Capture review page

A deliberately small Next.js app whose only job is to answer one question:
**what did the ESP32-S3-BOX-3 actually record?** It is now a debugging tool
rather than part of the pipeline.

> ⚠️ **The `peak` metric is misleading.** Every capture opens with a
> full-scale codec transient in the first ~50 ms, so `peak` reports that
> spike rather than the speech — clean table-distance speech reads
> `peak 100% clipping`. Since it is pinned at full scale regardless of gain,
> it cannot be used to compare gain settings. Fix: compute peak and the
> clipping flag excluding the first 50 ms. (`brain/speech.py` already trims
> that window before transcribing.)

> ⚠️ `GET /api/captures` appears to be cached at the edge — a freshly posted
> clip can be missing from the API response while showing correctly on the
> page after a hard refresh. Trust the page, not the API.

The board POSTs a clip after a voice trigger; this page lists the clips newest
first with a waveform, a player, and the numbers that matter when a voice
pipeline misbehaves — sample rate, duration, peak level, WiFi RSSI. Nothing
transcribes or interprets the audio. That comes later, once the input is
trustworthy.

## Wire format

```
POST /api/capture?device=box3&trigger=wake&sr=16000&rssi=-52&fw=0.1.0
X-Device-Key: <DEVICE_KEY>
Content-Type: audio/wav          # a complete WAV file
             audio/l16           # or raw signed 16-bit little-endian PCM
<body: audio bytes>
```

Raw PCM is wrapped in a WAV header server-side using `sr` / `ch` / `bits`
(defaults: 16000 / 1 / 16), so the firmware never has to build a header.

| Param     | Meaning                                    |
|-----------|--------------------------------------------|
| `device`  | free-form device name                       |
| `trigger` | what started the recording (`wake`, `button`) |
| `sr`      | sample rate, PCM uploads only               |
| `ch`      | channels, PCM uploads only                  |
| `bits`    | bits per sample, PCM uploads only           |
| `rssi`    | WiFi RSSI at capture time                   |
| `fw`      | firmware version                            |
| `note`    | free text shown under the clip              |

Keep clips under ~4MB — Vercel functions cap request bodies at 4.5MB. At
16kHz mono that's about two minutes, far more than a command needs.

Responses are JSON: `{ ok: true, id, durationSec, ... }`, or `{ ok: false, error }`
with 401 (bad key), 400 (unreadable audio), 413 (too big), 503 (`DEVICE_KEY` unset).

## Endpoints

| Route | Purpose |
|---|---|
| `POST /api/capture` | ingest, device-key auth |
| `GET /api/captures` | newest-first list with metadata |
| `GET /api/audio?id=` | streams a private WAV through the app |
| `DELETE /api/capture?id=` | remove a clip and its metadata |

## Setup

1. `npm install`
2. Create a Vercel project and link it: `npx vercel link`
3. Create a **private** Blob store in the Vercel dashboard (Storage → Blob) and
   connect it to this project. That sets `BLOB_READ_WRITE_TOKEN` automatically.
4. Set a device key — this is what stops anyone from posting audio to your page:
   ```
   openssl rand -hex 16
   npx vercel env add DEVICE_KEY
   ```
5. Optional but recommended, since these are recordings of your kitchen:
   ```
   npx vercel env add VIEW_PASSWORD
   ```
   Any username plus that password gets you into the page; ingest is unaffected.
6. `npx vercel env pull .env.local` then `npm run dev` for local work.
7. Deploy: `npx vercel --prod`

## Verifying before the board is flashed

```
npm run post-test -- https://your-app.vercel.app
```

Sends a 2.5s synthetic clip (room tone, rising chirp, quiet tail). If it shows
up on the page with a waveform you can play, the ingest path is proven and
anything that goes wrong afterwards is the firmware's fault. Pass a WAV file
path as a second argument to upload a real recording instead.

## Storage

Audio and a JSON sidecar per capture live in Vercel Blob under `captures/`, in a
private store — clips are served only through `/api/audio`, never from a public
blob URL. Delete clips from the page as you go; this is a test bench, not an
archive.
