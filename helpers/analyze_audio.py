#!/usr/bin/env python3
"""Analyze audio in a video or audio file.

Detects silence regions, onset peaks, beat grid, energy profile, and a
heuristic speech/non-speech split. Suggests optimal timestamps for sound
effects (transition, impact, ambient) and reports cut-point candidates for
voiceless content where no Scribe transcript is available.

Works on both voiced and voiceless content. For voiceless sources (b-roll,
timelapse, product shots) this replaces `pack_transcripts.py` as the primary
editorial-signal generator.

Usage:
    # Human-readable summary to stdout
    python helpers/analyze_audio.py <video_or_audio>

    # Also scan an sfx/ folder and suggest file matches
    python helpers/analyze_audio.py <video> --sfx-dir <sfx_folder>

    # Restrict analysis to a time window
    python helpers/analyze_audio.py <video> --start 10.0 --end 40.0

    # Write machine-readable JSON (for EDL construction)
    python helpers/analyze_audio.py <video> --out analysis.json

    # Combine: full analysis with sfx matching, save JSON
    python helpers/analyze_audio.py <video> --sfx-dir sfx/ --out edit/audio_analysis.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import librosa
import numpy as np


# -------- Audio extraction ---------------------------------------------------


def extract_mono_wav(source: Path, tmp_dir: Path, start: float = 0.0, end: float | None = None) -> Path:
    """Extract a mono 22 kHz WAV from any video or audio file."""
    wav_path = tmp_dir / "audio_analysis.wav"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-nostats"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(source)]
    if end is not None:
        cmd += ["-t", f"{end - start:.3f}"]
    cmd += ["-ac", "1", "-ar", "22050", "-vn", str(wav_path)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wav_path


# -------- Core detectors -----------------------------------------------------


def detect_silence(y: np.ndarray, sr: int, top_db: float = 35.0, min_silence_s: float = 0.25) -> list[dict]:
    """Return silence regions as [{start, end, duration}] in source seconds."""
    non_silent = librosa.effects.split(y, top_db=top_db)
    duration = len(y) / sr
    regions: list[dict] = []

    prev_end = 0.0
    for seg_start, seg_end in non_silent:
        gap_start = prev_end
        gap_end = seg_start / sr
        gap_dur = gap_end - gap_start
        if gap_dur >= min_silence_s:
            regions.append({"start": round(gap_start, 3), "end": round(gap_end, 3), "duration": round(gap_dur, 3)})
        prev_end = seg_end / sr

    trailing = duration - prev_end
    if trailing >= min_silence_s:
        regions.append({"start": round(prev_end, 3), "end": round(duration, 3), "duration": round(trailing, 3)})

    return regions


def detect_onsets(y: np.ndarray, sr: int) -> list[float]:
    """Return onset times (sharp energy transients)."""
    times = librosa.onset.onset_detect(y=y, sr=sr, units="time", backtrack=True)
    return [round(float(t), 3) for t in times]


def detect_beats(y: np.ndarray, sr: int) -> tuple[float, list[float]]:
    """Return (tempo_bpm, [beat_times])."""
    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr, units="frames")
    beat_times = librosa.frames_to_time(beat_frames, sr=sr)
    return round(float(np.atleast_1d(tempo)[0]), 1), [round(float(t), 3) for t in beat_times]


def energy_profile(y: np.ndarray, sr: int, hop_s: float = 0.25) -> list[dict]:
    """RMS energy sampled every hop_s seconds."""
    hop = int(sr * hop_s)
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
    return [{"time": round(float(t), 3), "rms": round(float(r), 5)} for t, r in zip(times, rms)]


def energy_peaks(profile: list[dict], percentile: float = 80.0) -> list[dict]:
    """Return energy samples above the given percentile — useful as cut candidates."""
    if not profile:
        return []
    threshold = float(np.percentile([e["rms"] for e in profile], percentile))
    peaks = [e for e in profile if e["rms"] >= threshold]
    # Deduplicate: keep only local maxima (within 0.5s windows)
    deduped: list[dict] = []
    for p in peaks:
        if not deduped or p["time"] - deduped[-1]["time"] >= 0.5:
            deduped.append({**p, "is_peak": True})
    return deduped


def heuristic_speech_split(y: np.ndarray, sr: int) -> tuple[list[dict], list[dict]]:
    """Rough speech vs non-speech split using ZCR + energy.

    Not a replacement for Scribe — use only when no transcript exists.
    Speech signature: moderate-to-high energy + moderate-to-high ZCR.
    """
    hop = int(sr * 0.05)  # 50 ms frames
    rms = librosa.feature.rms(y=y, hop_length=hop)[0]
    zcr = librosa.feature.zero_crossing_rate(y, hop_length=hop)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)

    rms_n = rms / (rms.max() + 1e-9)
    zcr_n = zcr / (zcr.max() + 1e-9)

    # Speech: energy > 5% of max AND zcr > 15% of max
    is_speech = (rms_n > 0.05) & (zcr_n > 0.15)

    def _merge_frames(mask: np.ndarray, min_dur: float = 0.15) -> list[dict]:
        segs: list[dict] = []
        in_seg = False
        t_start = 0.0
        for i, (t, v) in enumerate(zip(times, mask)):
            if v and not in_seg:
                t_start = float(t)
                in_seg = True
            elif not v and in_seg:
                dur = float(t) - t_start
                if dur >= min_dur:
                    segs.append({"start": round(t_start, 3), "end": round(float(t), 3), "duration": round(dur, 3)})
                in_seg = False
        if in_seg:
            dur = float(times[-1]) - t_start
            if dur >= min_dur:
                segs.append({"start": round(t_start, 3), "end": round(float(times[-1]), 3), "duration": round(dur, 3)})
        return segs

    speech = _merge_frames(is_speech)
    non_speech = _merge_frames(~is_speech)
    return speech, non_speech


# -------- SFX suggestions ----------------------------------------------------


def _sfx_type_from_name(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("whoosh", "swish", "sweep", "transition", "swoosh", "fly")):
        return "transition"
    if any(k in n for k in ("impact", "hit", "punch", "thud", "crash", "boom", "smash", "drop")):
        return "impact"
    if any(k in n for k in ("ambient", "crowd", "room", "bg", "background", "nature", "rain", "wind", "atmo")):
        return "ambient"
    if any(k in n for k in ("ding", "chime", "bell", "notification", "pop", "click", "tick")):
        return "accent"
    return "transition"  # generic default


def suggest_sfx_placements(
    silence_regions: list[dict],
    onsets: list[float],
    profile: list[dict],
    non_speech: list[dict],
    sfx_names: list[str] | None = None,
    time_offset: float = 0.0,
) -> list[dict]:
    """Suggest timestamps and SFX types based on audio structure.

    Strategy:
    - transition  → 0.1–0.15s before each silence gap (cut-point entry)
    - impact      → onset peaks with energy significantly above average
    - ambient     → start of long non-speech / silence regions (≥ 2s)
    - accent      → onset peaks at moderate energy (button clicks, dings)
    """
    if profile:
        avg_rms = float(np.mean([e["rms"] for e in profile]))
        rms_lookup = {e["time"]: e["rms"] for e in profile}

        def nearest_rms(t: float) -> float:
            if not profile:
                return 0.0
            return min(profile, key=lambda e: abs(e["time"] - t))["rms"]
    else:
        avg_rms = 0.0

        def nearest_rms(t: float) -> float:
            return 0.0

    suggestions: list[dict] = []

    # Transition SFX: at entry to every silence gap ≥ 0.4s
    for sil in silence_regions:
        if sil["duration"] >= 0.4:
            t = max(0.0, sil["start"] - 0.12) + time_offset
            conf = "high" if sil["duration"] >= 1.0 else "medium"
            suggestions.append({
                "time": round(t, 3),
                "type": "transition",
                "confidence": conf,
                "reason": f"silence gap {sil['start']:.2f}-{sil['end']:.2f}s ({sil['duration']:.2f}s) -- clean cut point",
            })

    # Impact / accent SFX: onset peaks with meaningful energy (>2.5x avg to avoid noise flood)
    for onset in onsets:
        e = nearest_rms(onset)
        if avg_rms > 0 and e > avg_rms * 2.5:
            sfx_type = "impact" if e > avg_rms * 4.0 else "accent"
            conf = "high" if e > avg_rms * 4.0 else "medium"
            suggestions.append({
                "time": round(onset + time_offset, 3),
                "type": sfx_type,
                "confidence": conf,
                "reason": f"onset peak at {onset:.2f}s, energy {e:.4f} ({e / avg_rms:.1f}x avg)",
            })

    # Ambient SFX: long non-speech or silence regions >= 2s
    ambient_regions = [r for r in non_speech if r.get("duration", 0) >= 2.0]
    ambient_regions += [r for r in silence_regions if r.get("duration", 0) >= 2.0]
    seen_starts: set[float] = set()
    for r in sorted(ambient_regions, key=lambda x: x["start"]):
        t = round(r["start"] + time_offset, 3)
        if t not in seen_starts:
            seen_starts.add(t)
            suggestions.append({
                "time": t,
                "type": "ambient",
                "confidence": "medium",
                "reason": f"non-speech region {r['start']:.2f}-{r['end']:.2f}s ({r['duration']:.2f}s)",
            })

    # Sort and deduplicate: keep highest-confidence within 0.5s windows
    suggestions.sort(key=lambda s: s["time"])
    deduped: list[dict] = []
    for s in suggestions:
        if not deduped or s["time"] - deduped[-1]["time"] > 0.5:
            deduped.append(s)
        elif s["confidence"] == "high" and deduped[-1]["confidence"] != "high":
            deduped[-1] = s  # upgrade to higher confidence

    # Match SFX files by type
    if sfx_names:
        by_type: dict[str, list[str]] = {"transition": [], "impact": [], "ambient": [], "accent": []}
        for name in sfx_names:
            by_type.setdefault(_sfx_type_from_name(name), []).append(name)

        for s in deduped:
            candidates = by_type.get(s["type"], [])
            if not candidates:
                candidates = by_type.get("transition", [])  # fallback
            if candidates:
                s["suggested_sfx"] = candidates[0]

    return deduped


# -------- Voiceless cut candidates -------------------------------------------


def beat_cut_candidates(beat_times: list[float], every_n: int = 2) -> list[dict]:
    """Return every Nth beat as a suggested cut point (music-sync editing)."""
    return [
        {"time": round(t, 3), "type": "beat_cut", "reason": f"beat {i + 1} (every {every_n})"}
        for i, t in enumerate(beat_times)
        if (i + 1) % every_n == 0
    ]


# -------- Main ---------------------------------------------------------------


def analyze(
    source: Path,
    start: float = 0.0,
    end: float | None = None,
    sfx_dir: Path | None = None,
    min_silence_s: float = 0.25,
    top_db: float = 35.0,
) -> dict:
    """Run full audio analysis. Returns a result dict."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = extract_mono_wav(source, Path(tmp), start=start, end=end)
        y, sr = librosa.load(str(wav), sr=22050, mono=True)

    duration = len(y) / sr
    time_offset = start

    silence = detect_silence(y, sr, top_db=top_db, min_silence_s=min_silence_s)
    onsets = detect_onsets(y, sr)
    tempo, beats = detect_beats(y, sr)
    profile = energy_profile(y, sr)
    peaks = energy_peaks(profile)
    speech_segs, non_speech_segs = heuristic_speech_split(y, sr)

    # Offset all times by window start
    def offset(regions: list[dict]) -> list[dict]:
        return [{**r, "start": round(r["start"] + time_offset, 3),
                 "end": round(r["end"] + time_offset, 3)} for r in regions]

    silence_abs = offset(silence)
    speech_abs = offset(speech_segs)
    non_speech_abs = offset(non_speech_segs)
    beats_abs = [round(t + time_offset, 3) for t in beats]
    onsets_abs = [round(t + time_offset, 3) for t in onsets]
    peaks_abs = [{**p, "time": round(p["time"] + time_offset, 3)} for p in peaks]

    # Heuristic: has_speech = > 15% of duration classified as speech
    speech_total = sum(s["duration"] for s in speech_segs)
    has_speech = (speech_total / max(duration, 0.1)) > 0.10

    sfx_names: list[str] | None = None
    if sfx_dir and sfx_dir.is_dir():
        sfx_names = sorted(
            p.name for p in sfx_dir.iterdir()
            if p.suffix.lower() in {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
        )

    suggestions = suggest_sfx_placements(
        silence_abs, onsets_abs, peaks_abs, non_speech_abs,
        sfx_names=sfx_names, time_offset=0.0,
    )

    beat_cuts = beat_cut_candidates(beats_abs, every_n=2)

    return {
        "file": str(source),
        "analysis_window": {"start": start, "end": round(start + duration, 3)},
        "duration_s": round(start + duration, 3),
        "has_speech": has_speech,
        "speech_fraction": round(speech_total / max(duration, 0.1), 3),
        "tempo_bpm": tempo,
        "beat_times": beats_abs,
        "beat_cut_candidates": beat_cuts,
        "silence_regions": silence_abs,
        "onset_times": onsets_abs,
        "energy_peaks": peaks_abs,
        "speech_regions": speech_abs,
        "non_speech_regions": non_speech_abs,
        "sfx_available": sfx_names or [],
        "sfx_suggestions": suggestions,
    }


def _print_report(result: dict) -> None:
    print(f"\nAudio analysis: {Path(result['file']).name}")
    print(f"  duration     : {result['duration_s']:.1f}s")
    print(f"  has_speech   : {result['has_speech']}  ({result['speech_fraction']:.0%} of duration)")
    print(f"  tempo        : {result['tempo_bpm']} BPM  ({len(result['beat_times'])} beats)")
    print(f"  silence gaps : {len(result['silence_regions'])}  (>= 0.25s)")
    print(f"  onset peaks  : {len(result['onset_times'])}")

    print(f"\nSilence regions ({len(result['silence_regions'])}):")
    for s in result["silence_regions"]:
        print(f"  [{s['start']:6.2f} - {s['end']:6.2f}s]  {s['duration']:.2f}s")

    print(f"\nBeat cut candidates (every 2nd beat, {len(result['beat_cut_candidates'])} total):")
    if result["beat_cut_candidates"]:
        times = [str(c["time"]) for c in result["beat_cut_candidates"][:20]]
        suffix = "  ..." if len(result["beat_cut_candidates"]) > 20 else ""
        print(f"  {', '.join(times)}{suffix}")
    else:
        print("  (no strong beat grid detected)")

    if result["sfx_available"]:
        print(f"\nSFX files found ({len(result['sfx_available'])}):")
        for name in result["sfx_available"]:
            print(f"  {name}")

    print(f"\nSFX placement suggestions ({len(result['sfx_suggestions'])}):")
    for s in result["sfx_suggestions"]:
        sfx_hint = f"  -> {s['suggested_sfx']}" if "suggested_sfx" in s else ""
        print(f"  t={s['time']:6.2f}s  [{s['type']:10s}] [{s['confidence']:6s}]  {s['reason']}{sfx_hint}")

    if not result["has_speech"]:
        print(
            "\n[voiceless content] No significant speech detected.\n"
            "  Use beat_cut_candidates or silence_regions as EDL cut points.\n"
            "  Use sfx_suggestions for SFX placement.\n"
            "  Run with --sfx-dir <folder> to match SFX files to suggestions."
        )

    print()


def main() -> None:
    ap = argparse.ArgumentParser(description="Analyze audio -- silence, beats, onsets, SFX placement")
    ap.add_argument("source", type=Path, help="Video or audio file to analyze")
    ap.add_argument("--start", type=float, default=0.0, help="Analysis window start (seconds)")
    ap.add_argument("--end", type=float, default=None, help="Analysis window end (seconds)")
    ap.add_argument("--sfx-dir", type=Path, default=None, help="Folder of SFX files to match against suggestions")
    ap.add_argument("--out", type=Path, default=None, help="Write JSON result to this path")
    ap.add_argument("--min-silence", type=float, default=0.25, help="Minimum silence duration to report (default 0.25s)")
    ap.add_argument("--top-db", type=float, default=35.0, help="Silence threshold in dB below peak (default 35)")
    args = ap.parse_args()

    if not args.source.exists():
        sys.exit(f"file not found: {args.source}")

    result = analyze(
        args.source,
        start=args.start,
        end=args.end,
        sfx_dir=args.sfx_dir,
        min_silence_s=args.min_silence,
        top_db=args.top_db,
    )

    _print_report(result)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"JSON saved -> {args.out}")


if __name__ == "__main__":
    main()
