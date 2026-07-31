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
2. **S3 event** — read from the Lambda's `{"event": "invoked"}` log line
3. **Compress** — read from `{"event": "archived"}`, including the real ratio
4. **Verify** — the archive is confirmed present in S3 by `head_object`
5. **Delete original** — confirmed by the original returning 404
6. **Done** — with end-to-end timing

Stages 2 and 3 come from the function's own structured logs. Stages 4 and 5 are
verified independently against S3 rather than trusted from the log line that
claims them — the same reasoning as the handler's own verify-before-delete.

## How "real time" works here, honestly

The browser connection is a genuine WebSocket, and events are pushed to it the
moment the server learns of them.

**The AWS side is polling.** The server calls `FilterLogEvents` about once a
second and checks object existence every 700 ms. Neither S3 nor Lambda pushes
events to a laptop, and the alternatives — an API Gateway WebSocket API, or
EventBridge to a subscriber — would mean deploying extra infrastructure into an
account this tool deliberately does not modify.

The practical effect is that events appear within roughly a second of happening,
which is well inside the 3–5 second pipeline. But it is polling behind a
WebSocket, not a push pipeline end to end, and it would be wrong to present it as
one.

## Cost

Negligible, but not zero: `FilterLogEvents` and `HeadObject` calls while the page
is open, plus the S3 storage and Lambda invocation for whatever you upload.
Uploads go to the real bucket and are really processed. Stop the server when
you're done.
