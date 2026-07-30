"""Shared pytest fixtures.

The handler builds its boto3 client at import time, which is correct for Lambda -
the client is then reused across warm invocations instead of being rebuilt on every
request. It does mean the module has to be imported while moto's mock is already
active, hence the reload inside the fixture rather than a plain import at the top.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))

BUCKET = "test-archiver-bucket"
REGION = "ap-southeast-1"


@pytest.fixture
def aws_credentials(monkeypatch):
    """Fake credentials, so a misconfigured mock can never reach real AWS."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@pytest.fixture
def s3(aws_credentials):
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield client


@pytest.fixture
def app(s3):
    """The handler module, imported against the active mock."""
    import app as app_module

    return importlib.reload(app_module)


def s3_event(bucket: str, key: str) -> dict:
    """Minimal S3 notification event, matching the shape Lambda receives."""
    return {
        "Records": [
            {
                "eventName": "ObjectCreated:Put",
                "s3": {
                    "bucket": {"name": bucket},
                    "object": {"key": key},
                },
            }
        ]
    }
