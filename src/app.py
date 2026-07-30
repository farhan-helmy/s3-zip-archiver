"""Compress objects landing in S3 into ZIP archives and remove the originals.

Triggered by S3 ObjectCreated notifications filtered to SOURCE_PREFIX. Each object
is streamed out of S3, deflated into a ZIP, written back to the same bucket under
ARCHIVE_PREFIX, and only then deleted from its original location.

Two properties matter more than anything else here:

1. The function must never trigger itself. It writes into the same bucket it reads
   from, so an unfiltered notification would loop forever. The S3 notification is
   filtered to SOURCE_PREFIX in template.yaml, which means the recursive event is
   never generated in the first place; the guard in this module is a second line of
   defence in case that filter is ever widened.

2. The delete is destructive and irreversible from the caller's point of view. The
   original is removed only after the archive has been written AND independently
   confirmed to exist with a non-zero size. If anything fails, the original is left
   untouched and the invocation raises so the event lands in the dead-letter queue.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import zipfile
from typing import Any
from urllib.parse import unquote_plus

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

SOURCE_PREFIX = os.environ.get("SOURCE_PREFIX", "incoming/")
ARCHIVE_PREFIX = os.environ.get("ARCHIVE_PREFIX", "archive/")

# Deflate level 6 is zlib's default and sits at the knee of the curve: level 9
# costs noticeably more CPU (which Lambda bills by the millisecond) for a
# marginal gain on JSON. See the cost analysis in README.md.
COMPRESSION_LEVEL = int(os.environ.get("COMPRESSION_LEVEL", "6"))

# Objects are streamed rather than loaded whole. Anything under this threshold is
# assembled in memory; larger payloads spill to /tmp automatically.
SPOOL_MAX_BYTES = int(os.environ.get("SPOOL_MAX_BYTES", str(32 * 1024 * 1024)))
CHUNK_BYTES = 1024 * 1024

# Adaptive retries so throttling under a burst of notifications backs off rather
# than hammering S3 and burning billed duration.
_s3 = boto3.client(
    "s3",
    config=Config(retries={"max_attempts": 5, "mode": "adaptive"}),
)


def _log(event: str, **fields: Any) -> None:
    """Emit a single-line JSON log so CloudWatch Logs Insights can query fields."""
    LOGGER.info(json.dumps({"event": event, **fields}))


def archive_key_for(source_key: str) -> str:
    """Map an incoming object key to its destination archive key.

    The path below SOURCE_PREFIX is preserved so the bucket layout round-trips:
        incoming/2026/07/result.json -> archive/2026/07/result.json.zip
    """
    relative = source_key[len(SOURCE_PREFIX):]
    return f"{ARCHIVE_PREFIX}{relative}.zip"


def compress_object(bucket: str, key: str) -> dict[str, Any] | None:
    """Compress a single object and delete the original once the archive is verified.

    Returns a summary dict, or None when the object was skipped (not an error).
    Raises on genuine failure so the event is retried and ultimately dead-lettered.
    """
    # Defence in depth. The S3 notification filter should mean we never see these,
    # but a widened filter must not be able to start a compress-the-compressed loop.
    if not key.startswith(SOURCE_PREFIX):
        _log("skipped_outside_source_prefix", bucket=bucket, key=key)
        return None

    # Console "folders" and multipart placeholders arrive as zero-byte keys.
    if key.endswith("/"):
        _log("skipped_directory_marker", bucket=bucket, key=key)
        return None

    destination_key = archive_key_for(key)

    try:
        source = _s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in ("NoSuchKey", "404"):
            # S3 notifications are at-least-once, so a redelivered event for an
            # object we already archived and deleted is expected. Treating this as
            # success is what stops it retrying until it dead-letters.
            _log("skipped_already_processed", bucket=bucket, key=key)
            return None
        raise

    original_bytes = source["ContentLength"]
    member_name = os.path.basename(key)

    buffer = tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES)
    try:
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=COMPRESSION_LEVEL,
        ) as archive:
            # force_zip64 because the final size is unknown while streaming, and
            # members above 2 GiB need the ZIP64 extensions.
            with archive.open(member_name, mode="w", force_zip64=True) as member:
                body = source["Body"]
                while chunk := body.read(CHUNK_BYTES):
                    member.write(chunk)

        compressed_bytes = buffer.tell()
        buffer.seek(0)

        _s3.upload_fileobj(
            buffer,
            bucket,
            destination_key,
            ExtraArgs={
                "ContentType": "application/zip",
                "Metadata": {
                    "source-key": key,
                    "original-bytes": str(original_bytes),
                },
            },
        )
    finally:
        buffer.close()

    # Verify independently of the upload call before destroying the only copy.
    # upload_fileobj returns nothing on success, so a HEAD is the cheapest way to
    # confirm the archive genuinely landed and is not empty.
    verified = _s3.head_object(Bucket=bucket, Key=destination_key)
    if verified["ContentLength"] <= 0:
        raise RuntimeError(
            f"Archive {destination_key} verified as empty; refusing to delete {key}"
        )

    _s3.delete_object(Bucket=bucket, Key=key)

    ratio = 1 - (compressed_bytes / original_bytes) if original_bytes else 0.0
    summary = {
        "bucket": bucket,
        "source_key": key,
        "archive_key": destination_key,
        "original_bytes": original_bytes,
        "compressed_bytes": compressed_bytes,
        "compression_ratio": round(ratio, 4),
    }
    _log("archived", **summary)
    return summary


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Entry point. Processes every record in the S3 notification event."""
    records = event.get("Records", [])
    _log("invoked", record_count=len(records))

    results = []
    for record in records:
        bucket = record["s3"]["bucket"]["name"]
        # S3 URL-encodes keys in notifications; spaces arrive as '+'.
        key = unquote_plus(record["s3"]["object"]["key"])

        summary = compress_object(bucket, key)
        if summary is not None:
            results.append(summary)

    return {"archived": len(results), "results": results}
