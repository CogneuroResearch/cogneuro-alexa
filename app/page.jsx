'use client';

import { useCallback, useEffect, useState } from 'react';

function ago(iso) {
  const secs = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return `${Math.round(secs)}s ago`;
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`;
  return `${Math.round(secs / 86400)}d ago`;
}

function Waveform({ peaks }) {
  if (!peaks?.length) return null;
  const width = 800;
  const height = 56;
  const mid = height / 2;
  const barWidth = width / peaks.length;

  return (
    <svg className="wave" viewBox={`0 0 ${width} ${height}`} preserveAspectRatio="none" aria-hidden="true">
      <line x1="0" y1={mid} x2={width} y2={mid} stroke="var(--border)" strokeWidth="1" />
      {peaks.map((p, i) => {
        const h = Math.max(1.5, p * (height - 6));
        const clipped = p > 0.98;
        return (
          <rect
            key={i}
            x={i * barWidth + barWidth * 0.15}
            y={mid - h / 2}
            width={barWidth * 0.7}
            height={h}
            rx={Math.min(1.5, barWidth * 0.35)}
            fill={clipped ? '#c0392b' : 'var(--wave)'}
          />
        );
      })}
    </svg>
  );
}

function Capture({ capture, onDelete }) {
  const peak = capture.peaks?.length ? Math.max(...capture.peaks) : null;
  const clipping = peak !== null && peak > 0.98;
  const quiet = peak !== null && peak < 0.05;

  return (
    <article className="card">
      <div className="card-top">
        <div>
          <span className="when">{new Date(capture.receivedAt).toLocaleTimeString()}</span>
          <span className="ago">{ago(capture.receivedAt)}</span>
        </div>
        <span className="tag">{capture.device}</span>
      </div>

      <Waveform peaks={capture.peaks} />
      <audio controls preload="none" src={capture.audioUrl} />

      <div className="tags">
        <span className="tag"><b>{capture.durationSec.toFixed(2)}</b>s</span>
        <span className="tag"><b>{(capture.sampleRate / 1000).toFixed(1)}</b>kHz</span>
        <span className="tag"><b>{capture.channels}</b>ch · <b>{capture.bitsPerSample}</b>bit</span>
        <span className="tag"><b>{(capture.totalBytes / 1024).toFixed(0)}</b>kB</span>
        {capture.trigger && <span className="tag">trigger <b>{capture.trigger}</b></span>}
        {capture.rssi !== null && capture.rssi !== undefined && (
          <span className="tag">rssi <b>{capture.rssi}</b></span>
        )}
        {capture.firmware && <span className="tag">fw <b>{capture.firmware}</b></span>}
        {peak !== null && (
          <span className="tag" style={clipping ? { color: '#c0392b' } : undefined}>
            peak <b>{(peak * 100).toFixed(0)}%</b>
            {clipping ? ' clipping' : quiet ? ' very quiet' : ''}
          </span>
        )}
      </div>

      {capture.note && <p className="sub" style={{ marginTop: 10 }}>{capture.note}</p>}

      <div className="row">
        <a className="link" href={capture.audioUrl} download={`${capture.id}.wav`}>
          Download WAV
        </a>
        <button className="danger" onClick={() => onDelete(capture.id)}>Delete</button>
      </div>
    </article>
  );
}

export default function Page() {
  const [captures, setCaptures] = useState([]);
  const [error, setError] = useState(null);
  const [loaded, setLoaded] = useState(false);
  const [live, setLive] = useState(true);

  const load = useCallback(async () => {
    try {
      const res = await fetch('/api/captures', { cache: 'no-store' });
      const data = await res.json();
      if (!data.ok) throw new Error(data.error || 'request failed');
      setCaptures(data.captures);
      setError(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    if (!live) return undefined;
    const timer = setInterval(load, 4000);
    return () => clearInterval(timer);
  }, [live, load]);

  const onDelete = async (id) => {
    setCaptures((prev) => prev.filter((c) => c.id !== id));
    await fetch(`/api/capture?id=${encodeURIComponent(id)}`, { method: 'DELETE' });
    load();
  };

  return (
    <main>
      <header>
        <div>
          <h1>Capture Review</h1>
          <p className="sub">Raw audio arriving from the ESP32-S3-BOX-3, exactly as recorded.</p>
        </div>
        <div className="controls">
          <label>
            <input type="checkbox" checked={live} onChange={(e) => setLive(e.target.checked)} />
            {live && <span className="dot" />} Auto-refresh
          </label>
          <button onClick={load}>Refresh</button>
        </div>
      </header>

      {error && (
        <div className="card err">
          <strong>Could not load captures.</strong>
          <p className="sub" style={{ marginTop: 6 }}>{error}</p>
        </div>
      )}

      {loaded && !error && captures.length === 0 && (
        <div className="empty">
          <h2>No captures yet</h2>
          <p>
            Waiting for the first POST. To prove the page works before the board is flashed, send a
            test clip from your Mac:
          </p>
          <pre>npm run post-test -- https://your-app.vercel.app</pre>
        </div>
      )}

      {captures.map((capture) => (
        <Capture key={capture.id} capture={capture} onDelete={onDelete} />
      ))}
    </main>
  );
}
