import { get } from '@vercel/blob';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// GET /api/audio?id=<capture id> -> streams the private WAV through the app,
// so recordings are never exposed at a public blob URL.
export async function GET(request) {
  const id = new URL(request.url).searchParams.get('id');
  if (!id || !/^[\w.-]+$/.test(id)) {
    return new Response('missing or invalid id', { status: 400 });
  }

  const blob = await get(`captures/${id}.wav`, { access: 'private' });
  if (!blob?.stream) return new Response('not found', { status: 404 });

  return new Response(blob.stream, {
    headers: {
      'content-type': 'audio/wav',
      'cache-control': 'private, max-age=3600',
      'content-disposition': `inline; filename="${id}.wav"`,
    },
  });
}
