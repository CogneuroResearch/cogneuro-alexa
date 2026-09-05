import { NextResponse } from 'next/server';

// If VIEW_PASSWORD is set, everything except the ingest endpoint sits behind
// HTTP Basic auth (any username). Recordings of your kitchen shouldn't be a
// public URL away from anyone who guesses the deployment name.
export function proxy(request) {
  const password = process.env.VIEW_PASSWORD;
  if (!password) return NextResponse.next();

  const header = request.headers.get('authorization') || '';
  if (header.startsWith('Basic ')) {
    const decoded = atob(header.slice(6));
    const supplied = decoded.slice(decoded.indexOf(':') + 1);
    if (supplied === password) return NextResponse.next();
  }

  return new NextResponse('Authentication required', {
    status: 401,
    headers: { 'WWW-Authenticate': 'Basic realm="capture review"' },
  });
}

export const config = {
  matcher: ['/((?!api/capture|_next/static|_next/image|favicon.ico).*)'],
};
