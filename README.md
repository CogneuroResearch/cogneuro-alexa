# cogneuro-alexa — capture review

A deliberately small Next.js app whose only job is to answer one question:
**what did the ESP32-S3-BOX-3 actually record?**

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
