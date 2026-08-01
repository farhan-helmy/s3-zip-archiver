#!/usr/bin/env python3
"""Cost model for the S3 ZIP archiver at the volume described in the brief.

Every figure in the cost analysis section of README.md is produced by this
script, so the arithmetic can be checked and re-run rather than taken on trust.

Prices are ap-southeast-1 on-demand, pulled from the AWS Pricing API
(`aws pricing get-products --region us-east-1 --service-code ...`) in July 2026.
Compression ratio and Lambda duration are measured from the deployed stack, not
assumed - see the smoke command in run.py.

Usage:
    python3 scripts/cost_model.py
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Prices: ap-southeast-1, on-demand, USD. Source: AWS Pricing API, July 2026.
# ---------------------------------------------------------------------------
S3_STANDARD_TIERS = [        # (GB up to, $/GB-month)
    (50_000, 0.025),         # first 50 TB
    (500_000, 0.024),        # next 450 TB
    (float("inf"), 0.023),   # over 500 TB
]
S3_GLACIER_IR_PER_GB = 0.005
S3_STANDARD_IA_PER_GB = 0.0138

S3_PUT = 0.005 / 1_000            # PUT/COPY/POST/LIST, Standard
S3_GET = 0.004 / 10_000           # GET and all other, Standard
S3_PUT_GLACIER_IR = 0.02 / 1_000  # 4x the Standard PUT price
S3_DELETE = 0.0                   # DELETE requests are not charged

LAMBDA_GB_SECOND = 0.0000166667   # tier 1: first 6bn GB-s/month
LAMBDA_REQUEST = 0.0000002

NAT_GATEWAY_HOUR = 0.059
NAT_GATEWAY_PER_GB = 0.059

CW_LOGS_INGEST_PER_GB = 0.70
CW_ALARM_MONTH = 0.10

# ---------------------------------------------------------------------------
# Workload, from the brief.
# ---------------------------------------------------------------------------
FILES_PER_HOUR = 1_000_000
AVG_FILE_MB = 10
HOURS_PER_MONTH = 730             # AWS's standard month

# ---------------------------------------------------------------------------
# Measured on the deployed stack. See README "Measured, not assumed".
# ---------------------------------------------------------------------------
MEASURED_ORIGINAL_BYTES = 10_753_870
MEASURED_COMPRESSED_BYTES = 1_808_200
LAMBDA_MEMORY_GB = 1.0
LAMBDA_SECONDS_PER_FILE = 0.758   # warm duration for a 10.26 MB payload
LOG_BYTES_PER_INVOCATION = 450    # three structured JSON lines plus REPORT

COMPRESSED_FRACTION = MEASURED_COMPRESSED_BYTES / MEASURED_ORIGINAL_BYTES
REDUCTION = 1 - COMPRESSED_FRACTION

FILES_PER_MONTH = FILES_PER_HOUR * HOURS_PER_MONTH
INGEST_GB_PER_MONTH = FILES_PER_MONTH * AVG_FILE_MB / 1_000
ARCHIVE_GB_PER_MONTH = INGEST_GB_PER_MONTH * COMPRESSED_FRACTION


def tiered_storage_cost(gb: float, tiers=S3_STANDARD_TIERS) -> float:
    """S3 Standard is billed in tiers, so a flat rate overstates cost at scale."""
    cost = 0.0
    previous = 0.0
    for ceiling, rate in tiers:
        if gb <= previous:
            break
        billable = min(gb, ceiling) - previous
        cost += billable * rate
        previous = ceiling
    return cost


def money(value: float) -> str:
    return f"${value:,.2f}"


def row(label: str, value: float, note: str = "") -> str:
    return f"| {label} | {money(value)} | {note} |"


def main() -> None:
    print("=" * 78)
    print("MEASURED INPUTS")
    print("=" * 78)
    print(f"  sample original          {MEASURED_ORIGINAL_BYTES:>14,} bytes")
    print(f"  sample compressed        {MEASURED_COMPRESSED_BYTES:>14,} bytes")
    print(f"  reduction                {REDUCTION * 100:>14.2f} %")
    print(f"  lambda duration (warm)   {LAMBDA_SECONDS_PER_FILE * 1000:>14.0f} ms"
          f" at {LAMBDA_MEMORY_GB:.0f} GB")
    print()
    print("=" * 78)
    print("DERIVED MONTHLY VOLUME")
    print("=" * 78)
    print(f"  files / month            {FILES_PER_MONTH:>14,}")
    print(f"  ingested                 {INGEST_GB_PER_MONTH:>14,.0f} GB"
          f"  ({INGEST_GB_PER_MONTH / 1_000_000:.2f} PB)")
    print(f"  archived                 {ARCHIVE_GB_PER_MONTH:>14,.0f} GB"
          f"  ({ARCHIVE_GB_PER_MONTH / 1_000_000:.2f} PB)")
    print(f"  storage avoided          {INGEST_GB_PER_MONTH - ARCHIVE_GB_PER_MONTH:>14,.0f} GB")
    print()

    # ---- Cost of running the feature -------------------------------------
    lambda_compute = (
        FILES_PER_MONTH * LAMBDA_MEMORY_GB * LAMBDA_SECONDS_PER_FILE * LAMBDA_GB_SECOND
    )
    lambda_requests = FILES_PER_MONTH * LAMBDA_REQUEST
    s3_get = FILES_PER_MONTH * S3_GET
    s3_put = FILES_PER_MONTH * S3_PUT
    s3_delete = FILES_PER_MONTH * S3_DELETE
    logs = FILES_PER_MONTH * LOG_BYTES_PER_INVOCATION / 1e9 * CW_LOGS_INGEST_PER_GB
    alarms = 3 * CW_ALARM_MONTH
    feature_total = (
        lambda_compute + lambda_requests + s3_get + s3_put + s3_delete + logs + alarms
    )

    print("=" * 78)
    print("A. RECURRING COST OF RUNNING THE FEATURE (per month)")
    print("=" * 78)
    print("| Component | Cost | Basis |")
    print("|---|---:|---|")
    compute_basis = (
        f"{FILES_PER_MONTH:,} x {LAMBDA_SECONDS_PER_FILE}s x {LAMBDA_MEMORY_GB:.0f}GB"
    )
    print(row("Lambda compute", lambda_compute, compute_basis))
    print(row("Lambda requests", lambda_requests, f"{FILES_PER_MONTH:,} invocations"))
    print(row("S3 GET (read original)", s3_get, f"{FILES_PER_MONTH:,} requests"))
    print(row("S3 PUT (write archive)", s3_put, f"{FILES_PER_MONTH:,} requests"))
    print(row("S3 DELETE (remove original)", s3_delete, "DELETE is not charged"))
    print(row("CloudWatch Logs", logs, f"~{LOG_BYTES_PER_INVOCATION}B/invocation"))
    print(row("CloudWatch alarms", alarms, "3 alarms"))
    print(row("**Total**", feature_total, ""))
    print()

    # ---- Storage, with and without --------------------------------------
    storage_without = tiered_storage_cost(INGEST_GB_PER_MONTH)
    storage_with = tiered_storage_cost(ARCHIVE_GB_PER_MONTH)
    storage_saved = storage_without - storage_with

    print("=" * 78)
    print("B. STORAGE FOR ONE MONTH'S INGEST (recurs every month it is retained)")
    print("=" * 78)
    print("| Scenario | Cost | Basis |")
    print("|---|---:|---|")
    print(row("Uncompressed (today)", storage_without,
              f"{INGEST_GB_PER_MONTH:,.0f} GB S3 Standard"))
    print(row("Compressed", storage_with, f"{ARCHIVE_GB_PER_MONTH:,.0f} GB S3 Standard"))
    print(row("**Saved**", storage_saved, f"{REDUCTION * 100:.1f}% less data"))
    print()

    net = storage_saved - feature_total
    print("=" * 78)
    print("C. NET EFFECT")
    print("=" * 78)
    print(f"  storage saved            {money(storage_saved):>16}")
    print(f"  feature costs            {money(-feature_total):>16}")
    print(f"  NET SAVING / MONTH       {money(net):>16}")
    print(f"  ratio                    {storage_saved / feature_total:>15.1f}x return")
    print()

    # ---- What a NAT Gateway would have cost -------------------------------
    nat_gb = INGEST_GB_PER_MONTH + ARCHIVE_GB_PER_MONTH  # reads plus writes
    nat_data = nat_gb * NAT_GATEWAY_PER_GB
    nat_hours = 2 * HOURS_PER_MONTH * NAT_GATEWAY_HOUR   # one per AZ
    print("=" * 78)
    print("D. THE NETWORKING DECISION (S3 Gateway Endpoint vs NAT Gateway)")
    print("=" * 78)
    print("| Option | Cost | Basis |")
    print("|---|---:|---|")
    print(row("S3 Gateway Endpoint (chosen)", 0.0, "no hourly and no data charge"))
    print(row("NAT Gateway - data processing", nat_data,
              f"{nat_gb:,.0f} GB x ${NAT_GATEWAY_PER_GB}/GB"))
    print(row("NAT Gateway - hourly", nat_hours, f"2 AZs x {HOURS_PER_MONTH}h"))
    print(row("**NAT total**", nat_data + nat_hours, "for traffic that never leaves AWS"))
    print("\n  Choosing the endpoint over a NAT Gateway avoids"
          f" {money(nat_data + nat_hours)}/month,")
    print(f"  which alone exceeds the entire storage saving of {money(storage_saved)}.")
    print()

    # ---- Optimisations ----------------------------------------------------
    print("=" * 78)
    print("E. FURTHER OPTIMISATIONS")
    print("=" * 78)

    # Glacier Instant Retrieval, written directly.
    gir_storage = ARCHIVE_GB_PER_MONTH * S3_GLACIER_IR_PER_GB
    gir_put_premium = FILES_PER_MONTH * (S3_PUT_GLACIER_IR - S3_PUT)
    gir_net = (storage_with - gir_storage) - gir_put_premium

    print("\n-- 1. Write archives straight to Glacier Instant Retrieval --")
    print(f"  storage in Standard      {money(storage_with):>16}")
    print(f"  storage in Glacier IR    {money(gir_storage):>16}")
    print(f"  PUT premium (4x price)   {money(-gir_put_premium):>16}")
    print(f"  net saving               {money(gir_net):>16}")
    print("  Note: the 4x PUT premium consumes roughly half the storage saving,")
    print("  because at this object count per-request charges dominate.")

    # Batching.
    for batch in (10, 100):
        batched_files = FILES_PER_MONTH / batch
        b_put = batched_files * S3_PUT
        b_get = FILES_PER_MONTH * S3_GET       # every original is still read
        b_req = batched_files * LAMBDA_REQUEST
        b_logs = batched_files * LOG_BYTES_PER_INVOCATION / 1e9 * CW_LOGS_INGEST_PER_GB
        # Compute is proportional to bytes processed, so it barely moves.
        b_total = lambda_compute + b_req + b_get + b_put + b_logs + alarms
        print(f"\n-- 2. Batch {batch} objects per archive --")
        print(f"  invocations / month      {batched_files:>16,.0f}")
        print(f"  feature cost             {money(b_total):>16}")
        print(f"  saving vs per-object     {money(feature_total - b_total):>16}")

    # Batching plus Glacier IR: the per-request premium stops mattering.
    batch = 100
    batched_files = FILES_PER_MONTH / batch
    combo_put = batched_files * S3_PUT_GLACIER_IR
    combo_get = FILES_PER_MONTH * S3_GET
    combo_req = batched_files * LAMBDA_REQUEST
    combo_logs = batched_files * LOG_BYTES_PER_INVOCATION / 1e9 * CW_LOGS_INGEST_PER_GB
    combo_feature = lambda_compute + combo_req + combo_get + combo_put + combo_logs + alarms
    combo_storage = ARCHIVE_GB_PER_MONTH * S3_GLACIER_IR_PER_GB
    combo_net = storage_without - combo_storage - combo_feature

    print("\n-- 3. Batch 100 AND write to Glacier IR --")
    print(f"  feature cost             {money(combo_feature):>16}")
    print(f"  storage cost             {money(combo_storage):>16}")
    print(f"  total                    {money(combo_feature + combo_storage):>16}")
    print(f"  vs uncompressed today    {money(storage_without):>16}")
    print(f"  NET SAVING / MONTH       {money(combo_net):>16}")
    print(f"  reduction vs today       {combo_net / storage_without * 100:>15.1f} %")
    print()


if __name__ == "__main__":
    main()
