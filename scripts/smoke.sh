#!/usr/bin/env bash
#
# End-to-end verification against the deployed stack.
#
# Uploads a representative payload, waits for the pipeline to process it, and then
# checks the three things that actually matter:
#
#   1. An archive was produced.
#   2. The original was deleted - but only after the archive existed.
#   3. The archive's contents are byte-identical to what was uploaded. A ZIP that
#      exists but does not round-trip is worse than no ZIP at all, because the
#      original has already been deleted by that point.
#
# Also reports the measured compression ratio, which is the figure the cost
# analysis in README.md is built on.

set -euo pipefail

STACK="${STACK:-s3-zip-archiver}"
REGION="${REGION:-ap-southeast-1}"
PROFILE="${PROFILE:-sk8jx}"
SIZE_MB="${SIZE_MB:-10}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-90}"

AWS=(aws --region "$REGION" --profile "$PROFILE")

fail() { echo "FAIL: $*" >&2; exit 1; }

stack_output() {
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$STACK" \
    --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text
}

BUCKET="$(stack_output BucketName)"
SOURCE_PREFIX="$(stack_output SourcePrefix)"
ARCHIVE_PREFIX="$(stack_output ArchivePrefix)"
[[ -n "$BUCKET" && "$BUCKET" != "None" ]] || fail "could not read BucketName from stack $STACK"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

KEY_NAME="smoke-$(date +%s).json"
SOURCE_KEY="${SOURCE_PREFIX}${KEY_NAME}"
ARCHIVE_KEY="${ARCHIVE_PREFIX}${KEY_NAME}.zip"

echo "Stack:  $STACK ($REGION)"
echo "Bucket: $BUCKET"
echo

echo "==> Generating a ${SIZE_MB} MB representative payload"
python3 scripts/generate_sample.py --size-mb "$SIZE_MB" --output "$WORKDIR/$KEY_NAME"
ORIGINAL_BYTES="$(wc -c < "$WORKDIR/$KEY_NAME")"

echo "==> Uploading to s3://$BUCKET/$SOURCE_KEY"
"${AWS[@]}" s3 cp "$WORKDIR/$KEY_NAME" "s3://$BUCKET/$SOURCE_KEY" --only-show-errors

echo "==> Waiting for the archive to appear (timeout ${TIMEOUT_SECONDS}s)"
START="$(date +%s)"
until "${AWS[@]}" s3api head-object --bucket "$BUCKET" --key "$ARCHIVE_KEY" >/dev/null 2>&1; do
  ELAPSED=$(( $(date +%s) - START ))
  if (( ELAPSED > TIMEOUT_SECONDS )); then
    fail "no archive at s3://$BUCKET/$ARCHIVE_KEY after ${TIMEOUT_SECONDS}s. Check: make logs"
  fi
  sleep 2
done
ELAPSED=$(( $(date +%s) - START ))
echo "    archive appeared after ~${ELAPSED}s"

echo "==> Verifying the original was deleted"
if "${AWS[@]}" s3api head-object --bucket "$BUCKET" --key "$SOURCE_KEY" >/dev/null 2>&1; then
  fail "original still present at $SOURCE_KEY"
fi
echo "    original removed"

echo "==> Verifying the archive round-trips to the original bytes"
"${AWS[@]}" s3 cp "s3://$BUCKET/$ARCHIVE_KEY" "$WORKDIR/archive.zip" --only-show-errors
COMPRESSED_BYTES="$(wc -c < "$WORKDIR/archive.zip")"
unzip -q -o "$WORKDIR/archive.zip" -d "$WORKDIR/extracted"
if ! cmp -s "$WORKDIR/$KEY_NAME" "$WORKDIR/extracted/$KEY_NAME"; then
  fail "extracted contents differ from what was uploaded"
fi
echo "    contents identical"

RATIO="$(python3 -c "print(f'{(1 - $COMPRESSED_BYTES / $ORIGINAL_BYTES) * 100:.2f}')")"

echo
echo "==================== RESULT ===================="
printf 'original      %15s bytes\n' "$ORIGINAL_BYTES"
printf 'compressed    %15s bytes\n' "$COMPRESSED_BYTES"
printf 'reduction     %15s %%\n' "$RATIO"
printf 'latency       %15s s (upload to archive available)\n' "$ELAPSED"
echo "================================================"
echo
echo "Cleaning up the archive this run created"
"${AWS[@]}" s3 rm "s3://$BUCKET/$ARCHIVE_KEY" --only-show-errors
echo "PASS"
