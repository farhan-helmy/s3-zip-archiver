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

/** The stages a single object passes through, in order. */
const STEPS: { stage: Stage; label: string; hint: string }[] = [
  { stage: "upload", label: "Upload", hint: "PUT to incoming/" },
  { stage: "trigger", label: "S3 event", hint: "prefix filter matched" },
  { stage: "compress", label: "Compress", hint: "stream through DEFLATE" },
  { stage: "verify", label: "Verify", hint: "head_object, non-zero" },
  { stage: "cleanup", label: "Delete original", hint: "only after verify" },
  { stage: "done", label: "Done", hint: "archive available" },
];

const STEP_INDEX = new Map(STEPS.map((s, i) => [s.stage, i]));

function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(2)} MB`;
}

function fmtTime(ts: number): string {
  return new Date(ts).toLocaleTimeString("en-GB", { hour12: false });
}

/**
 * Builds a payload shaped like the video-processing output described in the
 * brief: repetitive per-frame records, which is exactly the structure DEFLATE
 * handles well. Generated in the browser so no Python is needed to try this.
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
    processing: {
      pipeline: "video-analysis-v2",
      node: "onprem-render-14",
      models: ["yolov8x", "whisper-large-v3"],
    },
    frames,
  };

  while (true) {
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
  const [sizeMb, setSizeMb] = useState(2);
  const [runStart, setRunStart] = useState<number | null>(null);

  const feedRef = useRef<HTMLDivElement>(null);

  // --- config -------------------------------------------------------------
  const loadConfig = useCallback(async () => {
    const res = await fetch("/api/config");
    const body = await res.json();
    setConfig(body.config);
    setConfigError(body.error);
  }, []);

  useEffect(() => {
    void loadConfig();
  }, [loadConfig]);

  // --- websocket ----------------------------------------------------------
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
        if (payload.type === "history") setEvents(payload.events);
        else if (payload.type === "event") {
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

  // --- derived state ------------------------------------------------------
  const runEvents = useMemo(
    () => (runStart ? events.filter((e) => e.ts >= runStart - 500) : []),
    [events, runStart],
  );

  const reached = useMemo(() => {
    let furthest = -1;
    for (const event of runEvents) {
      const idx = STEP_INDEX.get(event.stage);
      if (idx !== undefined && idx > furthest) furthest = idx;
    }
    return furthest;
  }, [runEvents]);

  const failed = runEvents.some((e) => e.stage === "error");
  const complete = runEvents.some((e) => e.stage === "done");

  const result = useMemo(() => {
    const archived = [...runEvents].reverse().find((e) => e.stage === "compress");
    if (!archived?.data) return null;
    const original = Number(archived.data.original_bytes);
    const compressed = Number(archived.data.compressed_bytes);
    if (!original || !compressed) return null;
    return { original, compressed, ratio: 1 - compressed / original };
  }, [runEvents]);

  const elapsed = useMemo(() => {
    const done = runEvents.find((e) => e.stage === "done");
    return done?.data?.elapsedMs ? Number(done.data.elapsedMs) / 1000 : null;
  }, [runEvents]);

  // --- actions ------------------------------------------------------------
  const upload = useCallback(async (file: File) => {
    setBusy(true);
    setRunStart(Date.now());
    try {
      const form = new FormData();
      form.append("file", file);
      const res = await fetch("/api/upload", { method: "POST", body: form });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        alert(body.error ?? "upload failed");
      }
    } finally {
      setBusy(false);
    }
  }, []);

  const sendGenerated = useCallback(() => {
    const json = generateSample(sizeMb * 1024 * 1024);
    const file = new File([json], `sample-${sizeMb}mb.json`, {
      type: "application/json",
    });
    void upload(file);
  }, [sizeMb, upload]);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const file = e.dataTransfer.files[0];
      if (file) void upload(file);
    },
    [upload],
  );

  // --- render -------------------------------------------------------------
  if (configError) {
    return (
      <div className="shell">
        <div className="card error-card">
          <h2>Not connected to AWS</h2>
          <p className="mono">{configError}</p>
          <button
            className="primary"
            onClick={async () => {
              await fetch("/api/reconnect", { method: "POST" });
              void loadConfig();
            }}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="shell">
      <header>
        <div>
          <h1>s3-zip-archiver</h1>
          <p className="sub">
            Live view of the deployed pipeline. Every event below is read from the
            real stack.
          </p>
        </div>
        <div className={`status ${connected ? "ok" : "off"}`}>
          <span className="dot" />
          {connected ? "streaming" : "reconnecting"}
        </div>
      </header>

      {config && (
        <div className="meta mono">
          <span><b>bucket</b> {config.bucket}</span>
          <span><b>fn</b> {config.functionName}</span>
          <span><b>region</b> {config.region}</span>
        </div>
      )}

      <div className="grid">
        <section className="card">
          <h2>Send an object</h2>

          <div
            className={`drop ${busy ? "busy" : ""}`}
            onDragOver={(e) => e.preventDefault()}
            onDrop={onDrop}
          >
            <p>Drop a file here</p>
            <p className="hint">
              it is uploaded to <code>{config?.sourcePrefix ?? "incoming/"}</code>
            </p>
          </div>

          <div className="divider"><span>or generate one</span></div>

          <label className="slider-row">
            <span>Sample size</span>
            <strong className="mono">{sizeMb} MB</strong>
          </label>
          <input
            type="range"
            min={1}
            max={20}
            value={sizeMb}
            onChange={(e) => setSizeMb(Number(e.target.value))}
          />

          <button className="primary" disabled={busy} onClick={sendGenerated}>
            {busy ? "Uploading…" : `Generate & upload ${sizeMb} MB`}
          </button>

          <p className="footnote">
            Representative video-analysis JSON — repetitive per-frame records, the
            shape the brief describes.
          </p>

          {result && (
            <div className="result">
              <div className="ratio">{(result.ratio * 100).toFixed(2)}%</div>
              <div className="ratio-label">smaller</div>
              <div className="bar">
                <div
                  className="bar-fill"
                  style={{ width: `${(1 - result.ratio) * 100}%` }}
                />
              </div>
              <dl>
                <div><dt>original</dt><dd className="mono">{fmtBytes(result.original)}</dd></div>
                <div><dt>archive</dt><dd className="mono">{fmtBytes(result.compressed)}</dd></div>
                {elapsed !== null && (
                  <div><dt>end to end</dt><dd className="mono">{elapsed.toFixed(1)}s</dd></div>
                )}
              </dl>
            </div>
          )}
        </section>

        <section className="card">
          <h2>Pipeline</h2>
          <ol className="steps">
            {STEPS.map((step, i) => {
              const state = failed && i > reached
                ? "failed"
                : i < reached || (complete && i <= reached)
                  ? "done"
                  : i === reached
                    ? "active"
                    : "idle";
              return (
                <li key={step.stage} className={state}>
                  <span className="marker">{state === "done" ? "✓" : i + 1}</span>
                  <div>
                    <strong>{step.label}</strong>
                    <span className="hint">{step.hint}</span>
                  </div>
                </li>
              );
            })}
          </ol>

          <div className="note">
            Nothing fires for <code>{config?.archivePrefix ?? "archive/"}</code> —
            the notification filter is what makes the loop impossible.
          </div>
        </section>

        <section className="card feed-card">
          <h2>
            Events
            <button className="ghost" onClick={() => setEvents([])}>clear</button>
          </h2>
          <div className="feed" ref={feedRef}>
            {events.length === 0 && (
              <p className="empty">Waiting for activity…</p>
            )}
            {events.map((event) => (
              <div key={event.id} className={`row ${event.stage}`}>
                <span className="ts mono">{fmtTime(event.ts)}</span>
                <span className="tag">{event.stage}</span>
                <div className="body">
                  <strong>{event.title}</strong>
                  {event.detail && <span className="detail mono">{event.detail}</span>}
                </div>
              </div>
            ))}
          </div>
        </section>
      </div>
    </div>
  );
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
