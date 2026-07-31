# Live pipeline viewer

A small local tool for watching the deployed pipeline actually work: upload an
object, and see the real stages light up as S3 and Lambda process it.

**This is a development and demo tool. It is not part of the deployed system, it
is not deployed anywhere, and it changes nothing in AWS.** It runs on your
machine, uses your own credentials, and only reads what the stack is already
doing.

## Running it

```bash
aws sso login --profile sk8jx     # or however you authenticate
cd ui
bun install
bun run dev                        # http://localhost:4173
```

Configuration comes from environment variables, all optional:

| Variable | Default |
|---|---|
| `AWS_PROFILE` | `sk8jx` |
| `AWS_REGION` | `ap-southeast-1` |
| `STACK_NAME` | `s3-zip-archiver` |
| `PORT` | `4173` |

The bucket and function names are read from the CloudFormation stack outputs at
startup, so this follows the deployment rather than drifting from it.

## What it shows

Drop a file in, or generate a sample of a chosen size in the browser. Then:

1. **Upload** — the server PUTs the object under `incoming/`
2. **Lambda ran** — inferred, because an archive cannot exist without an invocation
3. **Compress** — with the real ratio, computed from the two S3 object sizes
4. **Verify** — the archive confirmed present by `head_object`
5. **Delete original** — confirmed by the original returning 404
6. **Done** — with end-to-end timing

Every one of those is observed directly against S3. The function's structured log
lines then arrive a few seconds later and appear in the feed as supplementary
detail: the invocation record count, the ratio Lambda measured itself, and the
execution report with duration and peak memory.

## Downloading the archive

Once a run finishes, **Download archive** pulls the ZIP back out of S3 through
`/api/archive/download`. Unzip it however you like — the file is served as-is.

This is the part that actually closes the loop. The original has been deleted by
then, so opening the archive and finding the original content intact is the only
remaining proof nothing was lost. Verified with the system `unzip`:

```
$ unzip -t downloaded.zip
    testing: 1785470664196-download-test.json   OK
No errors detected in compressed data of downloaded.zip.

sent.json    2,150,362 bytes
archive        370,974 bytes    82.75% smaller
extracted    2,150,362 bytes    byte-identical to what was sent
```

The endpoint refuses any key outside the archive prefix. It is a local tool, but
the key still arrives from the client and does not get to name arbitrary objects
in the bucket.

## Why the stages don't come from the logs

The first version of this tool read the compression stages out of CloudWatch
Logs, and it was wrong in a way worth recording.

**CloudWatch Logs lag ingestion by roughly 10 seconds. The pipeline finishes in
3–5.** So a run reliably completed *before* its own log lines became readable,
and the UI showed a finished pipeline with two stages still blank. Measured
during development:

```
11:16:28  upload / trigger / compress / verify / cleanup / done   ← S3-observed
11:16:38  invocation, ratio, execution report                     ← CloudWatch, 9s later
```

The fix was to stop depending on logs for anything time-sensitive. The upload
size is known because the server sent it, and the archive size comes back from
`head_object` — so the ratio is available immediately, from the source of truth,
without parsing a log line that hasn't arrived.

This leaves a useful property: the ratio computed from S3 object sizes and the
ratio Lambda calculated internally are derived independently and shown side by
side. They agree, which is a real cross-check rather than the same number
displayed twice.

## How "real time" works here, honestly

The browser connection is a genuine WebSocket, and events are pushed to it the
moment the server learns of them.

**The AWS side is polling.** The server calls `FilterLogEvents` about once a
second and checks object existence every 700 ms. Neither S3 nor Lambda pushes
events to a laptop, and the alternatives — an API Gateway WebSocket API, or
EventBridge to a subscriber — would mean deploying extra infrastructure into an
account this tool deliberately does not modify.

The practical effect is that stages appear within about a second of happening.
But it is polling behind a WebSocket, not a push pipeline end to end, and it
would be wrong to present it as one.

## Cost

Negligible, but not zero: `FilterLogEvents` and `HeadObject` calls while the page
is open, plus the S3 storage and Lambda invocation for whatever you upload.
Uploads go to the real bucket and are really processed. Stop the server when
you're done.
