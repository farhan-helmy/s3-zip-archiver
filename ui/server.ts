/**
 * Local development tool for watching the deployed pipeline work.
 *
 * This is NOT part of the deployed system and changes nothing in AWS. It runs on
 * your machine, uses your own AWS credentials, uploads an object to the real
 * bucket, and then reports what actually happens by reading CloudWatch Logs and
 * polling S3. Those observations are pushed to the browser over a WebSocket.
 *
 * Worth being precise about what "real time" means here: S3 and Lambda do not
 * push events to us. The server polls CloudWatch Logs roughly once a second and
 * streams whatever it finds. The browser connection is a genuine WebSocket; the
 * AWS side is polling, because that is the only option without deploying extra
 * infrastructure to an account this tool deliberately does not modify.
 *
 *   bun install && bun run dev     # then open http://localhost:4173
 */

import {
  CloudFormationClient,
  DescribeStacksCommand,
} from "@aws-sdk/client-cloudformation";
import {
  CloudWatchLogsClient,
  FilterLogEventsCommand,
} from "@aws-sdk/client-cloudwatch-logs";
import {
  HeadObjectCommand,
  PutObjectCommand,
  S3Client,
} from "@aws-sdk/client-s3";
import { fromNodeProviderChain } from "@aws-sdk/credential-providers";

import index from "./index.html";

const REGION = process.env.AWS_REGION ?? "ap-southeast-1";
const PROFILE = process.env.AWS_PROFILE ?? "sk8jx";
const STACK_NAME = process.env.STACK_NAME ?? "s3-zip-archiver";
const PORT = Number(process.env.PORT ?? 4173);
const POLL_INTERVAL_MS = 1000;

const credentials = fromNodeProviderChain({ profile: PROFILE });
const clientConfig = { region: REGION, credentials };

const cfn = new CloudFormationClient(clientConfig);
const s3 = new S3Client(clientConfig);
const logs = new CloudWatchLogsClient(clientConfig);

// ---------------------------------------------------------------------------
// Types shared with the browser.
// ---------------------------------------------------------------------------
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
  logGroup: string;
  region: string;
  aliasVersion?: string;
}

let config: StackConfig | null = null;
let configError: string | null = null;

let sequence = 0;
const nextId = () => `${Date.now()}-${++sequence}`;

// ---------------------------------------------------------------------------
// Stack discovery. Nothing is hardcoded beyond the stack name, so this tool
// follows the deployment rather than drifting from it.
// ---------------------------------------------------------------------------
async function loadConfig(): Promise<void> {
  try {
    const result = await cfn.send(
      new DescribeStacksCommand({ StackName: STACK_NAME }),
    );
    const outputs = result.Stacks?.[0]?.Outputs ?? [];
    const get = (key: string) =>
      outputs.find((o) => o.OutputKey === key)?.OutputValue ?? "";

    const functionName = get("FunctionName");
    config = {
      bucket: get("BucketName"),
      sourcePrefix: get("SourcePrefix") || "incoming/",
      archivePrefix: get("ArchivePrefix") || "archive/",
      functionName,
      logGroup: `/aws/lambda/${functionName}`,
      region: REGION,
    };
    configError = null;
    console.log(`Connected to stack ${STACK_NAME} in ${REGION}`);
    console.log(`  bucket:   ${config.bucket}`);
    console.log(`  function: ${config.functionName}`);
  } catch (error) {
    const message = (error as Error).message ?? String(error);
    configError = message.includes("sso") || message.includes("Token")
      ? `AWS credentials expired. Run: aws sso login --profile ${PROFILE}`
      : `Could not read stack ${STACK_NAME}: ${message}`;
    console.error(configError);
  }
}

// ---------------------------------------------------------------------------
// WebSocket fan-out.
// ---------------------------------------------------------------------------
const TOPIC = "pipeline";
const recent: PipelineEvent[] = [];

function emit(
  stage: Stage,
  title: string,
  detail?: string,
  data?: Record<string, unknown>,
): void {
  const event: PipelineEvent = { id: nextId(), ts: Date.now(), stage, title, detail, data };
  recent.push(event);
  if (recent.length > 300) recent.shift();
  server?.publish(TOPIC, JSON.stringify({ type: "event", event }));
}

// ---------------------------------------------------------------------------
// CloudWatch Logs tail.
//
// The handler emits single-line JSON, which is what makes this readable rather
// than a regex exercise - the same property that makes the logs queryable in
// CloudWatch Logs Insights.
// ---------------------------------------------------------------------------
let logCursor = Date.now();
const seenLogEvents = new Set<string>();

function parseStructured(message: string): Record<string, unknown> | null {
  const start = message.indexOf("{");
  if (start === -1) return null;
  try {
    return JSON.parse(message.slice(start));
  } catch {
    return null;
  }
}

async function pollLogs(): Promise<void> {
  if (!config) return;

  try {
    const result = await logs.send(
      new FilterLogEventsCommand({
        logGroupName: config.logGroup,
        startTime: logCursor,
        limit: 100,
      }),
    );

    for (const entry of result.events ?? []) {
      const key = `${entry.eventId}`;
      if (seenLogEvents.has(key)) continue;
      seenLogEvents.add(key);
      if (entry.timestamp && entry.timestamp >= logCursor) {
        logCursor = entry.timestamp + 1;
      }

      const message = (entry.message ?? "").trim();
      const structured = parseStructured(message);

      if (structured?.event === "invoked") {
        emit(
          "trigger",
          "Lambda invoked",
          `S3 delivered ${structured.record_count} record(s) to the live alias`,
          structured,
        );
      } else if (structured?.event === "archived") {
        const original = Number(structured.original_bytes);
        const compressed = Number(structured.compressed_bytes);
        emit(
          "compress",
          "Compressed and uploaded",
          `${fmtBytes(original)} to ${fmtBytes(compressed)}`,
          structured,
        );
        emit("verify", "Archive verified", "head_object confirmed non-zero size", structured);
      } else if (structured?.event?.toString().startsWith("skipped")) {
        emit("log", `Skipped: ${structured.event}`, String(structured.key ?? ""), structured);
      } else if (message.startsWith("REPORT")) {
        const duration = /Duration: ([\d.]+) ms/.exec(message)?.[1];
        const billed = /Billed Duration: (\d+) ms/.exec(message)?.[1];
        const maxMem = /Max Memory Used: (\d+) MB/.exec(message)?.[1];
        const init = /Init Duration: ([\d.]+) ms/.exec(message)?.[1];
        emit(
          "log",
          "Execution report",
          `${duration} ms${init ? ` (+${init} ms cold start)` : ""} · billed ${billed} ms · ${maxMem} MB peak`,
          { duration, billed, maxMem, init },
        );
      } else if (/ERROR|Traceback|Task timed out/.test(message)) {
        emit("error", "Lambda error", message.slice(0, 300));
      }
    }

    if (seenLogEvents.size > 2000) seenLogEvents.clear();
  } catch (error) {
    const message = (error as Error).message ?? String(error);
    if (!message.includes("ResourceNotFound")) {
      console.error("log poll failed:", message);
    }
  }
}

// ---------------------------------------------------------------------------
// Object lifecycle watch: proves the original really was deleted and the
// archive really did appear, rather than trusting the log line that says so.
// ---------------------------------------------------------------------------
async function exists(key: string): Promise<number | null> {
  if (!config) return null;
  try {
    const head = await s3.send(
      new HeadObjectCommand({ Bucket: config.bucket, Key: key }),
    );
    return head.ContentLength ?? 0;
  } catch {
    return null;
  }
}

async function watchObject(sourceKey: string): Promise<void> {
  if (!config) return;

  const name = sourceKey.slice(config.sourcePrefix.length);
  const archiveKey = `${config.archivePrefix}${name}.zip`;

  let sawArchive = false;
  let sawDeletion = false;
  const started = Date.now();

  while (Date.now() - started < 120_000) {
    await Bun.sleep(700);

    if (!sawArchive) {
      const size = await exists(archiveKey);
      if (size !== null) {
        sawArchive = true;
        emit("verify", "Archive present in S3", `${archiveKey} · ${fmtBytes(size)}`, {
          key: archiveKey,
          bytes: size,
        });
      }
    }

    if (!sawDeletion && (await exists(sourceKey)) === null) {
      sawDeletion = true;
      emit("cleanup", "Original deleted", `${sourceKey} is gone`, { key: sourceKey });
    }

    if (sawArchive && sawDeletion) {
      emit(
        "done",
        "Pipeline complete",
        `${((Date.now() - started) / 1000).toFixed(1)}s end to end`,
        { archiveKey, elapsedMs: Date.now() - started },
      );
      return;
    }
  }

  emit("error", "Timed out waiting for the pipeline", `${archiveKey} never appeared`);
}

function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return "?";
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 ** 2).toFixed(2)} MB`;
}

// ---------------------------------------------------------------------------
// HTTP
// ---------------------------------------------------------------------------
async function handleUpload(req: Request): Promise<Response> {
  if (!config) {
    return Response.json({ error: configError ?? "not connected" }, { status: 503 });
  }

  const form = await req.formData();
  const file = form.get("file");
  if (!(file instanceof File)) {
    return Response.json({ error: "no file supplied" }, { status: 400 });
  }

  const safeName = file.name.replace(/[^a-zA-Z0-9._-]/g, "_") || "upload.json";
  const key = `${config.sourcePrefix}${Date.now()}-${safeName}`;
  const body = new Uint8Array(await file.arrayBuffer());

  emit("upload", "Uploading to S3", `${key} · ${fmtBytes(body.byteLength)}`, {
    key,
    bytes: body.byteLength,
  });

  try {
    await s3.send(
      new PutObjectCommand({
        Bucket: config.bucket,
        Key: key,
        Body: body,
        ContentType: "application/json",
      }),
    );
  } catch (error) {
    const message = (error as Error).message ?? String(error);
    emit("error", "Upload failed", message);
    return Response.json({ error: message }, { status: 500 });
  }

  emit(
    "upload",
    "Object created",
    `S3 will now emit ObjectCreated for the ${config.sourcePrefix} prefix`,
    { key },
  );

  void watchObject(key);
  return Response.json({ key, bytes: body.byteLength });
}

await loadConfig();

const server = Bun.serve({
  port: PORT,
  routes: {
    "/": index,
    "/api/config": {
      GET: () =>
        Response.json({
          config,
          error: configError,
          stackName: STACK_NAME,
          profile: PROFILE,
        }),
    },
    "/api/upload": { POST: handleUpload },
    "/api/reconnect": {
      POST: async () => {
        await loadConfig();
        return Response.json({ config, error: configError });
      },
    },
  },
  fetch(req, srv) {
    if (new URL(req.url).pathname === "/ws") {
      return srv.upgrade(req)
        ? undefined
        : new Response("websocket upgrade failed", { status: 400 });
    }
    return new Response("not found", { status: 404 });
  },
  websocket: {
    open(ws) {
      ws.subscribe(TOPIC);
      // Replay recent history so a page refresh does not lose the run.
      ws.send(JSON.stringify({ type: "history", events: recent.slice(-60) }));
    },
    message() {},
    close(ws) {
      ws.unsubscribe(TOPIC);
    },
  },
});

setInterval(() => {
  void pollLogs();
}, POLL_INTERVAL_MS);

console.log(`\n  s3-zip-archiver live view  →  http://localhost:${server.port}\n`);
if (configError) {
  console.log(`  ${configError}\n`);
}
