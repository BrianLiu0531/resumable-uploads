import { useCallback, useEffect, useRef, useState, type DragEvent } from 'react';
import * as tus from 'tus-js-client';
import { formatBytes, formatDuration, uploadId } from './format';

interface Config {
  endpoint: string;
  maxUploadSize: number;
  chunkSize: number;
  ttlHours: number;
  storage: string;
  dailyQuotaRemaining: number;
}

interface Done {
  id: string;
  filename: string;
  length: number;
  expiresAt: string;
}

interface LogEntry {
  n: number;
  method: string;
  path: string;
  status?: number;
  detail: string;
  ms?: number;
  kind?: 'drop' | 'info';
}

type Phase = 'uploading' | 'paused' | 'dropped' | 'done' | 'error';

interface Active {
  name: string;
  size: number;
  sent: number;
  phase: Phase;
  speed: number;
  notice?: string;
  error?: string;
}

const DONE_KEY = 'resumable-uploads:done';

function loadDone(): Done[] {
  try {
    return JSON.parse(localStorage.getItem(DONE_KEY) ?? '[]');
  } catch {
    return [];
  }
}

function saveDone(list: Done[]) {
  try {
    localStorage.setItem(DONE_KEY, JSON.stringify(list));
  } catch {
    // Private mode: the list just won't survive a reload
  }
}

export default function App() {
  const [config, setConfig] = useState<Config>();
  const [active, setActive] = useState<Active>();
  const [log, setLog] = useState<LogEntry[]>([]);
  const [done, setDone] = useState<Done[]>(loadDone);
  const [unfinished, setUnfinished] = useState<tus.PreviousUpload[]>([]);
  const [dragging, setDragging] = useState(false);
  const uploadRef = useRef<tus.Upload>();
  const seq = useRef(0);
  const started = useRef(new WeakMap<object, number>());
  const lastProgress = useRef({ t: 0, bytes: 0 });

  useEffect(() => {
    fetch('/api/config').then((r) => r.json()).then(setConfig).catch(() => undefined);
    tus.defaultOptions.urlStorage?.findAllUploads().then(setUnfinished).catch(() => undefined);
    // Forget finished files the server has already deleted
    Promise.all(loadDone().map((d) => fetch(`/api/uploads/${d.id}`).then((r) => (r.ok ? d : null)).catch(() => d))).then(
      (list) => {
        const kept = list.filter((d): d is Done => d !== null);
        setDone(kept);
        saveDone(kept);
      },
    );
  }, []);

  const addLog = useCallback((e: Omit<LogEntry, 'n'>) => {
    setLog((l) => [{ ...e, n: ++seq.current }, ...l].slice(0, 60));
  }, []);

  function start(file: File) {
    if (!config) return;
    if (file.size > config.maxUploadSize) {
      setActive({ name: file.name, size: file.size, sent: 0, phase: 'error', speed: 0, error: `This demo accepts files up to ${formatBytes(config.maxUploadSize)}.` });
      return;
    }
    uploadRef.current?.abort();
    setLog([]);
    lastProgress.current = { t: performance.now(), bytes: 0 };
    setActive({ name: file.name, size: file.size, sent: 0, phase: 'uploading', speed: 0 });

    const upload = new tus.Upload(file, {
      endpoint: config.endpoint,
      chunkSize: config.chunkSize,
      retryDelays: [0, 1000, 3000, 5000, 10000],
      metadata: { filename: file.name, filetype: file.type || 'application/octet-stream' },
      removeFingerprintOnSuccess: true,
      onBeforeRequest(req) {
        started.current.set(req, performance.now());
      },
      onAfterResponse(req, res) {
        const method = req.getMethod();
        const offsetIn = req.getHeader('Upload-Offset');
        const offsetOut = res.getHeader('Upload-Offset');
        const t0 = started.current.get(req);
        let detail = '';
        if (method === 'POST') detail = `created, length ${formatBytes(file.size)}`;
        else if (method === 'HEAD') detail = `server has ${formatBytes(Number(offsetOut ?? 0))}`;
        else if (method === 'PATCH' && offsetOut)
          detail = `${formatBytes(Number(offsetIn))} → ${formatBytes(Number(offsetOut))}`;
        else if (method === 'PATCH') detail = `rejected at ${formatBytes(Number(offsetIn))}`;
        else if (method === 'DELETE') detail = 'terminated';
        addLog({ method, path: new URL(req.getURL(), location.href).pathname, status: res.getStatus(), detail, ms: t0 ? Math.round(performance.now() - t0) : undefined });
      },
      onProgress(sent, total) {
        const now = performance.now();
        const prev = lastProgress.current;
        const instant = now > prev.t ? ((sent - prev.bytes) / (now - prev.t)) * 1000 : 0;
        lastProgress.current = { t: now, bytes: sent };
        setActive((a) => a && { ...a, sent, size: total, speed: a.speed ? a.speed * 0.7 + instant * 0.3 : instant });
      },
      onSuccess() {
        const id = uploadId(upload.url ?? '');
        setActive((a) => a && { ...a, sent: a.size, phase: 'done', notice: undefined });
        fetch(`/api/uploads/${id}`)
          .then((r) => r.json())
          .then((u) => {
            setDone((list) => {
              const next = [{ id, filename: u.filename, length: u.length, expiresAt: u.expiresAt }, ...list.filter((d) => d.id !== id)];
              saveDone(next);
              return next;
            });
          });
        tus.defaultOptions.urlStorage?.findAllUploads().then(setUnfinished);
      },
      onError(err) {
        const res = (err as tus.DetailedError).originalResponse;
        const message = res ? `${res.getStatus()} ${res.getBody()}` : err.message;
        setActive((a) => a && { ...a, phase: 'error', error: message });
      },
    });
    uploadRef.current = upload;

    // Same file chosen again (even after closing the tab)? Continue the earlier upload.
    upload.findPreviousUploads().then((previous) => {
      if (previous.length > 0) {
        upload.resumeFromPreviousUpload(previous[0]);
        setActive((a) => a && { ...a, notice: 'Found an earlier upload of this file: asking the server how much it already has, then continuing from there.' });
        addLog({ method: '↺', path: 'resume', detail: 'previous upload found in this browser', kind: 'info' });
      }
      upload.start();
    });
  }

  function pause() {
    uploadRef.current?.abort();
    setActive((a) => a && { ...a, phase: 'paused', speed: 0 });
  }

  function resume() {
    lastProgress.current = { t: performance.now(), bytes: active?.sent ?? 0 };
    uploadRef.current?.start();
    setActive((a) => a && { ...a, phase: 'uploading' });
  }

  function dropConnection() {
    // Abort the request in flight, as a lost connection would; the server keeps what arrived
    uploadRef.current?.abort();
    addLog({ method: '⚡', path: 'network', detail: 'connection dropped mid-request (simulated)', kind: 'drop' });
    setActive((a) => a && { ...a, phase: 'dropped', speed: 0 });
    setTimeout(() => {
      lastProgress.current = { t: performance.now(), bytes: 0 };
      uploadRef.current?.start();
      setActive((a) => a && a.phase === 'dropped' ? { ...a, phase: 'uploading' } : a);
    }, 1500);
  }

  function cancel() {
    uploadRef.current?.abort(true); // sends DELETE: the server removes what it has
    setActive(undefined);
  }

  async function remove(d: Done) {
    await fetch(`/files/${d.id}`, { method: 'DELETE', headers: { 'Tus-Resumable': '1.0.0' } });
    setDone((list) => {
      const next = list.filter((x) => x.id !== d.id);
      saveDone(next);
      return next;
    });
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    setDragging(false);
    const file = e.dataTransfer.files[0];
    if (file) start(file);
  }

  const pct = active && active.size ? (active.sent / active.size) * 100 : 0;

  return (
    <main>
      <header>
        <h1>Resumable Uploads</h1>
        <p className="lead">
          Large files over unreliable connections. Pause, lose your network or close the tab, and the upload carries on from the last byte the
          server stored.
        </p>
        <div className="badges">
          <span>tus 1.0 protocol</span>
          <span>{config?.storage === 's3' ? 'Amazon S3 multipart' : 'local storage'}</span>
          <span>files deleted after {config?.ttlHours ?? 24} h</span>
          {config && <span>max {formatBytes(config.maxUploadSize)}</span>}
        </div>
      </header>

      <label
        className={`drop ${dragging ? 'dragging' : ''}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <input type="file" onChange={(e) => e.target.files?.[0] && start(e.target.files[0])} />
        <strong>Drop a file here</strong> or click to choose one
        <span className="muted small">Uploaded in {config ? formatBytes(config.chunkSize) : '5 MB'} chunks, one S3 part per request</span>
      </label>

      {unfinished.length > 0 && !active && (
        <section className="card note">
          <strong>Unfinished uploads in this browser</strong>
          <ul>
            {unfinished.map((u) => (
              <li key={u.urlStorageKey}>
                {u.metadata.filename ?? 'file'} ({formatBytes(u.size ?? 0)}): choose the same file again to continue
              </li>
            ))}
          </ul>
        </section>
      )}

      {active && (
        <section className="card">
          <div className="row between">
            <strong className="filename">{active.name}</strong>
            <span className={`phase ${active.phase}`}>{active.phase}</span>
          </div>
          <div className="bar" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin={0} aria-valuemax={100}>
            <div style={{ width: `${pct}%` }} />
          </div>
          <div className="row between small muted">
            <span>
              {formatBytes(active.sent)} of {formatBytes(active.size)} ({pct.toFixed(1)}%)
            </span>
            {active.phase === 'uploading' && active.speed > 0 && <span>{formatBytes(active.speed)}/s</span>}
          </div>
          {active.notice && <p className="small notice">{active.notice}</p>}
          {active.error && <p className="small error">{active.error}</p>}
          <div className="row actions">
            {active.phase === 'uploading' && (
              <>
                <button onClick={pause}>Pause</button>
                <button onClick={dropConnection} title="Abort the request in flight, like a lost connection">
                  ⚡ Simulate network drop
                </button>
              </>
            )}
            {(active.phase === 'paused' || active.phase === 'error') && <button onClick={resume}>Resume</button>}
            {active.phase !== 'done' && (
              <button className="ghost" onClick={cancel}>
                Cancel
              </button>
            )}
          </div>
        </section>
      )}

      {log.length > 0 && (
        <section className="card">
          <h2>Protocol log</h2>
          <p className="muted small">Every request the tus client made, newest first.</p>
          <div className="log">
            {log.map((e) => (
              <div key={e.n} className={`log-row ${e.kind ? `is-${e.kind}` : ''}`}>
                <code className="method">{e.method}</code>
                <code className="path">{e.path}</code>
                <span className={`status s${String(e.status ?? '')[0]}`}>{e.status ?? ''}</span>
                <span className="detail">{e.detail}</span>
                <span className="ms">{e.ms !== undefined ? `${e.ms} ms` : ''}</span>
              </div>
            ))}
          </div>
        </section>
      )}

      {done.length > 0 && (
        <section className="card">
          <h2>Your uploaded files</h2>
          <ul className="files">
            {done.map((d) => (
              <li key={d.id}>
                <span className="filename">{d.filename}</span>
                <span className="muted small">
                  {formatBytes(d.length)} · deleted in {formatDuration(new Date(d.expiresAt).getTime() - Date.now())}
                </span>
                <span className="row">
                  <a className="button" href={`/api/uploads/${d.id}/download`}>
                    Download
                  </a>
                  <button className="ghost" onClick={() => remove(d)}>
                    Delete
                  </button>
                </span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="muted small">
        Django · PostgreSQL · Amazon S3 multipart uploads · React + tus-js-client ·{' '}
        <a href="https://github.com/BrianLiu0531/resumable-uploads">source on GitHub</a>
        <br />
        Files are private, deleted automatically after {config?.ttlHours ?? 24} hours, and uploads are rate-limited.
      </footer>
    </main>
  );
}
