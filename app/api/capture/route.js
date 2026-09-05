import { put, del } from '@vercel/blob';
import { parseWav, pcmToWav, durationSeconds, peaks } from '../../../lib/wav.mjs';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const MAX_BYTES = 4 * 1024 * 1024; // Vercel functions cap request bodies at ~4.5MB

function unauthorized(msg) {
  return Response.json({ ok: false, error: msg }, { status: 401 });
}

// POST /api/capture?device=box3&sr=16000&trigger=wake&rssi=-52&fw=0.1.0
// Body: a complete WAV file (Content-Type: audio/wav)
//       or raw signed 16-bit little-endian PCM (Content-Type: audio/l16)
export async function POST(request) {
  const expected = process.env.DEVICE_KEY;
  if (!expected) {
    return Response.json(
      { ok: false, error: 'DEVICE_KEY is not set on the server. Ingest is disabled.' },
      { status: 503 }
    );
  }
  if (request.headers.get('x-device-key') !== expected) {
    return unauthorized('bad or missing X-Device-Key header');
  }

  const url = new URL(request.url);
  const q = url.searchParams;
  const contentType = (request.headers.get('content-type') || '').toLowerCase();

  const body = Buffer.from(await request.arrayBuffer());
  if (body.length === 0) return Response.json({ ok: false, error: 'empty body' }, { status: 400 });
  if (body.length > MAX_BYTES) {
    return Response.json(
      { ok: false, error: `body is ${body.length} bytes, limit is ${MAX_BYTES}` },
      { status: 413 }
    );
  }

  const isWav = contentType.includes('wav') || body.toString('ascii', 0, 4) === 'RIFF';

  let wav;
  let format;
  try {
    if (isWav) {
      wav = body;
      format = parseWav(body);
    } else {
      format = {
        sampleRate: Number(q.get('sr') || 16000),
        channels: Number(q.get('ch') || 1),
        bitsPerSample: Number(q.get('bits') || 16),
        audioFormat: 1,
        dataBytes: body.length,
      };
      wav = pcmToWav(body, format);
    }
  } catch (err) {
    return Response.json({ ok: false, error: `could not read audio: ${err.message}` }, { status: 400 });
  }

  const receivedAt = new Date();
  const id = `${receivedAt.toISOString().replace(/[:.]/g, '-')}-${Math.random().toString(36).slice(2, 8)}`;

  const meta = {
    id,
    receivedAt: receivedAt.toISOString(),
    device: q.get('device') || 'unknown',
    trigger: q.get('trigger') || null,
    firmware: q.get('fw') || null,
    rssi: q.get('rssi') ? Number(q.get('rssi')) : null,
    note: q.get('note') || null,
    sampleRate: format.sampleRate,
    channels: format.channels,
    bitsPerSample: format.bitsPerSample,
    audioBytes: format.dataBytes,
    totalBytes: wav.length,
    durationSec: Number(durationSeconds(format).toFixed(3)),
    postedAsWav: isWav,
    peaks: peaks(wav.subarray(wav.length - format.dataBytes), { bitsPerSample: format.bitsPerSample }),
  };

  let audio;
  try {
    audio = await put(`captures/${id}.wav`, wav, {
      access: 'private',
      contentType: 'audio/wav',
    });
    await put(`captures/${id}.json`, JSON.stringify(meta), {
      access: 'private',
      contentType: 'application/json',
    });
  } catch (err) {
    // Almost always a missing or wrong BLOB_READ_WRITE_TOKEN, or no Blob store
    // connected to the project.
    return Response.json({ ok: false, error: `could not store capture: ${err.message}` }, { status: 502 });
  }

  return Response.json({ ok: true, ...meta, pathname: audio.pathname });
}

// DELETE /api/capture?id=<capture id>
export async function DELETE(request) {
  const id = new URL(request.url).searchParams.get('id');
  if (!id || !/^[\w.-]+$/.test(id)) {
    return Response.json({ ok: false, error: 'missing or invalid id' }, { status: 400 });
  }
  await del([`captures/${id}.wav`, `captures/${id}.json`]);
  return Response.json({ ok: true, id });
}
