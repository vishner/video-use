"""Generate animated visual effect overlays for slo-mo compositing.

Produces a black-background MP4 designed for `blend=all_mode=screen` compositing:
black pixels are transparent, colored pixels add brightness to the base video.

Effects:
  sparkle   - animated 4-point star particles drifting upward with twinkling
  dreamlike - sparkle particles + bloom (bloom applied via ffmpeg in render.py)

The bloom effect itself is handled as a pure ffmpeg filter in render.py and
does not require a separate overlay file.

Usage:
    python helpers/sparkle_overlay.py --duration 10.2 --width 1920 --height 1080 -o sparkle.mp4
    python helpers/sparkle_overlay.py --duration 7.2 --effect dreamlike --particles 150 -o fx.mp4
"""

from __future__ import annotations

import argparse
import math
import subprocess
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


# -------- Palette ------------------------------------------------------------

SPARKLE_COLORS = [
    (255, 255, 255),   # pure white
    (255, 220, 120),   # warm gold
    (255, 240, 180),   # light gold
    (200, 220, 255),   # cool blue-white
    (255, 200, 230),   # soft pink-white
    (180, 255, 220),   # mint
]


# -------- Particle drawing ---------------------------------------------------


def _draw_star(
    draw: ImageDraw.ImageDraw,
    cx: int,
    cy: int,
    size: float,
    color: tuple,
    alpha: int,
) -> None:
    """Draw a 4-point + 4-diagonal star at (cx, cy)."""
    r, g, b = color
    col = (r, g, b, alpha)
    s = max(1, int(size))
    thin = max(1, s // 4)
    diag = int(s * 0.55)

    draw.line([(cx - s, cy), (cx + s, cy)], fill=col, width=thin)
    draw.line([(cx, cy - s), (cx, cy + s)], fill=col, width=thin)
    draw.line([(cx - diag, cy - diag), (cx + diag, cy + diag)], fill=col, width=max(1, thin - 1))
    draw.line([(cx + diag, cy - diag), (cx - diag, cy + diag)], fill=col, width=max(1, thin - 1))
    r2 = max(1, s // 3)
    draw.ellipse([(cx - r2, cy - r2), (cx + r2, cy + r2)], fill=col)


# -------- Particle system ----------------------------------------------------


def _init_particles(
    width: int,
    height: int,
    total_frames: int,
    fps: float,
    n: int,
    rng: np.random.Generator,
) -> list[dict]:
    """Create N particles with randomised properties and staggered birth times."""
    particles = []
    for _ in range(n):
        birth_frame = int(rng.uniform(0, total_frames * 0.75))
        lifetime_frames = int(rng.uniform(fps * 0.35, fps * 1.6))
        particles.append({
            "x": float(rng.uniform(0, width)),
            "y": float(rng.uniform(height * 0.1, height * 0.95)),
            "vx": float(rng.uniform(-0.7, 0.7)),
            "vy": float(rng.uniform(-1.8, -0.25)),   # drift upward
            "size": float(rng.uniform(3, 13)),
            "color": SPARKLE_COLORS[int(rng.integers(0, len(SPARKLE_COLORS)))],
            "birth_frame": birth_frame,
            "lifetime_frames": lifetime_frames,
            "twinkle_phase": float(rng.uniform(0, 2 * math.pi)),
            "twinkle_speed": float(rng.uniform(0.12, 0.45)),
            "max_alpha": float(rng.uniform(0.45, 1.0)),
        })
    return particles


def _render_sparkle_frame(
    width: int,
    height: int,
    frame_idx: int,
    particles: list[dict],
) -> Image.Image:
    """Render one RGBA frame of sparkle particles."""
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    for p in particles:
        age = frame_idx - p["birth_frame"]
        if age < 0 or age > p["lifetime_frames"]:
            continue
        # Fade in + fade out: peak brightness at 50% of lifetime
        life_frac = age / p["lifetime_frames"]
        fade = 1.0 - abs(life_frac - 0.5) * 2.0
        if fade <= 0:
            continue
        twinkle = 0.5 + 0.5 * math.sin(p["twinkle_phase"] + age * p["twinkle_speed"])
        alpha = int(255 * fade * twinkle * p["max_alpha"])
        if alpha < 4:
            continue
        cx = int(p["x"] + p["vx"] * age)
        cy = int(p["y"] + p["vy"] * age)
        if cx < -20 or cx > width + 20 or cy < -20 or cy > height + 20:
            continue
        size = p["size"] * (0.55 + 0.45 * fade)
        _draw_star(draw, cx, cy, size, p["color"], alpha)

    return img


# -------- Render pipeline ----------------------------------------------------


def render_overlay(
    width: int,
    height: int,
    duration: float,
    fps: float,
    effect: str,
    out_path: Path,
    n_particles: int = 120,
    seed: int = 42,
) -> None:
    """Render the effect overlay to a screen-blend-ready black-background MP4.

    effect: "sparkle" or "dreamlike" (both produce a particle file;
    bloom is handled separately as a pure ffmpeg filter in render.py).
    """
    total_frames = max(1, int(math.ceil(duration * fps)))
    rng = np.random.default_rng(seed)
    particles = _init_particles(width, height, total_frames, fps, n_particles, rng)

    ffmpeg_cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-f", "rawvideo",
        "-pixel_format", "rgb24",
        "-video_size", f"{width}x{height}",
        "-framerate", str(fps),
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(out_path),
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        for f_idx in range(total_frames):
            rgba = _render_sparkle_frame(width, height, f_idx, particles)
            # Flatten RGBA onto black background -> RGB (black = transparent for screen blend)
            bg = Image.new("RGB", (width, height), (0, 0, 0))
            bg.paste(rgba, mask=rgba.split()[3])   # use alpha as mask
            proc.stdin.write(bg.tobytes())
    finally:
        proc.stdin.close()
        proc.wait()

    if proc.returncode != 0:
        stderr_text = proc.stderr.read().decode(errors="replace")
        raise RuntimeError(f"ffmpeg failed rendering overlay:\n{stderr_text[:600]}")


# -------- CLI ----------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate sparkle/dreamlike overlay for slo-mo compositing"
    )
    ap.add_argument("--duration", type=float, required=True, help="Duration in seconds")
    ap.add_argument("--fps", type=float, default=24.0)
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument(
        "--effect",
        choices=["sparkle", "dreamlike"],
        default="dreamlike",
        help="Effect type (default: dreamlike)",
    )
    ap.add_argument("--particles", type=int, default=120, help="Number of particles")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("-o", "--output", type=Path, required=True)
    args = ap.parse_args()

    print(f"Rendering {args.effect} overlay: {args.duration:.1f}s"
          f" @ {args.fps}fps  {args.width}x{args.height}")
    render_overlay(
        args.width, args.height,
        args.duration, args.fps,
        args.effect, args.output,
        args.particles, args.seed,
    )
    kb = args.output.stat().st_size // 1024
    print(f"  saved: {args.output} ({kb} KB)")


if __name__ == "__main__":
    main()
