import { list, get } from '@vercel/blob';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

// GET /api/captures -> newest-first list of captures with their metadata
export async function GET() {
  let blobs;
  try {
    ({ blobs } = await list({ prefix: 'captures/', limit: 400 }));
  } catch (err) {
    return Response.json(
      { ok: false, error: `blob store unreachable: ${err.message}`, captures: [] },
      { status: 500 }
    );
  }

  const sidecars = blobs
    .filter((b) => b.pathname.endsWith('.json'))
    .sort((a, b) => new Date(b.uploadedAt) - new Date(a.uploadedAt))
    .slice(0, 100);

  const captures = await Promise.all(
    sidecars.map(async (b) => {
      try {
        const res = await get(b.pathname, { access: 'private' });
        if (!res?.stream) return null;
        const meta = JSON.parse(await new Response(res.stream).text());
        return { ...meta, audioUrl: `/api/audio?id=${encodeURIComponent(meta.id)}` };
      } catch {
        return null;
      }
    })
  );

  return Response.json({
    ok: true,
    captures: captures.filter(Boolean),
  });
}
