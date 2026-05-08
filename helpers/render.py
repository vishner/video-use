"""Render a video from an EDL.

Implements the pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in
     (optional: slow-motion via setpts + atempo, per range)
  2. Lossless -c copy concat into base.mp4
  3. Final composite:
     - overlay animations (PTS-shifted)
     - mix background music and/or sound effects into audio
     - apply `subtitles` filter LAST -> final.mp4
  4. Two-pass loudness normalization (-14 LUFS / -1 dBTP / LRA 11)

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles

New EDL fields (all optional, backward-compatible):
  ranges[].speed                  float, default 1.0      slow-motion factor (<1 = slower, e.g. 0.5 = 2x slo-mo)
  ranges[].effect                 str,  default ""        visual effect: "bloom" | "sparkle" | "dreamlike"
  ranges[].effect_opacity         float, default 0.7      sparkle overlay screen-blend opacity (0.0-1.0)
  background_music                object                  {file, volume, fade_in, fade_out, loop, duck_narration, ...}
  background_music.duck_narration bool, default false     auto-duck music when narration is present
  background_music.duck_threshold float, default 0.02     sidechaincompress threshold (linear amplitude)
  background_music.duck_attack_ms float, default 200      duck attack time in milliseconds
  background_music.duck_release_ms float, default 800     duck release time in milliseconds
  sound_effects                   array                   [{file, start_in_output, volume}, ...]
  subtitles_enabled               bool, default true      set false to disable all subtitle processing
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

try:
    from grade import get_preset, auto_grade_for_clip  # same directory
except Exception:
    def get_preset(name: str) -> str:
        return ""

    def auto_grade_for_clip(video, start=0.0, duration=None, verbose=False):  # type: ignore
        return "eq=contrast=1.03:saturation=0.98", {}


# -------- Subtitle style (bold-overlay, proven at 1920x1080 and 1080x1920) ---
#
# MarginV=90 lands the caption baseline ~30% up from the bottom on any aspect
# ratio -- clear of TikTok/Reels/Shorts UI on every major vertical platform.
# Do not drop below ~75 without a specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- HDR -> SDR tone mapping (HLG / PQ sources) -------------------------
HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)

# -------- Helpers ------------------------------------------------------------


def run(cmd: list[str], quiet: bool = False) -> None:
    if not quiet:
        print(f"  $ {' '.join(str(c) for c in cmd[:6])}{' ...' if len(cmd) > 6 else ''}")
    subprocess.run(cmd, check=True)


def run_ffmpeg(cmd: list[str], context: str) -> None:
    """Run an ffmpeg command, capturing stderr. On failure, print a useful
    excerpt of stderr along with the failing command and exit non-zero.

    Use this anywhere we'd previously call ``subprocess.run(..., check=True,
    stderr=subprocess.PIPE)`` and silently lose the error context.
    """
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if proc.returncode == 0:
        return
    err = (proc.stderr or b"").decode(errors="replace")
    tail = "\n".join(err.strip().splitlines()[-20:]) or "(no stderr)"
    print(f"\nffmpeg failed during: {context}", file=sys.stderr)
    print(f"  cmd: {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    print(f"  stderr (last 20 lines):\n{tail}", file=sys.stderr)
    sys.exit(proc.returncode or 1)


def get_video_duration(path: Path) -> float:
    """Return duration in seconds via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout.strip())


def get_video_info(path: Path) -> dict:
    """Return {duration, width, height} via ffprobe."""
    out = subprocess.run(
        ["ffprobe", "-v", "error",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    data = json.loads(out.stdout)
    duration = float(data.get("format", {}).get("duration", 0.0))
    video_streams = [s for s in data.get("streams", []) if "width" in s]
    if video_streams:
        w = int(video_streams[0]["width"])
        h = int(video_streams[0]["height"])
    else:
        w, h = 1920, 1080
    return {"duration": duration, "width": w, "height": h}


def get_effective_duration(r: dict) -> float:
    """Output duration of a range accounting for speed (slow-motion stretches it)."""
    raw = float(r["end"]) - float(r["start"])
    return raw / float(r.get("speed", 1.0))


def _scaled_dims(src_w: int, src_h: int, scale_w: int) -> tuple[int, int]:
    """Compute output (width, height) after scaling width to scale_w, keeping aspect."""
    if src_w <= 0:
        return scale_w, scale_w * 9 // 16
    h = int(round(src_h * scale_w / src_w))
    if h % 2 != 0:
        h += 1
    return scale_w, h


def generate_segment_effect(
    effect: str,
    duration: float,
    fps: float,
    width: int,
    height: int,
    out_path: Path,
    n_particles: int = 120,
    seed: int = 42,
) -> None:
    """Generate a sparkle overlay for a single segment and save to out_path.

    Only called for effects that require a particle overlay file
    (sparkle, dreamlike). The bloom part of dreamlike is applied as
    a pure ffmpeg filter during extraction -- no file needed.
    """
    try:
        from sparkle_overlay import render_overlay  # same directory
    except ImportError:
        # Resolve relative to this file's directory
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "sparkle_overlay",
            Path(__file__).parent / "sparkle_overlay.py",
        )
        mod = importlib.util.module_from_spec(spec)   # type: ignore[arg-type]
        spec.loader.exec_module(mod)                   # type: ignore[union-attr]
        render_overlay = mod.render_overlay

    render_overlay(
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        effect=effect,
        out_path=out_path,
        n_particles=n_particles,
        seed=seed,
    )


def resolve_grade_filter(grade_field: str | None) -> str:
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError:
            print(f"warning: unknown preset '{grade_field}', using as raw filter")
            return grade_field
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def is_hdr_source(video: Path) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    duration: float,
    grade_filter: str,
    out_path: Path,
    preview: bool = False,
    draft: bool = False,
    speed: float = 1.0,
    effect: str = "",
    effect_opacity: float = 0.7,
    effect_overlay: Path | None = None,
) -> None:
    """Extract a cut range as its own MP4 with grade, 30ms audio fades, optional slow-motion,
    and optional visual effects (bloom / sparkle / dreamlike).

    speed < 1.0 = slow-motion (e.g. 0.5 = 2x). Applied via setpts (video) and
    atempo chain (audio). Output duration = source_duration / speed.

    effect:
      "bloom"     -- dreamy glow via split/gblur/screen blend (no overlay file)
      "sparkle"   -- particle overlay composited via screen blend
      "dreamlike" -- bloom + sparkle (overlay file required + bloom filter)
      ""          -- no effect (default)

    effect_overlay: pre-generated sparkle MP4 (required for sparkle/dreamlike).
    effect_opacity: screen-blend opacity for the particle overlay (0.0-1.0).
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)

    scale_w = 1280 if draft else 1920
    scale = f"scale={scale_w}:-2"

    # Build base video filter chain (applied first regardless of effect)
    vf_parts: list[str] = []
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(scale)
    if grade_filter:
        vf_parts.append(grade_filter)
    if speed != 1.0:
        vf_parts.append(f"setpts={1.0 / speed:.6f}*PTS")
    base_vf = ",".join(vf_parts)

    # Audio: atempo chain for speed, then 30ms boundary fades (Rule 3)
    out_duration = duration / speed
    fade_out_start = max(0.0, out_duration - 0.03)

    af_parts: list[str] = []
    if speed != 1.0:
        # atempo must stay in [0.5, 2.0]; chain filters for extreme values
        remaining = speed
        while remaining < 0.5:
            af_parts.append("atempo=0.5")
            remaining /= 0.5
        while remaining > 2.0:
            af_parts.append("atempo=2.0")
            remaining /= 2.0
        if abs(remaining - 1.0) > 0.001:
            af_parts.append(f"atempo={remaining:.6f}")
    af_parts.append("afade=t=in:st=0:d=0.03")
    af_parts.append(f"afade=t=out:st={fade_out_start:.3f}:d=0.03")
    af = ",".join(af_parts)

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    video_codec = ["-c:v", "libx264", "-preset", preset, "-crf", crf,
                   "-pix_fmt", "yuv420p", "-r", "24"]
    audio_codec = ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]

    # --- No effect: simple -vf/-af command ---
    has_bloom = effect in ("bloom", "dreamlike")
    has_sparkle = effect in ("sparkle", "dreamlike") and effect_overlay is not None

    if not has_bloom and not has_sparkle:
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{seg_start:.3f}",
            "-i", str(source),
            "-t", f"{out_duration:.3f}",
            "-vf", base_vf,
            "-af", af,
            *video_codec, "-movflags", "+faststart",
            str(out_path),
        ]
        run_ffmpeg(cmd, context=f"segment extract {out_path.name}")
        return

    # --- Effect: use filter_complex ---
    # Bloom sigma scales with output width for consistent softness
    bloom_sigma = max(8, scale_w // 55)

    # Get output frame dimensions to scale the overlay correctly
    info = get_video_info(source)
    out_w, out_h = _scaled_dims(info["width"], info["height"], scale_w)

    # Build filter_complex chain
    # Step 1: apply base vf to source -> [vtmp]
    fc_parts: list[str] = [f"[0:v]{base_vf}[vtmp]"]

    current_v = "[vtmp]"

    # Step 2: bloom (split -> gblur -> screen blend)
    if has_bloom:
        fc_parts.append(
            f"{current_v}split[_ba][_bb];"
            f"[_bb]gblur=sigma={bloom_sigma}[_blur];"
            f"[_ba][_blur]blend=all_mode=screen:all_opacity=0.30[_bloomed]"
        )
        current_v = "[_bloomed]"

    # Step 3: sparkle overlay (requires extra -i input at index 1)
    if has_sparkle:
        fc_parts.append(
            f"[1:v]scale={out_w}:{out_h}[_spk];"
            f"{current_v}[_spk]blend=all_mode=screen:all_opacity={effect_opacity:.2f}[outv]"
        )
    else:
        fc_parts.append(f"{current_v}copy[outv]")

    filter_complex = ";".join(fc_parts)

    # Build input list: -t must come AFTER all -i flags to be an output option.
    # If placed between two -i flags, ffmpeg treats it as an input option for
    # the second file (limits sparkle overlay duration, not the output).
    inputs: list[str] = ["-ss", f"{seg_start:.3f}", "-i", str(source)]
    if has_sparkle:
        inputs += ["-i", str(effect_overlay)]

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-t", f"{out_duration:.3f}",    # OUTPUT option: must be after all -i
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-map", "0:a",
        "-af", af,
        *video_codec, "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(cmd, context=f"effect segment extract {out_path.name} (effect={effect})")


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
) -> list[Path]:
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]

    seg_paths: list[Path] = []
    scale_w = 1280 if draft else 1920
    print(f"extracting {len(ranges)} segment(s) -> {clips_dir.name}/")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        start = float(r["start"])
        end = float(r["end"])
        duration = end - start
        speed = float(r.get("speed", 1.0))
        effect = r.get("effect", "")
        effect_opacity = float(r.get("effect_opacity", 0.7))
        out_path = clips_dir / f"seg_{i:02d}_{src_name}.mp4"

        if is_auto:
            seg_filter, _stats = auto_grade_for_clip(
                src_path, start=start, duration=duration, verbose=False
            )
        else:
            seg_filter = resolved

        note = r.get("beat") or r.get("note") or ""
        speed_note = f"  [{speed}x slo-mo -> {get_effective_duration(r):.2f}s]" if speed != 1.0 else ""
        effect_note = f"  [effect: {effect}]" if effect else ""
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)"
              f"{speed_note}{effect_note}  {note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")

        # Generate sparkle overlay for effects that need a particle file
        effect_overlay: Path | None = None
        if effect in ("sparkle", "dreamlike"):
            out_dur = get_effective_duration(r)
            info = get_video_info(src_path)
            ow, oh = _scaled_dims(info["width"], info["height"], scale_w)
            overlay_path = clips_dir / f"effect_{i:02d}.mp4"
            print(f"        generating {effect} overlay ({out_dur:.1f}s, {ow}x{oh})")
            try:
                generate_segment_effect(
                    effect=effect,
                    duration=out_dur,
                    fps=24.0,
                    width=ow,
                    height=oh,
                    out_path=overlay_path,
                )
                effect_overlay = overlay_path
            except Exception as exc:
                print(f"        warning: effect overlay failed ({exc}), rendering without effect")
                effect = ""

        extract_segment(
            src_path, start, duration, seg_filter, out_path,
            preview=preview, draft=draft, speed=speed,
            effect=effect, effect_opacity=effect_opacity,
            effect_overlay=effect_overlay,
        )
        seg_paths.append(out_path)

    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path, edit_dir: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = edit_dir / "_concat.txt"
    concat_list.write_text("".join(f"file '{p.resolve()}'\n" for p in segment_paths))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        "-movflags", "+faststart",
        str(out_path),
    ]
    print(f"concat -> {out_path.name}")
    run_ffmpeg(cmd, context=f"concat -> {out_path.name}")
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    Word timestamps are remapped through speed so slow-motion segments keep
    captions in sync: output_time = (word.start - seg_start) / speed + seg_offset
    """
    transcripts_dir = edit_dir / "transcripts"
    sources = edl["sources"]

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_end = float(r["end"])
        speed = float(r.get("speed", 1.0))
        seg_duration = get_effective_duration(r)  # actual output duration

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        chunks: list[list[dict]] = []
        current: list[dict] = []
        for w in words_in_seg:
            text = (w.get("text") or "").strip()
            if not text:
                continue
            current.append(w)
            ends_in_punct = bool(text) and text[-1] in PUNCT_BREAK
            if len(current) >= 2 or ends_in_punct:
                chunks.append(current)
                current = []
        if current:
            chunks.append(current)

        for chunk in chunks:
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            # Remap source timestamps to output timeline via speed
            out_start = max(0.0, (local_start - seg_start) / speed + seg_offset)
            out_end = max(0.0, (local_end - seg_start) / speed + seg_offset)
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip().rstrip(",;:").upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"master SRT -> {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    stderr = proc.stderr
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    if preview:
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) -> {output_path.name}")
        run_ffmpeg(cmd, context="loudnorm (1-pass preview)")
        return True

    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed -- falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LOUDNORM_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing -> {output_path.name}")
    run_ffmpeg(cmd, context="loudnorm (pass 2)")
    return True


# -------- Final compositing (Rule 1 + Rule 4 + music + SFX) -----------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    music_spec: dict | None = None,
    sfx_list: list[dict] | None = None,
) -> None:
    """Final pass: base -> overlays (PTS-shifted) -> subtitles LAST -> out.

    Optionally mixes background music and/or point-in-time sound effects into
    the audio track. All operations happen in a single ffmpeg invocation to
    avoid redundant re-encodes.
    """
    sfx_list = sfx_list or []
    has_overlays = bool(overlays)
    has_subs = subtitles_path is not None and subtitles_path.exists()
    has_music = music_spec is not None
    has_sfx = bool(sfx_list)
    has_audio_mix = has_music or has_sfx
    has_video_filter = has_overlays or has_subs

    if not has_video_filter and not has_audio_mix:
        run(["ffmpeg", "-y", "-i", str(base_path), "-c", "copy", str(out_path)], quiet=True)
        return

    # Build inputs list as (pre_flags, path) so -stream_loop can precede music -i
    input_specs: list[tuple[list[str], str]] = [([], str(base_path))]

    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        input_specs.append(([], str(ov_path)))

    music_input_idx: int | None = None
    if has_music:
        assert music_spec is not None
        music_path = resolve_path(music_spec["file"], edit_dir)
        loop_flag = ["-stream_loop", "-1"] if music_spec.get("loop", True) else []
        music_input_idx = len(input_specs)
        input_specs.append((loop_flag, str(music_path)))

    sfx_input_indices: list[int] = []
    for sfx in sfx_list:
        sfx_path = resolve_path(sfx["file"], edit_dir)
        sfx_input_indices.append(len(input_specs))
        input_specs.append(([], str(sfx_path)))

    all_inputs: list[str] = []
    for pre_flags, path in input_specs:
        all_inputs += pre_flags + ["-i", path]

    filter_parts: list[str] = []
    current_video = "[0:v]"

    if has_video_filter:
        # PTS-shift overlay frame 0 to its output window start (Rule 4)
        for idx, ov in enumerate(overlays, start=1):
            t = float(ov["start_in_output"])
            filter_parts.append(f"[{idx}:v]setpts=PTS-STARTPTS+{t}/TB[ov{idx}]")

        for idx, ov in enumerate(overlays, start=1):
            t = float(ov["start_in_output"])
            dur = float(ov["duration"])
            end = t + dur
            next_label = f"[v{idx}]"
            filter_parts.append(
                f"{current_video}[ov{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
            )
            current_video = next_label

        # Subtitles LAST (Rule 1)
        if has_subs:
            subs_abs = (
                str(subtitles_path.resolve())
                .replace("\\", "/")
                .replace(":", r"\:")
                .replace("'", r"\'")
            )
            filter_parts.append(
                f"{current_video}subtitles='{subs_abs}':force_style='{SUB_FORCE_STYLE}'[outv]"
            )
        else:
            filter_parts.append(f"{current_video}null[outv]")
        video_map = "[outv]"
    else:
        video_map = "0:v"

    # Audio mixing: narration + optional music (with optional ducking) + optional SFX
    audio_map = "0:a"
    if has_audio_mix:
        duck_narration = has_music and bool(music_spec.get("duck_narration", False))  # type: ignore[union-attr]

        if duck_narration:
            # Split narration: one stream for the final mix, one as sidechain for compression
            filter_parts.append("[0:a]asplit=2[narr][narr_sc]")
            audio_labels = ["[narr]"]
        else:
            audio_labels = ["[0:a]"]

        if has_music and music_input_idx is not None:
            vol = music_spec.get("volume", 0.15)  # type: ignore[union-attr]
            fade_in = float(music_spec.get("fade_in", 2.0))  # type: ignore[union-attr]
            fade_out_dur = float(music_spec.get("fade_out", 3.0))  # type: ignore[union-attr]
            video_dur = get_video_duration(base_path)
            fade_out_start = max(0.0, video_dur - fade_out_dur)

            music_chain: list[str] = [f"volume={vol}"]
            if fade_in > 0:
                music_chain.append(f"afade=t=in:st=0:d={fade_in:.3f}")
            if fade_out_dur > 0 and fade_out_start > 0:
                music_chain.append(
                    f"afade=t=out:st={fade_out_start:.3f}:d={fade_out_dur:.3f}"
                )
            # Trim to video length so looped music doesn't extend the output
            music_chain.append(f"atrim=0:{video_dur:.3f},asetpts=PTS-STARTPTS")

            if duck_narration:
                # Output to intermediate label, then sidechain-compress against narration
                filter_parts.append(
                    f"[{music_input_idx}:a]{','.join(music_chain)}[music_raw]"
                )
                duck_thresh = float(music_spec.get("duck_threshold", 0.02))  # type: ignore[union-attr]
                duck_attack = float(music_spec.get("duck_attack_ms", 200))  # type: ignore[union-attr]
                duck_release = float(music_spec.get("duck_release_ms", 800))  # type: ignore[union-attr]
                filter_parts.append(
                    f"[music_raw][narr_sc]sidechaincompress="
                    f"threshold={duck_thresh}:ratio=8:"
                    f"attack={duck_attack:.0f}:release={duck_release:.0f}"
                    f"[music]"
                )
            else:
                filter_parts.append(
                    f"[{music_input_idx}:a]{','.join(music_chain)}[music]"
                )
            audio_labels.append("[music]")

        for i, (sfx, sfx_idx) in enumerate(zip(sfx_list, sfx_input_indices)):
            vol = sfx.get("volume", 1.0)
            delay_ms = int(float(sfx["start_in_output"]) * 1000)
            filter_parts.append(
                f"[{sfx_idx}:a]volume={vol},adelay={delay_ms}|{delay_ms}[sfx{i}]"
            )
            audio_labels.append(f"[sfx{i}]")

        n = len(audio_labels)
        filter_parts.append(
            f"{''.join(audio_labels)}amix=inputs={n}:duration=first:normalize=0[outa]"
        )
        audio_map = "[outa]"

    duck_narration_active = has_music and bool(music_spec.get("duck_narration", False))  # type: ignore[union-attr]
    print(f"compositing -> {out_path.name}")
    print(
        f"  overlays: {len(overlays)}, subtitles: {'yes' if has_subs else 'no'}, "
        f"music: {'yes' if has_music else 'no'}"
        + (" (ducking)" if duck_narration_active else "")
        + f", sfx: {len(sfx_list)}"
    )

    video_codec = (
        ["-c:v", "libx264", "-preset", "fast", "-crf", "18", "-pix_fmt", "yuv420p"]
        if has_video_filter else ["-c:v", "copy"]
    )
    audio_codec = (
        ["-c:a", "aac", "-b:a", "192k", "-ar", "48000"]
        if has_audio_mix else ["-c:a", "copy"]
    )

    cmd = [
        "ffmpeg", "-y",
        *all_inputs,
        "-filter_complex", ";".join(filter_parts),
        "-map", video_map,
        "-map", audio_map,
        *video_codec,
        *audio_codec,
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(cmd, context=f"final composite -> {out_path.name}")


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview", action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 -- evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft", action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 -- cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles", action="store_true",
        help="Build master.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles", action="store_true",
        help="Skip subtitles even if the EDL references one (also respected via subtitles_enabled:false in EDL)",
    )
    ap.add_argument(
        "--no-loudnorm", action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    # EDL-level feature flags (all optional, backward-compatible)
    music_spec = edl.get("background_music")
    sfx_list: list[dict] = edl.get("sound_effects") or []
    subtitles_enabled: bool = edl.get("subtitles_enabled", True)

    # 1. Extract per-segment (with optional slow-motion per range)
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft
    )

    # 2. Lossless concat -> base
    if args.draft:
        base_name = "base_draft.mp4"
    elif args.preview:
        base_name = "base_preview.mp4"
    else:
        base_name = "base.mp4"
    base_path = edit_dir / base_name
    concat_segments(segment_paths, base_path, edit_dir)

    # 3. Build subtitles (if requested and not disabled)
    subs_path: Path | None = None
    if not args.no_subtitles and subtitles_enabled:
        if args.build_subtitles:
            subs_path = edit_dir / "master.srt"
            build_master_srt(edl, edit_dir, subs_path)
        elif edl.get("subtitles"):
            subs_path = resolve_path(edl["subtitles"], edit_dir)
            if not subs_path.exists():
                print(f"warning: subtitles path in EDL does not exist: {subs_path}")
                subs_path = None

    # 4. Composite (overlays + music + sfx + subtitles LAST) -> prenorm
    overlays = edl.get("overlays") or []
    if args.no_loudnorm:
        build_final_composite(
            base_path, overlays, subs_path, out_path, edit_dir,
            music_spec=music_spec, sfx_list=sfx_list,
        )
    else:
        tmp_composite = out_path.with_suffix(".prenorm.mp4")
        build_final_composite(
            base_path, overlays, subs_path, tmp_composite, edit_dir,
            music_spec=music_spec, sfx_list=sfx_list,
        )
        print("loudness normalization -> social-ready (-14 LUFS / -1 dBTP / LRA 11)")
        apply_loudnorm_two_pass(tmp_composite, out_path, preview=args.draft)
        tmp_composite.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
