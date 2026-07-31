"""Tests for the compressor handler.

These focus on the behaviours that would be expensive to get wrong rather than on
line coverage: the function deletes customer data, and it writes into the bucket it
reads from. The two failure modes worth guarding against are therefore losing an
object, and triggering itself forever.
"""

from __future__ import annotations

import io
import json
import zipfile

import pytest
from botocore.exceptions import ClientError
from conftest import BUCKET, s3_event

SAMPLE = json.dumps({"frames": [{"i": n, "label": "person"} for n in range(500)]}).encode()


def put_source(s3, key: str, body: bytes = SAMPLE) -> None:
    s3.put_object(Bucket=BUCKET, Key=key, Body=body)


def object_exists(s3, key: str) -> bool:
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


def test_object_is_compressed_and_original_deleted(s3, app):
    put_source(s3, "incoming/result.json")

    app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    assert object_exists(s3, "archive/result.json.zip")
    assert not object_exists(s3, "incoming/result.json")


def test_archive_round_trips_to_the_original_bytes(s3, app):
    put_source(s3, "incoming/result.json")

    app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    body = s3.get_object(Bucket=BUCKET, Key="archive/result.json.zip")["Body"].read()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        # The member is named after the original file, not the full key.
        assert archive.namelist() == ["result.json"]
        assert archive.read("result.json") == SAMPLE


def test_archive_is_actually_smaller(s3, app):
    put_source(s3, "incoming/result.json")

    result = app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    summary = result["results"][0]
    assert summary["compressed_bytes"] < summary["original_bytes"]
    assert 0 < summary["compression_ratio"] < 1


def test_nested_paths_are_preserved(s3, app):
    put_source(s3, "incoming/2026/07/result.json")

    app.lambda_handler(s3_event(BUCKET, "incoming/2026/07/result.json"), None)

    assert object_exists(s3, "archive/2026/07/result.json.zip")


def test_keys_outside_the_source_prefix_are_ignored(s3, app):
    """The recursion guard.

    Archives live under archive/. If the function ever processed one it would
    produce an archive of an archive, whose creation would trigger it again, and
    so on without limit. The S3 notification filter should mean this event never
    arrives; this asserts the handler refuses it even if the filter is widened.
    """
    put_source(s3, "archive/result.json.zip")

    result = app.lambda_handler(s3_event(BUCKET, "archive/result.json.zip"), None)

    assert result["archived"] == 0
    # Untouched: not re-compressed, and not deleted.
    assert object_exists(s3, "archive/result.json.zip")
    assert not object_exists(s3, "archive/result.json.zip.zip")


def test_redelivered_event_for_a_processed_object_is_not_an_error(s3, app):
    """S3 notifications are at-least-once.

    A duplicate delivery arrives after the original has already been archived and
    deleted. Raising here would send a successfully-processed object to the
    dead-letter queue and page someone for nothing.
    """
    put_source(s3, "incoming/result.json")
    app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    result = app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    assert result["archived"] == 0


def test_original_survives_when_archive_verification_fails(s3, app, monkeypatch):
    """The delete must be contingent on the archive genuinely being there.

    Simulates an upload that reports success but leaves a zero-byte object. The
    original is the only copy at that moment, so the handler must raise and leave
    it alone rather than complete and destroy it.
    """
    put_source(s3, "incoming/result.json")
    monkeypatch.setattr(app._s3, "head_object", lambda **_: {"ContentLength": 0})

    with pytest.raises(RuntimeError, match="refusing to delete"):
        app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    assert object_exists(s3, "incoming/result.json")


def test_directory_markers_are_ignored(s3, app):
    s3.put_object(Bucket=BUCKET, Key="incoming/subdir/", Body=b"")

    result = app.lambda_handler(s3_event(BUCKET, "incoming/subdir/"), None)

    assert result["archived"] == 0


def test_url_encoded_keys_are_decoded(s3, app):
    """S3 URL-encodes keys in notifications; spaces arrive as '+'."""
    put_source(s3, "incoming/my result.json")

    app.lambda_handler(s3_event(BUCKET, "incoming/my+result.json"), None)

    assert object_exists(s3, "archive/my result.json.zip")
    assert not object_exists(s3, "incoming/my result.json")


def test_multiple_records_in_one_event(s3, app):
    put_source(s3, "incoming/a.json")
    put_source(s3, "incoming/b.json")

    event = s3_event(BUCKET, "incoming/a.json")
    event["Records"].append(s3_event(BUCKET, "incoming/b.json")["Records"][0])

    result = app.lambda_handler(event, None)

    assert result["archived"] == 2
    assert object_exists(s3, "archive/a.json.zip")
    assert object_exists(s3, "archive/b.json.zip")


def test_archives_default_to_standard_storage(s3, app):
    put_source(s3, "incoming/result.json")

    app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    head = s3.head_object(Bucket=BUCKET, Key="archive/result.json.zip")
    # S3 omits the field entirely for STANDARD.
    assert head.get("StorageClass", "STANDARD") == "STANDARD"


def test_archives_honour_a_configured_storage_class(s3, monkeypatch):
    """Archives are write-once, read-rarely, so a colder class is the cheaper home.

    Configurable rather than hardcoded: GLACIER_IR carries a 90-day minimum
    billing duration, which is the wrong trade for anything short-lived.
    """
    import importlib

    import app as app_module

    monkeypatch.setenv("ARCHIVE_STORAGE_CLASS", "GLACIER_IR")
    app = importlib.reload(app_module)

    put_source(s3, "incoming/result.json")
    app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    head = s3.head_object(Bucket=BUCKET, Key="archive/result.json.zip")
    assert head["StorageClass"] == "GLACIER_IR"


def test_incompressible_data_is_stored_not_deflated(s3, app):
    """Already-compressed input must not be deflated.

    DEFLATE on high-entropy data produces output larger than its input, and the
    original is deleted afterwards - so compressing it would spend CPU to end up
    storing more bytes than before.
    """
    import os as _os

    random_bytes = _os.urandom(300_000)
    s3.put_object(Bucket=BUCKET, Key="incoming/photo.png", Body=random_bytes)

    result = app.lambda_handler(s3_event(BUCKET, "incoming/photo.png"), None)

    summary = result["results"][0]
    assert summary["method"] == "stored"
    # A stored entry carries only ZIP framing, so it must not balloon the way a
    # deflated one would.
    assert summary["compressed_bytes"] < summary["original_bytes"] * 1.01


def test_compressible_data_is_still_deflated(s3, app):
    put_source(s3, "incoming/result.json")

    result = app.lambda_handler(s3_event(BUCKET, "incoming/result.json"), None)

    summary = result["results"][0]
    assert summary["method"] == "deflated"
    assert summary["compression_ratio"] > 0.5


def test_stored_archives_still_extract(s3, app):
    """Storing rather than deflating must not change the archive's contract."""
    import os as _os

    payload = _os.urandom(200_000)
    s3.put_object(Bucket=BUCKET, Key="incoming/blob.bin", Body=payload)

    app.lambda_handler(s3_event(BUCKET, "incoming/blob.bin"), None)

    body = s3.get_object(Bucket=BUCKET, Key="archive/blob.bin.zip")["Body"].read()
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        assert archive.read("blob.bin") == payload
