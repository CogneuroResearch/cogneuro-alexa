#!/usr/bin/env node
// Posts a capture to the review page so you can verify the whole path before
// the ESP32 is flashed.
//
//   npm run post-test -- https://your-app.vercel.app            (synthetic tone)
//   npm run post-test -- https://your-app.vercel.app clip.wav   (a real WAV file)
//
// DEVICE_KEY is read from the environment or from .env.local.

import { readFile } from 'node:fs/promises';
import { pcmToWav } from '../lib/wav.mjs';

const [baseArg, fileArg] = process.argv.slice(2);
const base = (baseArg || 'http://localhost:3000').replace(/\/$/, '');

let key = process.env.DEVICE_KEY;
if (!key) {
  try {
    const env = await readFile(new URL('../.env.local', import.meta.url), 'utf8');
    key = env.match(/^DEVICE_KEY=(.*)$/m)?.[1]?.trim();
  } catch {}
}
if (!key) {
  console.error('DEVICE_KEY not set. Export it, or put it in .env.local.');
  process.exit(1);
}

function syntheticWav() {
  // 2.5s at 16kHz: a moment of room tone, a rising chirp, then quiet again —
  // enough shape to tell at a glance that the waveform and player are honest.
  const sampleRate = 16000;
  const total = Math.floor(sampleRate * 2.5);
  const pcm = Buffer.alloc(total * 2);

  for (let i = 0; i < total; i++) {
    const t = i / sampleRate;
    let v = (Math.random() - 0.5) * 400; // room tone
    if (t > 0.5 && t < 1.9) {
      const freq = 240 + (t - 0.5) * 300;
      const env = Math.min(1, (t - 0.5) * 6) * Math.min(1, (1.9 - t) * 6);
      v += Math.sin(2 * Math.PI * freq * t) * 9000 * env;
    }
    pcm.writeInt16LE(Math.max(-32768, Math.min(32767, Math.round(v))), i * 2);
  }
  return pcmToWav(pcm, { sampleRate });
}

const wav = fileArg ? await readFile(fileArg) : syntheticWav();
const params = new URLSearchParams({
  device: fileArg ? 'mac-file' : 'mac-test',
  trigger: 'manual',
  fw: 'post-test',
  note: fileArg ? `uploaded from ${fileArg}` : 'synthetic tone from post-test-capture.mjs',
});

const res = await fetch(`${base}/api/capture?${params}`, {
  method: 'POST',
  headers: { 'content-type': 'audio/wav', 'x-device-key': key },
  body: wav,
});

const text = await res.text();
console.log(res.status, text);
process.exit(res.ok ? 0 : 1);
