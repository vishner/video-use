---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles, add background music, place sound effects, apply slow-motion — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.
13. **Never slow-mo a speech segment.** `speed` < 1.0 is only safe on silent action ranges. Slowed speech is unintelligible. Split the range at word boundaries if needed.
14. **Subtitles are disabled** via `"subtitles_enabled": false` in the EDL or the `--no-subtitles` CLI flag. Both are honored. Omitting the field defaults to enabled.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
├── music.mp3                    ← background music (any name, any audio format)
├── sfx/                         ← sound effects folder (self-descriptive filenames)
│   ├── whoosh_transition.mp3
│   ├── impact_hit.wav
│   └── ambient_crowd.mp3
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades
    ├── master.srt               ← output-timeline subtitles
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

At session start, also scan for optional assets in `<videos_dir>/`:

```bash
# Music: any audio file at the root level
find <videos_dir> -maxdepth 1 \( -name "*.mp3" -o -name "*.wav" -o -name "*.m4a" -o -name "*.aac" -o -name "*.ogg" \) ! -name ".*"

# Sound effects: anything inside sfx/ subfolder
ls <videos_dir>/sfx/ 2>/dev/null
```

If music is found, note it and propose including it in the strategy. If an `sfx/` folder exists, read the filenames — they are self-describing (`whoosh_transition.mp3`, `impact_hit.wav`). Propose relevant SFX placements in the strategy phase; do not add them silently.

- `ELEVENLABS_API_KEY` resolves — either in the environment or in `.env` at the video-use repo root. If missing, ask the user to paste one and write it to `.env` (never to the user's `<videos_dir>`).
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Helpers

- **`transcribe.py <video>`** — single-file Scribe call. `--num-speakers N` optional. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly.
- **`analyze_audio.py <video>`** — audio analysis for voiceless/music-only content. Detects silence regions, beat tempo, onset peaks, energy profile, and outputs SFX placement suggestions + beat-cut candidates. Use instead of transcript-based editing when `has_speech: false`. See Voiceless content workflow below.
- **`detect_approach.py <video>`** — detects segments where a subject moves toward the camera by tracking face/person bounding box growth. Outputs `approach_segments` JSON with `start`, `end`, `growth_pct`, `confidence`, `suggested_speed`, `suggested_effect`. Use to auto-populate EDL ranges for approach slo-mo + effects. See Approach slo-mo + effects below.
- **`sparkle_overlay.py`** — generates animated sparkle/glitter particle overlays as screen-blend-ready MP4s. Called automatically by `render.py` when a range has `effect: "sparkle"` or `effect: "dreamlike"`. Can also be run standalone for custom overlay work.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → subtitles LAST. `--preview` for 720p fast. `--build-subtitles` to generate master.srt inline.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression. Scan for music files and `sfx/` folder (see Setup).
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Slow motion

Slow-motion is a per-range EDL field: `"speed": 0.5` = 2× slo-mo, `"speed": 0.25` = 4×. `render.py` applies `setpts` (video) and an `atempo` chain (audio) automatically.

**When to use it — read the material, then decide:**

- **Cinematic pour / reveal shots.** Liquid pouring, product reveal, close-up detail work. These are money shots. Speed 0.5 is almost always right.
- **Physical action peaks.** Shaking, impact, a dramatic gesture. Speed 0.5–0.33.
- **Hero moments in a narrative.** The first product close-up, a reaction shot, a key beat the user wants to land. Speed 0.5.
- **Fast-paced montage accents.** One or two 0.5× shots in an otherwise normal-speed montage reads as intentional style, not accident.
- **User requested "cinematic", "dramatic", "filmic", or "slo-mo".** Identify the 1–3 best visual moments and apply there, not everywhere.

**Source frame rate matters.** Slow-mo below 0.5× on a 24/30fps source produces visible judder. Check `ffprobe` — if the source is 60fps, you can safely go to 0.25× (4×). At 120fps, 0.125× is clean.

**Never slow-mo a speech segment** (Hard Rule 13). If a narration phrase overlaps the action you want to slow, split the range: keep the speech segment at speed 1.0, extract the post-narration action silence as a separate range at the target speed.

**Typical factors:**

| Factor | Effect | Best for |
|--------|--------|----------|
| 0.5 | 2× slo-mo | Most cases — cinematic without feeling artificial |
| 0.33 | 3× slo-mo | Close-up reveals, hero product shots |
| 0.25 | 4× slo-mo | 60fps+ source only — dramatic action peaks |

**In the EDL:**

```json
{"source": "clip", "start": 33.10, "end": 36.00, "beat": "SHAKE_ACTION", "speed": 0.5,
 "quote": "", "reason": "cinematic shake reveal at 2x slo-mo"}
```

## Approach slo-mo + visual effects

Certain styles — K-pop MVs, fashion reels, dramatic reveals — slow down exactly when a person walks or moves toward camera and layer glitter, bloom, or sparkle effects during the slow moment. This is fully automatic.

**Detection workflow:**

```bash
# 1. Detect approach segments in the source
python helpers/detect_approach.py <video> --out edit/approach.json

# Result: approach_segments with start, end, confidence, suggested_speed, suggested_effect
```

The detector samples at 4fps, tracks the largest face or person bounding box, and flags windows where the box area grows >= 20% over >= 1.5s. Each segment gets `suggested_speed: 0.5` and `suggested_effect: "dreamlike"` by default. Adjust thresholds:
- `--growth 0.15` — more sensitive (catches subtle approaches)
- `--growth 0.35` — stricter (only dramatic lunges)
- `--min-duration 1.0` — shorter minimum approach windows

**Use the output to populate EDL ranges:**

Take `approach_segments[].start/end` as the range boundaries. Split at word boundaries if narration overlaps (Hard Rule 13). Set `"speed": 0.5` and `"effect": "dreamlike"`.

**Effect types:**

| Effect | What it does | Best for |
|--------|-------------|----------|
| `"bloom"` | Dreamy radial glow — split/blur/screen blend, no extra file | Cinematic, romantic, soft focus |
| `"sparkle"` | Animated 4-point star particles drifting upward | Fashion, celebration, product reveal |
| `"dreamlike"` | Bloom + sparkle combined | K-pop style, dramatic approach shots |
| `""` (default) | No effect | Normal slo-mo without visual treatment |

`render.py` handles everything automatically: for `sparkle`/`dreamlike` it generates the particle overlay before extraction and composites it via `blend=all_mode=screen`. For `bloom` it adds a pure ffmpeg filter chain — no extra file needed.

**`effect_opacity`** controls the sparkle intensity (0.0–1.0, default 0.7). Lower for subtle shimmer, higher for full K-pop glitter.

**EDL range with approach effect:**

```json
{
  "source": "clip",
  "start": 5.20, "end": 9.80,
  "beat": "APPROACH_HERO",
  "speed": 0.5,
  "effect": "dreamlike",
  "effect_opacity": 0.7,
  "quote": "",
  "reason": "Subject walks toward camera. 2x slo-mo + dreamlike effect."
}
```

**Combine with music:** approach slo-mo with dreamlike effects hits hardest when a music drop or chorus lands at the same moment. Align the approach segment start to a beat from `analyze_audio.py beat_cut_candidates`.

**Anti-patterns:**
- Don't apply effects to every segment — pick 1–3 peak moments.
- Don't use dreamlike on speech segments (it distracts from the words).
- Don't use bloom sigma too high (> 40) — the image washes out.

## Voiceless content workflow

When `analyze_audio.py` reports `has_speech: false` (or the content is clearly action/montage/music-only), skip the transcript-based pipeline and use audio analysis as the editorial signal instead.

**When to use this path:**
- B-roll montages, product demos with no narration
- Action / sports footage
- Music video or cinematic reels
- Any clip where `has_speech` is false or speech fraction is below ~20%

**Workflow:**

```bash
# 1. Analyze the source
python helpers/analyze_audio.py <video> --sfx-dir <videos_dir>/sfx/ --out edit/audio_analysis.json

# 2. Read the output: silence_regions, beat_cut_candidates, sfx_suggestions, tempo_bpm
```

The JSON output has three editorial signals:

| Field | Use for |
|-------|---------|
| `beat_cut_candidates` | Cut points aligned to musical beats (every 2nd beat by default) |
| `sfx_suggestions` | Pre-scored SFX placements at energy peaks/onsets, typed by SFX filename |
| `silence_regions` | Natural breathing room — good for cut boundaries |

**Beat-sync editing:** use `beat_cut_candidates[].time` values as range boundaries in the EDL. Cuts on beat boundaries feel intentional and rhythmic. The `tempo_bpm` field tells you the groove — communicate this to the user ("this footage has a ~120 BPM pulse; I'll cut on the 2-beat").

**SFX from suggestions:** `sfx_suggestions` includes `sfx_file`, `time`, `sfx_type` (transition/impact/accent/ambient), and `confidence`. Pick the high-confidence ones (> 0.7) for accent/impact types. Always propose them to the user — never place silently.

**Slow-motion on voiceless content:** all ranges are speech-free, so `speed` < 1.0 is safe anywhere. Identify the 1–3 highest-energy visual moments (typically where `sfx_suggestions` suggests an impact SFX) and apply 0.5× there.

**Still run `timeline_view`** at the beat candidates to verify visual coherence before finalizing the EDL. The beat times come from audio — the visuals may not match.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Background music (when present or requested)

Music is auto-detected at session start (see Setup). Include it in the strategy proposal; never add it silently.

**Placement:** put the music file anywhere at `<videos_dir>/` root. Any audio format works (`mp3`, `wav`, `m4a`, `aac`, `ogg`). The filename is irrelevant — any audio file at that level is treated as candidate background music.

**EDL field:**

```json
"background_music": {
  "file": "/abs/path/to/music.mp3",
  "volume": 0.15,
  "fade_in": 2.0,
  "fade_out": 3.0,
  "loop": true
}
```

- `volume`: 0.0–1.0 relative to narration. For voice-over content: 0.10–0.20. For b-roll montage: 0.30–0.60. For music video: 0.80–1.0.
- `fade_in` / `fade_out`: seconds. Match to the content feel — abrupt cuts feel wrong even at low volume.
- `loop`: set `true` if the music track is shorter than the video. `render.py` handles the loop and trims to video duration.
- `duck_narration`: set `true` to auto-duck music whenever speech is present (via `sidechaincompress`). See Ducking below.

**Ducking (auto-reduce music under narration):**

When `duck_narration: true`, `render.py` uses ffmpeg `sidechaincompress` to automatically reduce music volume whenever the narration track is audible. Requires no manual automation curves — the compressor reacts in real time. Useful for vlogs and talking heads where the music volume is set higher (0.25–0.40) but should drop out under speech.

Additional EDL fields (all optional, only read when `duck_narration: true`):
- `duck_threshold`: linear amplitude threshold above which ducking engages. Default `0.02` (~-34 dBFS). Increase if music ducks during soft breaths, decrease if speech doesn't trigger it.
- `duck_attack_ms`: time to ramp down music when speech starts. Default `200`. Faster feels abrupt; slower feels natural.
- `duck_release_ms`: time to ramp back up after speech stops. Default `800`. Longer release sounds more natural.

```json
"background_music": {
  "file": "/abs/path/music.mp3",
  "volume": 0.35,
  "fade_in": 2.0,
  "fade_out": 3.0,
  "loop": true,
  "duck_narration": true,
  "duck_threshold": 0.02,
  "duck_attack_ms": 200,
  "duck_release_ms": 800
}
```

**Volume guidance by content type:**

| Content | Music volume | Rationale |
|---------|-------------|-----------|
| Training / tutorial with narration | 0.10–0.15 | Music must never fight the voice |
| Vlog / talking head | 0.15–0.25 | Presence without distraction |
| B-roll / action montage | 0.40–0.70 | Music carries the energy |
| Product reveal / intro sequence | 0.60–0.90 | Music is the feature |

**After loudnorm**, the full mix (narration + music) is normalized to −14 LUFS. A music volume of 0.15 typically survives this without washing out the voice. If the result feels too loud or too quiet, adjust `volume` and re-render.

## Sound effects (when present or requested)

Sound effects live in `<videos_dir>/sfx/`. Filenames are the spec — name them descriptively: `whoosh_transition.mp3`, `impact_hit.wav`, `cash_register.mp3`, `crowd_cheer.wav`.

At session start, list the sfx folder and read the filenames. Propose placement in the strategy phase with the output timestamp and rationale. Never add SFX silently.

**EDL field:**

```json
"sound_effects": [
  {"file": "sfx/whoosh_transition.mp3", "start_in_output": 5.2,  "volume": 0.8},
  {"file": "sfx/impact_hit.wav",        "start_in_output": 24.4, "volume": 1.0}
]
```

- `file`: absolute path or relative to `edit/` (same resolution as overlays).
- `start_in_output`: seconds in the final output timeline where the SFX begins.
- `volume`: 0.0–1.0. Most SFX sit at 0.6–1.0. Ambient SFX (crowd, room tone) at 0.2–0.4.

**Common placement patterns:**
- Transition whoosh: 0.1–0.2s before the cut it punctuates
- Impact/hit: exactly at the visual hit frame (use `timeline_view` to find it)
- Ambient: `start_in_output: 0` so it starts with the video

## Subtitles — disabling

To render without subtitles, set `"subtitles_enabled": false` in the EDL, or pass `--no-subtitles` to `render.py`. Either disables all subtitle processing including SRT generation.

When the user says "no subtitles" or "clean video", set `subtitles_enabled: false` in the EDL before rendering. Do not omit the key — explicit is safer than implicit.

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Subtitles have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per line), **case** (UPPER/Title/Natural), and **placement** (margin from bottom). The right combo depends on content.

**Worked styles** — pick, adapt, or invent:

**`bold-overlay`** — short-form tech launch, fast-paced social. 2-word chunks, UPPERCASE, break on punctuation, Helvetica 18 Bold, white-on-outline, `MarginV=35`. `render.py` ships with this as `SUB_FORCE_STYLE`.

```
FontName=Helvetica,FontSize=18,Bold=1,
PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,
BorderStyle=1,Outline=2,Shadow=0,
Alignment=2,MarginV=35
```

**`natural-sentence`** (if you invent this mode) — narrative, documentary, education. 4–7 word chunks, sentence case, break on natural pauses, `MarginV=60–80`, larger font for readability, slightly wider max-width. No shipped force_style — design one if you need it.

Invent a third style if neither fits. Hard rules: subtitles LAST (Rule 1), output-timeline offsets (Rule 5).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` defaults the scale to 1080p from any source; pass `--filter` or edit the extract command for other targets. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 2,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42,  "end": 6.85,
     "beat": "HOOK",     "speed": 1.0,  "quote": "...", "reason": "Cleanest delivery."},
    {"source": "C0103", "start": 33.10, "end": 36.00,
     "beat": "SHAKE_ACTION", "speed": 0.5,
     "effect": "dreamlike", "effect_opacity": 0.7,
     "quote": "", "reason": "2x slo-mo hero shot with sparkle + bloom."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "edit/animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "background_music": {
    "file": "/abs/path/to/music.mp3",
    "volume": 0.15,
    "fade_in": 2.0,
    "fade_out": 3.0,
    "loop": true,
    "duck_narration": false,
    "duck_threshold": 0.02,
    "duck_attack_ms": 200,
    "duck_release_ms": 800
  },
  "sound_effects": [
    {"file": "sfx/whoosh.mp3", "start_in_output": 5.2, "volume": 0.8}
  ],
  "subtitles": "edit/master.srt",
  "subtitles_enabled": true,
  "total_duration_s": 87.4
}
```

- `ranges[].speed`: optional float, default 1.0. Values < 1 = slow-motion. Only safe on silent (non-speech) ranges — see Hard Rule 13.
- `ranges[].effect`: optional string, default "". Visual effect baked into segment: "bloom", "sparkle", "dreamlike". See Approach slo-mo + effects.
- `ranges[].effect_opacity`: optional float, default 0.7. Sparkle overlay screen-blend opacity for sparkle/dreamlike effects.
- `background_music`: optional object. Omit to disable music entirely. Set `duck_narration: true` to auto-duck under speech.
- `sound_effects`: optional array. Omit or empty array to disable SFX.
- `subtitles_enabled`: optional bool, default true. Set false to suppress all subtitle processing.
- `grade` is a preset name or raw ffmpeg filter. `overlays` are rendered animation clips. `subtitles` is applied LAST (Rule 1).

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use hosted Scribe.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
