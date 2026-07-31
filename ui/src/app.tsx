import { StrictMode, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";

import "./styles.css";

type Stage =
  | "upload"
  | "trigger"
  | "compress"
  | "verify"
  | "cleanup"
  | "done"
  | "log"
  | "error";

interface PipelineEvent {
  id: string;
  ts: number;
  stage: Stage;
  title: string;
  detail?: string;
  data?: Record<string, unknown>;
}

interface StackConfig {
  bucket: string;
  sourcePrefix: string;
  archivePrefix: string;
  functionName: string;
  region: string;
}


/**
 * A genuine ordered sequence, so the numbering carries information rather than
 * decorating. Each label is what happened, not what the system did internally.
 */
const STAGES: { stage: Stage; label: string }[] = [
  { stage: "upload", label: "Upload" },
  { stage: "trigger", label: "Invoke" },
  { stage: "compress", label: "Compress" },
  { stage: "verify", label: "Verify" },
  { stage: "cleanup", label: "Delete source" },
  { stage: "done", label: "Done" },
];

const STAGE_INDEX = new Map(STAGES.map((s, i) => [s.stage, i]));

/** Events read straight from S3. Anything else arrived second-hand, via logs. */
const DIRECT: ReadonlySet<Stage> = new Set([
  "upload",
  "trigger",
  "compress",
  "verify",
  "cleanup",
  "done",
]);

const bytes = (n: number) => n.toLocaleString("en-GB");

function human(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(2)} MB`;
}

function clock(ts: number): string {
  return new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * Builds a payload shaped like the video-processing output in the brief:
 * repetitive per-frame records, which is the structure DEFLATE handles best.
 * Generated in the browser so trying this needs nothing installed.
 */
function generateSample(targetBytes: number): string {
  const labels = ["person", "car", "bicycle", "traffic_light", "dog", "laptop"];
  const frames: unknown[] = [];
  let index = 0;

  const doc = {
    schema_version: "2.4.0",
    asset: {
      asset_id: "a7f3c2e1-9b4d-4c8a-b6e5-1d2f3a4b5c6d",
      source_filename: "master_4k_prores.mov",
      resolution: { width: 3840, height: 2160 },
      framerate: 29.97,
      codec: "h265",
    },
    processing: { pipeline: "video-analysis-v2", node: "onprem-render-14" },
    frames,
  };

  for (;;) {
    for (let i = 0; i < 400; i++) {
      const detections = [];
      for (let d = 0; d < 1 + Math.floor(Math.random() * 4); d++) {
        detections.push({
          label: labels[Math.floor(Math.random() * labels.length)],
          confidence: Number((0.42 + Math.random() * 0.57).toFixed(4)),
          bounding_box: {
            x: Number((Math.random() * 1920).toFixed(2)),
            y: Number((Math.random() * 1080).toFixed(2)),
            width: Number((20 + Math.random() * 380).toFixed(2)),
            height: Number((20 + Math.random() * 380).toFixed(2)),
          },
          tracking_id: Math.floor(Math.random() * 500),
        });
      }
      frames.push({
        frame_index: index,
        timestamp_seconds: Number((index / 29.97).toFixed(4)),
        keyframe: index % 48 === 0,
        detections,
        audio: {
          peak_db: Number((-60 + Math.random() * 57).toFixed(2)),
          rms_db: Number((-70 + Math.random() * 58).toFixed(2)),
        },
      });
      index++;
    }
    const serialised = JSON.stringify(doc);
    if (serialised.length >= targetBytes) return serialised;
  }
}

function App() {
  const [config, setConfig] = useState<StackConfig | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [events, setEvents] = useState<PipelineEvent[]>([]);
  const [busy, setBusy] = useState(false);
  const [sizeMb, setSizeMb] = useState(3);
  const [runStart, setRunStart] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);

  const feedRef = useRef<HTMLDivElement>(null);

  const loadConfig = useCallback(async () => {
    const res = await fetch("/api/config");
    const body = await res.json();
    setConfig(body.config);
    setConfigError(body.error);
  }, []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  useEffect(() => {
    let socket: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout>;

    const connect = () => {
      socket = new WebSocket(`ws://${location.host}/ws`);
      socket.onopen = () => setConnected(true);
      socket.onclose = () => {
        setConnected(false);
        retry = setTimeout(connect, 1500);
      };
      socket.onmessage = (msg) => {
        const payload = JSON.parse(msg.data);
        if (payload.type === "history") {
          setEvents(payload.events);
          // The server replays recent events on connect, so anchor the run to
          // the last upload it saw. Without this a refresh keeps the log but
          // silently drops the measurement, which is the part worth keeping.
          const lastUpload = [...(payload.events as PipelineEvent[])]
            .reverse()
            .find((e) => e.stage === "upload" && e.data?.bytes);
          if (lastUpload) setRunStart(lastUpload.ts);
        } else if (payload.type === "event") {
          setEvents((prev) => [...prev, payload.event].slice(-200));
        }
      };
    };

    connect();
    return () => {
      clearTimeout(retry);
      socket?.close();
    };
  }, []);

  useEffect(() => {
    feedRef.current?.scrollTo({ top: feedRef.current.scrollHeight, behavior: "smooth" });
  }, [events]);

  // --- derived ------------------------------------------------------------
  const runEvents = useMemo(
    () => (runStart ? events.filter((e) => e.ts >= runStart - 500) : []),
    [events, runStart],
  );

  const reached = useMemo(() => {
    let furthest = -1;
    for (const event of runEvents) {
      const idx = STAGE_INDEX.get(event.stage);
      if (idx !== undefined && idx > furthest) furthest = idx;
    }
    return furthest;
  }, [runEvents]);

  const measure = useMemo(() => {
    const uploaded = runEvents.find((e) => e.stage === "upload" && e.data?.bytes);
    const compressed = [...runEvents].reverse().find((e) => e.stage === "compress");
    const source = Number(uploaded?.data?.bytes ?? 0);
    const archive = Number(compressed?.data?.compressed_bytes ?? 0);
    if (!source) return null;
    return {
      source,
      archive: archive || null,
      ratio: archive ? 1 - archive / source : null,
      sourceGone: runEvents.some((e) => e.stage === "cleanup"),
    };
  }, [runEvents]);

  const elapsed = useMemo(() => {
    const done = runEvents.find((e) => e.stage === "done");
    return done?.data?.elapsedMs ? Number(done.data.elapsedMs) / 1000 : null;
  }, [runEvents]);

  /** Key of the archive this run produced, so it can be pulled back out of S3. */
  const archiveKey = useMemo(() => {
    const event = [...runEvents]
      .reverse()
      .find((e) => (e.stage === "compress" || e.stage === "verify") && e.data?.key);
    return (event?.data?.key as string) ?? null;
  }, [runEvents]);

  const failed = runEvents.some((e) => e.stage === "error");

  // --- actions ------------------------------------------------------------
  const send = useCallback(async (file: File) => {
    setBusy(true);
    setRunStart(Date.now());
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.error ?? "Upload failed.");
      }
    } finally {
      setBusy(false);
    }
  }, []);

  const sendGenerated = useCallback(() => {
    const json = generateSample(sizeMb * 1024 * 1024);
    void send(new File([json], `sample-${sizeMb}mb.json`, { type: "application/json" }));
  }, [sizeMb, send]);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      setDragging(false);
      const file = e.dataTransfer.files[0];
      if (file) void send(file);
    },
    [send],
  );

  // --- disconnected -------------------------------------------------------
  if (configError) {
    return (
      <main className="page">
        <div className="halt">
          <p className="eyebrow">Not connected</p>
          <h1 className="halt-title">{configError}</h1>
          <button
            className="btn"
            onClick={async () => {
              await fetch("/api/reconnect", { method: "POST" });
              void loadConfig();
            }}
          >
            Try again
          </button>
        </div>
      </main>
    );
  }

  const sourceWidth = 100;
  const archiveWidth = measure?.ratio != null ? Math.max((1 - measure.ratio) * 100, 0.4) : 0;

  return (
    <main className="page">
      <header className="masthead">
        <div>
          <h1 className="wordmark">s3-zip-archiver</h1>
          <p className="strapline">
            Every measurement below is read from the deployed stack.
          </p>
        </div>
        <div className={`signal ${connected ? "is-live" : "is-down"}`}>
          <span className="signal-mark" aria-hidden="true" />
          {connected ? "Live" : "Reconnecting"}
        </div>
      </header>

      {config && (
        <dl className="facts">
          <div>
            <dt>Bucket</dt>
            <dd>{config.bucket}</dd>
          </div>
          <div>
            <dt>Function</dt>
            <dd>{config.functionName}</dd>
          </div>
          <div>
            <dt>Region</dt>
            <dd>{config.region}</dd>
          </div>
        </dl>
      )}

      {/* Signature: source and archive drawn to true relative scale. */}
      <section
        className={`measure ${dragging ? "is-dragging" : ""} ${busy ? "is-busy" : ""}`}
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        {!measure ? (
          <div className="invite">
            <p className="invite-line">Drop a file to compress it</p>
            <p className="invite-sub">
              or generate one below. Anything you send goes to{" "}
              <code>{config?.sourcePrefix ?? "incoming/"}</code> in the real bucket.
            </p>
          </div>
        ) : (
          <div className="scale">
            <div className="band">
              <div className="band-head">
                <span className="eyebrow">Source</span>
                <span className="figure">
                  {bytes(measure.source)}
                  <span className="unit"> bytes</span>
                </span>
              </div>
              {/* Hollow once deleted: the object no longer exists. */}
              <div
                className={`bar ${measure.sourceGone ? "is-gone" : ""}`}
                style={{ width: `${sourceWidth}%` }}
              />
              <p className="band-note">
                {measure.sourceGone ? "Deleted after the archive was verified" : human(measure.source)}
              </p>
            </div>

            <div className="band">
              <div className="band-head">
                <span className="eyebrow">Archive</span>
                <span className="figure">
                  {measure.archive ? bytes(measure.archive) : "—"}
                  <span className="unit"> bytes</span>
                </span>
              </div>
              <div className="bar is-archive" style={{ width: `${archiveWidth}%` }} />
              <p className="band-note">
                {measure.archive ? human(measure.archive) : "Waiting for the archive"}
              </p>
            </div>

            {measure.ratio != null && (
              <div className="verdict">
                <span className="verdict-figure">{(measure.ratio * 100).toFixed(2)}%</span>
                <span className="verdict-label">
                  removed
                  {elapsed !== null && <> · {elapsed.toFixed(1)}s end to end</>}
                </span>
                {archiveKey && (
                  <a
                    className="btn btn--download"
                    href={`/api/archive/download?key=${encodeURIComponent(archiveKey)}`}
                    download
                  >
                    Download archive
                  </a>
                )}
              </div>
            )}
          </div>
        )}
      </section>

      <section className="controls">
        <div className="field">
          <label className="eyebrow" htmlFor="size">
            Sample size
          </label>
          <div className="field-row">
            <input
              id="size"
              type="range"
              min={1}
              max={20}
              value={sizeMb}
              onChange={(e) => setSizeMb(Number(e.target.value))}
            />
            <output className="figure">{sizeMb} MB</output>
          </div>
        </div>
        <button className="btn" disabled={busy} onClick={sendGenerated}>
          {busy ? "Sending" : `Compress ${sizeMb} MB`}
        </button>
      </section>

      <ol className="track" aria-label="Pipeline stages">
        {STAGES.map((item, i) => {
          const state = failed && i > reached ? "halted" : i <= reached ? "passed" : "pending";
          return (
            <li key={item.stage} className={`step is-${state}`}>
              <span className="step-index">{i + 1}</span>
              <span className="step-label">{item.label}</span>
            </li>
          );
        })}
      </ol>

      <section className="ledger">
        <div className="ledger-head">
          <h2 className="eyebrow">Events</h2>
          <p className="legend">
            <span className="legend-key legend-key--direct" /> observed in S3
            <span className="legend-key legend-key--relayed" /> reported by logs
          </p>
          <button className="btn btn--quiet" onClick={() => setEvents([])}>
            Clear
          </button>
        </div>

        <div className="ledger-body" ref={feedRef}>
          {events.length === 0 ? (
            <p className="ledger-empty">Nothing yet. Send an object to begin.</p>
          ) : (
            events.map((event) => (
              <article
                key={event.id}
                className={`entry ${DIRECT.has(event.stage) ? "is-direct" : "is-relayed"} ${
                  event.stage === "error" ? "is-error" : ""
                }`}
              >
                <time className="entry-time">{clock(event.ts)}</time>
                <span className="entry-stage">{event.stage}</span>
                <p className="entry-text">
                  {event.title}
                  {event.detail && <span className="entry-detail">{event.detail}</span>}
                </p>
              </article>
            ))
          )}
        </div>
      </section>

      <footer className="colophon">
        Stages are observed directly against S3. Log lines arrive roughly ten seconds
        later and are shown in grey — the pipeline finishes before CloudWatch has
        anything to say about it.
      </footer>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
