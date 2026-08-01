#!/usr/bin/env python3
"""Fetch current AWS prices for the components the cost model uses.

The figures in scripts/cost_model.py are hardcoded so the model runs without
credentials and gives the same answer every time. This script queries the AWS
Price List API for the same items so those constants can be checked, and
re-checked later when prices move.

Prints a table, and flags anything that no longer matches cost_model.py.

    python3 scripts/fetch_prices.py --profile sk8jx

The Price List API is only available in us-east-1 and eu-central-1 regardless of
which region you are pricing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

REGION_NAME = "Asia Pacific (Singapore)"

# usagetype -> (label, expected value in cost_model.py, unit)
# usagetype is the stable key; descriptions get reworded by AWS, these do not.
EXPECTED = {
    "APS1-TimedStorage-ByteHrs": ("S3 Standard storage (tiered)", None, "GB-Mo"),
    "APS1-TimedStorage-SIA-ByteHrs": ("S3 Standard-IA storage", 0.0138, "GB-Mo"),
    "APS1-TimedStorage-GIR-ByteHrs": ("S3 Glacier Instant Retrieval", 0.005, "GB-Mo"),
    "APS1-Requests-Tier1": ("S3 PUT/COPY/POST/LIST", 0.000005, "request"),
    "APS1-Requests-Tier2": ("S3 GET and all other", 0.0000004, "request"),
    "APS1-Requests-GIR-Tier1": ("S3 PUT to Glacier IR", 0.00002, "request"),
    "APS1-Lambda-GB-Second": ("Lambda compute (tiered)", None, "GB-second"),
    "APS1-Request": ("Lambda requests", 0.0000002, "request"),
    "APS1-NatGateway-Hours": ("NAT Gateway hourly", 0.059, "hour"),
    "APS1-NatGateway-Bytes": ("NAT Gateway data processing", 0.059, "GB"),
}

SERVICES = {
    "AmazonS3": ["APS1-TimedStorage", "APS1-Requests"],
    "AWSLambda": ["APS1-Lambda-GB-Second", "APS1-Request"],
    "AmazonEC2": ["APS1-NatGateway"],
    "AmazonCloudWatch": ["APS1-DataProcessing-Bytes", "APS1-CW:AlarmMonitorUsage"],
}


def query(service: str, profile: str | None) -> list[dict]:
    """Ask the Price List API for every on-demand price in the region."""
    command = [
        "aws", "pricing", "get-products",
        "--service-code", service,
        "--region", "us-east-1",
        "--filters", f"Type=TERM_MATCH,Field=location,Value={REGION_NAME}",
        "--max-items", "500",
        "--output", "json",
    ]
    if profile:
        command += ["--profile", profile]

    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  ! {service}: {result.stderr.strip().splitlines()[-1]}", file=sys.stderr)
        return []

    prices = []
    for entry in json.loads(result.stdout).get("PriceList", []):
        product = json.loads(entry) if isinstance(entry, str) else entry
        usagetype = product["product"]["attributes"].get("usagetype", "")
        for term in product["terms"].get("OnDemand", {}).values():
            for dimension in term["priceDimensions"].values():
                value = float(dimension["pricePerUnit"]["USD"])
                if value == 0:
                    continue
                prices.append({
                    "usagetype": usagetype,
                    "price": value,
                    "unit": dimension["unit"],
                    "description": dimension["description"],
                })
    return prices


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", default=None, help="AWS profile to use")
    args = parser.parse_args()

    print(f"AWS Price List API - on-demand, {REGION_NAME}\n")

    found: dict[str, list[dict]] = {}
    for service, prefixes in SERVICES.items():
        for price in query(service, args.profile):
            if any(price["usagetype"].startswith(p) for p in prefixes):
                found.setdefault(price["usagetype"], []).append(price)

    print(f"{'usagetype':<34}{'price':>14}  unit")
    print("-" * 72)
    for usagetype in sorted(found):
        for price in sorted(found[usagetype], key=lambda p: -p["price"]):
            print(f"{usagetype:<34}{price['price']:>14.10f}  {price['unit']}")
            print(f"{'':<34}{price['description'][:60]}")

    # A verification script that reports success when it could not verify
    # anything is worse than no script, because the output gets believed.
    if not found:
        print(
            "\nNo prices returned. Nothing was verified.\n"
            "Usually expired credentials - try: aws sso login --profile <profile>",
            file=sys.stderr,
        )
        sys.exit(1)

    print("\nAgainst the constants in scripts/cost_model.py:\n")
    mismatches = 0
    unresolved = 0
    for usagetype, (label, expected, _unit) in EXPECTED.items():
        entries = found.get(usagetype)
        if not entries:
            unresolved += 1
            print(f"  ?  {label:<36} not returned by the API")
            continue
        if expected is None:
            values = ", ".join(f"{e['price']:g}" for e in sorted(entries, key=lambda p: -p["price"]))
            print(f"  -  {label:<36} tiered: {values}")
            continue
        actual = min(e["price"] for e in entries)
        if abs(actual - expected) < 1e-12:
            print(f"  ok {label:<36} {expected:g}")
        else:
            mismatches += 1
            print(f"  !! {label:<36} model has {expected:g}, API says {actual:g}")

    if mismatches:
        print(f"\n{mismatches} price(s) have moved. Update scripts/cost_model.py.")
    if unresolved:
        print(f"{unresolved} price(s) could not be checked at all.")
    if mismatches or unresolved:
        sys.exit(1)

    print("\nAll fixed prices match the model.")


if __name__ == "__main__":
    main()
