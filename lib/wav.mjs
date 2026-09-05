// Minimal WAV helpers. The ESP32 may post either a complete WAV file or raw
// PCM; both end up stored as a playable WAV so the browser can play it back.

export function parseWav(buf) {
  if (buf.length < 44) throw new Error('too short to be a WAV file');
  if (buf.toString('ascii', 0, 4) !== 'RIFF' || buf.toString('ascii', 8, 12) !== 'WAVE') {
    throw new Error('missing RIFF/WAVE header');
  }

  let offset = 12;
  let fmt = null;
  let dataBytes = null;

  while (offset + 8 <= buf.length) {
    const id = buf.toString('ascii', offset, offset + 4);
    const size = buf.readUInt32LE(offset + 4);
    const body = offset + 8;

    if (id === 'fmt ') {
      fmt = {
        audioFormat: buf.readUInt16LE(body),
        channels: buf.readUInt16LE(body + 2),
        sampleRate: buf.readUInt32LE(body + 4),
        bitsPerSample: buf.readUInt16LE(body + 14),
      };
    } else if (id === 'data') {
      // Some streaming writers leave the size field at 0 or 0xFFFFFFFF because
      // they don't know the length up front. Fall back to what actually arrived.
      const remaining = buf.length - body;
      dataBytes = size > 0 && size <= remaining ? size : remaining;
      break;
    }
    offset = body + size + (size % 2);
  }

  if (!fmt) throw new Error('no fmt chunk');
  if (dataBytes === null) throw new Error('no data chunk');
  return { ...fmt, dataBytes };
}

export function pcmToWav(pcm, { sampleRate = 16000, channels = 1, bitsPerSample = 16 } = {}) {
  const byteRate = (sampleRate * channels * bitsPerSample) / 8;
  const blockAlign = (channels * bitsPerSample) / 8;
  const header = Buffer.alloc(44);

  header.write('RIFF', 0, 'ascii');
  header.writeUInt32LE(36 + pcm.length, 4);
  header.write('WAVE', 8, 'ascii');
  header.write('fmt ', 12, 'ascii');
  header.writeUInt32LE(16, 16);
  header.writeUInt16LE(1, 20); // PCM
  header.writeUInt16LE(channels, 22);
  header.writeUInt32LE(sampleRate, 24);
  header.writeUInt32LE(byteRate, 28);
  header.writeUInt16LE(blockAlign, 32);
  header.writeUInt16LE(bitsPerSample, 34);
  header.write('data', 36, 'ascii');
  header.writeUInt32LE(pcm.length, 40);

  return Buffer.concat([header, pcm]);
}

export function durationSeconds({ dataBytes, sampleRate, channels, bitsPerSample }) {
  const bytesPerSecond = (sampleRate * channels * bitsPerSample) / 8;
  if (!bytesPerSecond) return 0;
  return dataBytes / bytesPerSecond;
}

// Peak amplitude per bucket, 0..1. Cheap enough to do on ingest, and it makes
// clipping and dead air visible on the page without loading the audio.
export function peaks(pcm, { bucketCount = 96, bitsPerSample = 16 } = {}) {
  if (bitsPerSample !== 16 || pcm.length < 2) return [];
  const sampleCount = Math.floor(pcm.length / 2);
  const perBucket = Math.max(1, Math.floor(sampleCount / bucketCount));
  const out = [];

  for (let b = 0; b < bucketCount; b++) {
    const start = b * perBucket;
    if (start >= sampleCount) break;
    const end = Math.min(start + perBucket, sampleCount);
    let peak = 0;
    for (let i = start; i < end; i++) {
      const v = Math.abs(pcm.readInt16LE(i * 2));
      if (v > peak) peak = v;
    }
    out.push(Number((peak / 32768).toFixed(3)));
  }
  return out;
}
