"""Detect segments where a subject moves closer to the camera.

Samples the video at a low frame rate, detects the dominant face or person
bounding box in each frame, and finds windows where the bounding box area
grows continuously -- indicating the subject is approaching.

Output JSON:
{
  "video": "...",
  "fps": 30.0,
  "duration": 88.5,
  "approach_segments": [
    {
      "start": 5.20,
      "end": 9.80,
      "confidence": 0.82,
      "growth_pct": 45.2,
      "suggested_speed": 0.5,
      "suggested_effect": "dreamlike"
    }
  ],
  "total_samples_with_detection": 42
}

Usage:
    python helpers/detect_approach.py <video>
    python helpers/detect_approach.py <video> --out result.json
    python helpers/detect_approach.py <video> --min-duration 1.5 --growth 0.20 --sample-fps 4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import cv2
    import numpy as np
    CV2_AVAILABLE = True
except ImportError:
    CV2_AVAILABLE = False


# -------- Detection helpers ---------------------------------------------------


def _get_dominant_bbox(
    frame,
    face_cascade,
    hog,
    min_face_px: int = 30,
) -> tuple | None:
    """Return (x, y, w, h) of the largest detected face or person, or None."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)

    # Try face detection first -- more precise for close-up talking-head footage
    faces = face_cascade.detectMultiScale(
        gray, scaleFactor=1.1, minNeighbors=4, minSize=(min_face_px, min_face_px)
    )
    if len(faces) > 0:
        areas = [w * h for (x, y, w, h) in faces]
        return tuple(faces[int(np.argmax(areas))])

    # Fall back to HOG person detector for full-body shots
    try:
        rects, _ = hog.detectMultiScale(
            frame, winStride=(8, 8), padding=(4, 4), scale=1.05
        )
        if len(rects) > 0:
            areas = [w * h for (x, y, w, h) in rects]
            return tuple(rects[int(np.argmax(areas))])
    except Exception:
        pass

    return None


def sample_bboxes(video_path: Path, sample_fps: float = 4.0) -> list[dict]:
    """Return list of {t, area, relative_area} sampled at sample_fps."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")

    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frame_w = cap.get(cv2.CAP_PROP_FRAME_WIDTH)
    frame_h = cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
    frame_area = frame_w * frame_h
    frame_interval = max(1, int(src_fps / sample_fps))

    face_cascade = cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
    )
    hog = cv2.HOGDescriptor()
    hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())

    samples: list[dict] = []
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break
        if frame_idx % frame_interval == 0:
            t = frame_idx / src_fps
            bbox = _get_dominant_bbox(frame, face_cascade, hog)
            if bbox is not None:
                area = int(bbox[2]) * int(bbox[3])
                rel = area / frame_area if frame_area > 0 else 0.0
                samples.append({"t": round(t, 3), "area": area, "relative_area": rel})
            else:
                samples.append({"t": round(t, 3), "area": None, "relative_area": None})
        frame_idx += 1

    cap.release()
    return samples


def smooth_areas(samples: list[dict], window: int = 2) -> list[dict]:
    """Median-smooth the area column to reduce detection noise."""
    areas = [s["area"] for s in samples]
    result = []
    for i, s in enumerate(samples):
        if s["area"] is None:
            result.append({**s, "area_smooth": None})
            continue
        lo = max(0, i - window)
        hi = min(len(areas), i + window + 1)
        vals = [v for v in areas[lo:hi] if v is not None]
        smoothed = float(np.median(vals)) if vals else None
        result.append({**s, "area_smooth": smoothed})
    return result


# -------- Approach segment detection -----------------------------------------


def find_approach_segments(
    samples: list[dict],
    min_duration: float = 1.5,
    growth_threshold: float = 0.20,
    min_relative_area: float = 0.005,
) -> list[dict]:
    """Find windows where bbox area grows >= growth_threshold over >= min_duration.

    Returns list of {start, end, growth_pct, confidence, suggested_speed,
    suggested_effect}.
    """
    valid = [
        s for s in samples
        if s.get("area_smooth") is not None
        and s.get("relative_area") is not None
        and s["relative_area"] >= min_relative_area
    ]

    if len(valid) < 3:
        return []

    segments: list[dict] = []
    i = 0

    while i < len(valid) - 1:
        anchor = valid[i]
        best_j = None
        best_growth = 0.0

        for j in range(i + 1, len(valid)):
            dt = valid[j]["t"] - anchor["t"]
            if dt < min_duration:
                continue
            if valid[j]["area_smooth"] is None:
                continue
            growth = (valid[j]["area_smooth"] - anchor["area_smooth"]) / max(
                anchor["area_smooth"], 1.0
            )
            if growth >= growth_threshold and growth > best_growth:
                best_growth = growth
                best_j = j

        if best_j is not None:
            # Extend to the local peak (keep going while still growing)
            while (
                best_j + 1 < len(valid)
                and valid[best_j + 1].get("area_smooth") is not None
                and valid[best_j + 1]["area_smooth"] >= valid[best_j]["area_smooth"]
            ):
                g = (valid[best_j + 1]["area_smooth"] - anchor["area_smooth"]) / max(
                    anchor["area_smooth"], 1.0
                )
                if g > best_growth:
                    best_growth = g
                best_j += 1

            confidence = min(1.0, best_growth / (growth_threshold * 3.0))
            segments.append({
                "start": round(anchor["t"], 3),
                "end": round(valid[best_j]["t"], 3),
                "growth_pct": round(best_growth * 100.0, 1),
                "confidence": round(confidence, 2),
                "suggested_speed": 0.5,
                "suggested_effect": "dreamlike",
            })
            i = best_j + 1
        else:
            i += 1

    return segments


# -------- Top-level analysis --------------------------------------------------


def analyze(
    video_path: Path,
    min_duration: float = 1.5,
    growth_threshold: float = 0.20,
    sample_fps: float = 4.0,
) -> dict:
    samples = sample_bboxes(video_path, sample_fps=sample_fps)
    smoothed = smooth_areas(samples)
    segments = find_approach_segments(
        smoothed,
        min_duration=min_duration,
        growth_threshold=growth_threshold,
    )
    detected = sum(1 for s in samples if s["area"] is not None)
    fps_probe = 30.0
    dur = samples[-1]["t"] if samples else 0.0

    return {
        "video": str(video_path),
        "fps": fps_probe,
        "duration": dur,
        "approach_segments": segments,
        "total_samples_with_detection": detected,
    }


# -------- CLI -----------------------------------------------------------------


def main() -> None:
    if not CV2_AVAILABLE:
        sys.exit(
            "detect_approach.py requires opencv-python.\n"
            "Install with: pip install opencv-python"
        )

    ap = argparse.ArgumentParser(
        description="Detect camera-approach segments in a video."
    )
    ap.add_argument("video", type=Path, help="Source video file")
    ap.add_argument("--out", type=Path, default=None, help="Output JSON path")
    ap.add_argument(
        "--min-duration", type=float, default=1.5,
        help="Min approach duration in seconds (default: 1.5)",
    )
    ap.add_argument(
        "--growth", type=float, default=0.20,
        help="Min subject area growth fraction, e.g. 0.20 = 20%% (default: 0.20)",
    )
    ap.add_argument(
        "--sample-fps", type=float, default=4.0,
        help="Frames per second to sample (default: 4.0)",
    )
    args = ap.parse_args()

    if not args.video.exists():
        sys.exit(f"video not found: {args.video}")

    print(f"Detecting approach segments: {args.video.name}")
    print(f"  sample fps: {args.sample_fps}  min duration: {args.min_duration}s"
          f"  growth threshold: {args.growth * 100:.0f}%")

    result = analyze(args.video, args.min_duration, args.growth, args.sample_fps)
    segs = result["approach_segments"]

    print(f"  sampled frames with detection: {result['total_samples_with_detection']}")
    print(f"  approach segments found: {len(segs)}")
    for s in segs:
        print(
            f"    [{s['start']:6.2f} - {s['end']:6.2f}s]"
            f"  growth: {s['growth_pct']:5.1f}%"
            f"  confidence: {s['confidence']:.2f}"
            f"  -> speed {s['suggested_speed']}  effect: {s['suggested_effect']}"
        )

    if not segs:
        print("  (no approach segments detected -- try lowering --growth or --min-duration)")

    out_path = args.out or args.video.parent / f"{args.video.stem}_approach.json"
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  JSON saved -> {out_path}")


if __name__ == "__main__":
    main()
