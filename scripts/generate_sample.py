#!/usr/bin/env python3
"""Generate a representative video-processing result JSON.

The brief describes on-premises video processing that emits JSON results averaging
10 MB, which are then exported to S3. The compression ratio in the cost analysis
depends entirely on what that JSON actually looks like, so rather than assuming a
figure this produces a realistic payload - per-frame detections, scene boundaries,
audio levels, transcript segments - and the smoke test measures the real ratio
achieved against it.

Structurally repetitive records like these are what DEFLATE handles best, which is
precisely why compressing them is worth doing.

Usage:
    python3 scripts/generate_sample.py --size-mb 10 --output sample.json
"""

from __future__ import annotations

import argparse
import json
import random

LABELS = [
    "person", "car", "bicycle", "traffic_light", "dog", "handbag",
    "backpack", "bottle", "chair", "laptop", "cell_phone", "bus",
]
SCENE_TYPES = ["interior", "exterior", "aerial", "close_up", "wide_shot"]
CODECS = ["h264", "h265", "av1", "vp9"]


def build_frame(index: int, rng: random.Random) -> dict:
    timestamp = round(index / 29.97, 4)
    return {
        "frame_index": index,
        "timestamp_seconds": timestamp,
        "keyframe": index % 48 == 0,
        "detections": [
            {
                "label": rng.choice(LABELS),
                "confidence": round(rng.uniform(0.42, 0.999), 4),
                "bounding_box": {
                    "x": round(rng.uniform(0, 1920), 2),
                    "y": round(rng.uniform(0, 1080), 2),
                    "width": round(rng.uniform(20, 400), 2),
                    "height": round(rng.uniform(20, 400), 2),
                },
                "tracking_id": rng.randint(1, 500),
            }
            for _ in range(rng.randint(1, 5))
        ],
        "audio": {
            "peak_db": round(rng.uniform(-60, -3), 2),
            "rms_db": round(rng.uniform(-70, -12), 2),
        },
        "quality": {
            "blur_score": round(rng.uniform(0, 1), 4),
            "exposure": round(rng.uniform(-2, 2), 3),
        },
    }


def generate(target_bytes: int, seed: int = 42) -> dict:
    rng = random.Random(seed)

    document = {
        "schema_version": "2.4.0",
        "asset": {
            "asset_id": "a7f3c2e1-9b4d-4c8a-b6e5-1d2f3a4b5c6d",
            "source_filename": "master_4k_prores.mov",
            "duration_seconds": 0,
            "resolution": {"width": 3840, "height": 2160},
            "framerate": 29.97,
            "codec": rng.choice(CODECS),
            "bitrate_kbps": 88000,
        },
        "processing": {
            "pipeline": "video-analysis-v2",
            "node": "onprem-render-14",
            "started_at": "2026-07-31T02:14:07Z",
            "models": ["yolov8x", "whisper-large-v3", "scene-detect-1.2"],
        },
        "scenes": [],
        "transcript": [],
        "frames": [],
    }

    # Grow the frame list until the serialised document reaches the target size.
    # Checked in batches because json.dumps on a large structure is not cheap.
    index = 0
    while True:
        for _ in range(500):
            document["frames"].append(build_frame(index, rng))
            index += 1

        if index % 3000 == 0:
            document["scenes"].append({
                "scene_index": len(document["scenes"]),
                "start_frame": max(0, index - 3000),
                "end_frame": index,
                "scene_type": rng.choice(SCENE_TYPES),
                "dominant_colours": [
                    f"#{rng.randint(0, 0xFFFFFF):06x}" for _ in range(5)
                ],
            })
            document["transcript"].append({
                "start_seconds": round((index - 3000) / 29.97, 3),
                "end_seconds": round(index / 29.97, 3),
                "speaker": f"SPEAKER_{rng.randint(0, 4):02d}",
                "text": " ".join(
                    rng.choice([
                        "the", "camera", "pans", "across", "the", "scene",
                        "revealing", "a", "wide", "exterior", "shot", "of",
                        "the", "city", "at", "dusk",
                    ])
                    for _ in range(rng.randint(8, 24))
                ),
                "confidence": round(rng.uniform(0.7, 0.99), 3),
            })

        if len(json.dumps(document)) >= target_bytes:
            break

    document["asset"]["duration_seconds"] = round(index / 29.97, 2)
    document["frame_count"] = index
    return document


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--size-mb", type=float, default=10.0)
    parser.add_argument("--output", default="sample.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    document = generate(int(args.size_mb * 1024 * 1024), seed=args.seed)

    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(document, handle)

    import os
    size = os.path.getsize(args.output)
    print(f"{args.output}: {size:,} bytes ({size / 1024 / 1024:.2f} MB), "
          f"{document['frame_count']:,} frames")


if __name__ == "__main__":
    main()
