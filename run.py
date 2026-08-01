#!/usr/bin/env python3
"""Project commands.

    python3 run.py <command>        or        uv run run.py <command>

Standard library only, so it runs with no setup. Everything here either cannot be
done by CI - bootstrap creates the role CI authenticates with - or is an operator
command you want while looking at a live stack.

Deploying and rolling back are normally CI's job (.github/workflows). They exist
here for the first deploy, before CI has anything to authenticate with, and for
the case where CI itself is the thing that is broken.

Configure with STACK, REGION and PROFILE environment variables.
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import zipfile

STACK = os.environ.get("STACK", "s3-zip-archiver")
BOOTSTRAP_STACK = os.environ.get("BOOTSTRAP_STACK", f"{STACK}-bootstrap")
REGION = os.environ.get("REGION", "ap-southeast-1")
PROFILE = os.environ.get("PROFILE", "sk8jx")
ECR_REPO = os.environ.get("ECR_REPO", "s3-zip-archiver")

VENV = ".venv/bin"
COMMANDS: dict[str, callable] = {}


def command(fn):
    COMMANDS[fn.__name__.replace("_", "-")] = fn
    return fn


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def run(*args: str, capture: bool = False, check: bool = True) -> str:
    """Run a command, echoing it unless we only want its output."""
    if not capture:
        print(f"$ {' '.join(args)}", flush=True)
    result = subprocess.run(args, capture_output=capture, text=True)
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr.strip(), file=sys.stderr)
        sys.exit(result.returncode)
    return (result.stdout or "").strip()


def aws(*args: str, capture: bool = True, check: bool = True) -> str:
    return run(
        "aws", *args, "--region", REGION, "--profile", PROFILE,
        capture=capture, check=check,
    )


def stack_output(key: str) -> str:
    return aws(
        "cloudformation", "describe-stacks", "--stack-name", STACK,
        "--query", f"Stacks[0].Outputs[?OutputKey=='{key}'].OutputValue",
        "--output", "text",
    )


def account_id() -> str:
    return aws("sts", "get-caller-identity", "--query", "Account", "--output", "text")


def git_sha() -> str:
    return run("git", "rev-parse", "--short", "HEAD", capture=True)


def deploy_params() -> list[str]:
    """Every deploy passes the complete parameter set.

    SAM keeps the stack's previous value for anything omitted, so a single ad-hoc
    override sticks forever while the repository still shows the default.
    """
    with open("deploy.params") as handle:
        return [
            line.strip() for line in handle
            if line.strip() and not line.lstrip().startswith("#")
        ]


# ---------------------------------------------------------------------------
# development
# ---------------------------------------------------------------------------
@command
def install() -> None:
    """Create the dev virtualenv and install tooling."""
    # --clear so re-running is not an error. Without it uv refuses when .venv
    # already exists, which makes this work exactly once per clone.
    run("uv", "venv", "--clear", "--python", "3.12", ".venv")
    run("uv", "pip", "install", "--python", f"{VENV}/python", "-r", "requirements-dev.txt")


@command
def check() -> None:
    """Lint, test and validate. No AWS credentials needed."""
    run(f"{VENV}/ruff", "check", "src/", "tests/", "scripts/", "run.py")
    run(f"{VENV}/cfn-lint", "template.yaml", "bootstrap.yaml")
    run(f"{VENV}/pytest", "-q")
    run("sam", "validate", "--lint", "--region", REGION, "--profile", PROFILE)


@command
def costs() -> None:
    """Recompute the cost analysis. No AWS credentials needed."""
    run(sys.executable, "scripts/cost_model.py")


@command
def prices() -> None:
    """Re-verify the cost model's prices against the AWS Price List API."""
    run(sys.executable, "scripts/fetch_prices.py", "--profile", PROFILE)


# ---------------------------------------------------------------------------
# deployment
# ---------------------------------------------------------------------------
@command
def bootstrap() -> None:
    """Deploy the one-time ECR and CI role stack.

    CI cannot do this: it creates the role CI authenticates with.
    """
    existing = aws(
        "iam", "list-open-id-connect-providers",
        "--query",
        "OpenIDConnectProviderList[?contains(Arn,"
        "'token.actions.githubusercontent.com')].Arn | [0]",
        "--output", "text",
    )
    arn = "" if existing == "None" else existing
    aws(
        "cloudformation", "deploy",
        "--template-file", "bootstrap.yaml",
        "--stack-name", BOOTSTRAP_STACK,
        "--capabilities", "CAPABILITY_NAMED_IAM",
        "--parameter-overrides", f"ExistingOidcProviderArn={arn}",
        capture=False,
    )


@command
def deploy() -> None:
    """Build, push and deploy. Publishes a new Lambda version.

    Normally CI's job. Here for the first deploy and for when CI is broken.
    """
    if run("git", "status", "--porcelain", capture=True) and not os.environ.get("ALLOW_DIRTY"):
        sys.exit(
            "Uncommitted changes. The image is tagged with the commit SHA, so\n"
            "deploying now would produce an image no commit describes.\n"
            "Commit first, or set ALLOW_DIRTY=1."
        )

    registry = f"{account_id()}.dkr.ecr.{REGION}.amazonaws.com"
    sha = git_sha()
    image = f"{registry}/{ECR_REPO}:{sha}"

    # ECR tags are immutable, so pushing a commit CI has already built fails with
    # "the tag is immutable". The image for a given commit is the same image, so
    # skipping is correct rather than a workaround. CI does the same check.
    exists = aws(
        "ecr", "describe-images",
        "--repository-name", ECR_REPO,
        "--image-ids", f"imageTag={sha}",
        check=False,
    )

    if exists:
        print(f"Image {sha} already in ECR, skipping build and push.")
    else:
        password = aws("ecr", "get-login-password")
        subprocess.run(
            ["docker", "login", "--username", "AWS", "--password-stdin", registry],
            input=password, text=True, check=True,
        )

        # oci-mediatypes=false is required: BuildKit emits OCI manifests by
        # default and Lambda rejects them with "The image manifest, config or
        # layer media type for the source image is not supported". Provenance and
        # SBOM attestations are off for the same reason - they make the push a
        # multi-manifest index.
        run(
            "docker", "buildx", "build",
            "--platform", "linux/amd64",
            "--provenance=false",
            "--sbom=false",
            "--output", f"type=image,name={image},oci-mediatypes=false,push=true",
            "src/",
        )

    run(
        "sam", "deploy",
        "--stack-name", STACK,
        "--capabilities", "CAPABILITY_IAM",
        "--parameter-overrides", f"ImageUri={image}", *deploy_params(),
        "--image-repository", f"{registry}/{ECR_REPO}",
        "--resolve-s3",
        "--no-confirm-changeset",
        "--no-fail-on-empty-changeset",
        "--region", REGION, "--profile", PROFILE,
    )
    outputs()


@command
def destroy() -> None:
    """Delete the application stack. The bucket is retained deliberately."""
    aws("cloudformation", "delete-stack", "--stack-name", STACK, capture=False)
    aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", STACK, capture=False)
    print(
        f"\nStack deleted. The bucket has DeletionPolicy: Retain and still exists.\n"
        f"Removing it is an explicit, irreversible action:\n"
        f"  aws s3 rb s3://{STACK}-{account_id()} --force "
        f"--profile {PROFILE} --region {REGION}"
    )


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
@command
def outputs() -> None:
    """Show stack outputs."""
    aws(
        "cloudformation", "describe-stacks", "--stack-name", STACK,
        "--query", "Stacks[0].Outputs[].[OutputKey,OutputValue]",
        "--output", "table", capture=False,
    )


@command
def config() -> None:
    """Compare the declared configuration against what is deployed."""
    print("Declared in deploy.params:")
    for param in deploy_params():
        print(f"  {param}")
    print("\nCurrently deployed:")
    deployed = aws(
        "cloudformation", "describe-stacks", "--stack-name", STACK,
        "--query", "Stacks[0].Parameters[?ParameterKey!='ImageUri'].[ParameterKey,ParameterValue]",
        "--output", "text",
    )
    for line in deployed.splitlines():
        key, _, value = line.partition("\t")
        print(f"  {key}={value}")


@command
def versions() -> None:
    """List published versions and what the live alias serves."""
    function = f"{STACK}-compressor"
    print("Published versions and the image each pins:")
    listed = aws(
        "lambda", "list-versions-by-function", "--function-name", function,
        "--query", "Versions[?Version!='$LATEST'].Version", "--output", "text",
    )
    for version in listed.split():
        image = aws(
            "lambda", "get-function", "--function-name", f"{function}:{version}",
            "--query", "Code.ImageUri", "--output", "text",
        )
        print(f"  v{version:<4} {image.rsplit(':', 1)[-1]}")
    serving = aws(
        "lambda", "get-alias", "--function-name", function, "--name", "live",
        "--query", "FunctionVersion", "--output", "text",
    )
    print(f"\nAlias 'live' serves version {serving}")


@command
def logs() -> None:
    """Tail the function's logs."""
    aws("logs", "tail", f"/aws/lambda/{STACK}-compressor", "--follow", capture=False)


@command
def dlq() -> None:
    """Show how many events failed permanently."""
    count = aws(
        "sqs", "get-queue-attributes",
        "--queue-url", stack_output("DeadLetterQueueUrl"),
        "--attribute-names", "ApproximateNumberOfMessages",
        "--query", "Attributes.ApproximateNumberOfMessages", "--output", "text",
    )
    print(f"Messages in the dead-letter queue: {count}")


@command
def drift() -> None:
    """Detect resources changed outside CloudFormation."""
    detection = aws(
        "cloudformation", "detect-stack-drift", "--stack-name", STACK,
        "--query", "StackDriftDetectionId", "--output", "text",
    )
    status = detected = count = ""
    for _ in range(40):
        status, detected, count = aws(
            "cloudformation", "describe-stack-drift-detection-status",
            "--stack-drift-detection-id", detection,
            "--query", "[DetectionStatus,StackDriftStatus,DriftedStackResourceCount]",
            "--output", "text",
        ).split("\t")
        if status != "DETECTION_IN_PROGRESS":
            break
        time.sleep(4)

    print(f"{status}  {detected}  drifted: {count}")
    if status != "DETECTION_COMPLETE":
        sys.exit("Drift detection did not complete, so nothing was verified.")
    if detected != "IN_SYNC":
        aws(
            "cloudformation", "describe-stack-resource-drifts", "--stack-name", STACK,
            "--stack-resource-drift-status-filters", "MODIFIED", "DELETED",
            "--query",
            "StackResourceDrifts[].[LogicalResourceId,ResourceType,"
            "StackResourceDriftStatus]",
            "--output", "text", capture=False,
        )
        print(
            "\nA redeploy will not fix this. CloudFormation compares templates, not\n"
            "reality, so an unchanged template produces an empty change set. See the\n"
            "drift section of README.md."
        )
        sys.exit(1)


@command
def rollback() -> None:
    """Roll back to a prior version: run.py rollback 8

    Prefer the Rollback workflow, which records who and why. This moves the alias
    directly, which is fast and leaves the stack drifted until you redeploy.
    """
    if len(sys.argv) < 3:
        sys.exit("Usage: python3 run.py rollback <version>")
    version = sys.argv[2]
    aws(
        "lambda", "update-alias",
        "--function-name", f"{STACK}-compressor",
        "--name", "live", "--function-version", version,
        "--query", "[Name,FunctionVersion]", "--output", "text", capture=False,
    )
    print(
        f"\nLive traffic now served by version {version}.\n"
        "The stack still records the newer version, so this shows as drift until\n"
        "you redeploy from the intended commit."
    )


# ---------------------------------------------------------------------------
# verification
# ---------------------------------------------------------------------------
@command
def smoke() -> None:
    """End-to-end test against the deployed stack.

    Checks the three things that matter: an archive was produced, the original
    was deleted, and the archive round-trips to the bytes that were uploaded. By
    the time this runs the original is gone, so an archive that exists but does
    not extract would be worse than none.
    """
    size_mb = float(os.environ.get("SIZE_MB", "10"))
    bucket = stack_output("BucketName")
    source_prefix = stack_output("SourcePrefix") or "incoming/"
    archive_prefix = stack_output("ArchivePrefix") or "archive/"
    if not bucket or bucket == "None":
        sys.exit(f"Could not read BucketName from stack {STACK}")

    print(f"Stack:  {STACK} ({REGION})\nBucket: {bucket}\n")

    name = f"smoke-{int(time.time())}.json"
    source_key = f"{source_prefix}{name}"
    archive_key = f"{archive_prefix}{name}.zip"
    local = f"/tmp/{name}"

    print(f"==> Generating a {size_mb:g} MB payload")
    run(sys.executable, "scripts/generate_sample.py",
        "--size-mb", str(size_mb), "--output", local)
    with open(local, "rb") as handle:
        sent = handle.read()

    print(f"==> Uploading to s3://{bucket}/{source_key}")
    aws("s3", "cp", local, f"s3://{bucket}/{source_key}", "--only-show-errors", capture=False)

    print("==> Waiting for the archive")
    started = time.time()
    while time.time() - started < 90:
        head = aws("s3api", "head-object", "--bucket", bucket, "--key", archive_key,
                   check=False)
        if head:
            break
        time.sleep(2)
    else:
        sys.exit(f"No archive at s3://{bucket}/{archive_key} after 90s. Try: run.py logs")
    elapsed = time.time() - started
    print(f"    appeared after ~{elapsed:.0f}s")

    print("==> Verifying the original was deleted")
    if aws("s3api", "head-object", "--bucket", bucket, "--key", source_key, check=False):
        sys.exit(f"Original still present at {source_key}")
    print("    original removed")

    print("==> Verifying the archive round-trips")
    aws("s3", "cp", f"s3://{bucket}/{archive_key}", "/tmp/smoke.zip",
        "--only-show-errors", capture=False)
    with open("/tmp/smoke.zip", "rb") as handle:
        archive_bytes = handle.read()
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        extracted = archive.read(archive.namelist()[0])
    if extracted != sent:
        sys.exit("Extracted contents differ from what was uploaded")
    print("    contents identical")

    reduction = (1 - len(archive_bytes) / len(sent)) * 100
    print(f"\n{'=' * 48}")
    print(f"original      {len(sent):>15,} bytes")
    print(f"archive       {len(archive_bytes):>15,} bytes")
    print(f"reduction     {reduction:>15.2f} %")
    print(f"latency       {elapsed:>15.0f} s")
    print("=" * 48)

    aws("s3", "rm", f"s3://{bucket}/{archive_key}", "--only-show-errors", capture=False)
    os.remove(local)
    print("\nPASS")


# ---------------------------------------------------------------------------
@command
def help() -> None:  # noqa: A001 - the command really is called "help"
    """Show this list."""
    print(__doc__.strip())
    print("\nCommands:\n")
    for name, fn in COMMANDS.items():
        summary = (fn.__doc__ or "").strip().splitlines()[0]
        print(f"  {name:<12} {summary}")


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "help"
    if name in ("-h", "--help"):
        name = "help"
    if name not in COMMANDS:
        print(f"Unknown command: {name}\n", file=sys.stderr)
        help()
        sys.exit(1)
    COMMANDS[name]()


if __name__ == "__main__":
    main()
