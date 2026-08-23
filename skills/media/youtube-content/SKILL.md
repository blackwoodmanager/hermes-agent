---
name: youtube-content
description: "YouTube: transcripts, metadata, audio, video, frames."
version: 2.0.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [YouTube, Video, Transcripts, Media, Download, Audio]
    related_skills: []
---

# YouTube Content Tool

## When to use

Any time a YouTube link shows up. Someone pastes a URL with no instructions,
asks what a video is about, wants a summary, transcript, chapters or quotes,
asks who made it or when, wants the audio or the video file, or wants to know
what is actually *shown* on screen.

Never answer "I can't open YouTube links". This skill covers the whole path:
metadata and captions first, audio plus speech-to-text when a video has no
captions, and frame extraction when the answer is visual rather than spoken.

## Setup

Two dependencies, both optional-but-recommended:

```bash
uv pip install yt-dlp                    # metadata, captions, audio, video
uv pip install youtube-transcript-api    # second caption backend
```

`youtube_media.py` installs yt-dlp itself on first use when it is missing.
ffmpeg is only needed for audio format conversion, best-quality video merging
and frame extraction — everything else works without it.

Check the environment before blaming a video:

```bash
uv run python3 SKILL_DIR/scripts/youtube_media.py check
```

## Start here

`SKILL_DIR` is the directory containing this SKILL.md. Every subcommand
accepts any YouTube URL form — `watch?v=`, `youtu.be`, `shorts`, `embed`,
`live`, or a bare 11-character video ID.

For a pasted link with no further instruction, run **one** command:

```bash
uv run python3 SKILL_DIR/scripts/youtube_media.py brief "URL"
```

`brief` returns title, channel, upload date, duration, view/like counts,
tags, chapters, description **and** the transcript in a single JSON object.
That is usually everything needed to answer the question.

## Decision tree

1. Run `brief`.
2. `transcript.available` is `true` → answer from the transcript. Done.
3. `transcript.code` is `no_captions` → the video has no subtitles at all.
   Download the audio and transcribe it:
   ```bash
   uv run python3 SKILL_DIR/scripts/youtube_media.py audio "URL"
   ```
   Then transcribe the returned `path` with the Hermes speech-to-text stack
   (`transcribe_audio`). Tell the user this is happening — on a long video it
   takes a while.
4. The question is about what is *shown* (a chart, a UI, a place, a face),
   not what is said → extract frames and look at them:
   ```bash
   uv run python3 SKILL_DIR/scripts/youtube_media.py frames "URL" --count 12
   ```
   Then read the returned image paths with the vision tool.
5. The user wants the file itself → `audio` or `video`.

## Commands

```bash
# Metadata only — cheap, no captions fetched
youtube_media.py info "URL" [--full-description]

# Captions
youtube_media.py transcript "URL" [--language ru,en] [--timestamps] [--text-only] [--save]

# Metadata + captions in one extraction (preferred)
youtube_media.py brief "URL" [--language ru,en] [--timestamps]

# Audio for speech-to-text (native m4a by default; --format needs ffmpeg)
youtube_media.py audio "URL" [--format mp3] [--out DIR]

# Video file
youtube_media.py video "URL" [--max-height 720] [--out DIR]

# Evenly spaced stills for a vision model
youtube_media.py frames "URL" [--count 12] [--from-file PATH]

# Dependency / configuration report
youtube_media.py check
```

Downloads land in `$HERMES_HOME/media/youtube/<video_id>/` unless `--out`
says otherwise. `HERMES_YOUTUBE_DIR` overrides the base directory.

Every command prints one JSON object. On failure it prints
`{"error": ..., "code": ..., "hint": ...}` and exits non-zero — branch on
`code`, and pass `hint` on to the user when they need to act.

## Long transcripts

`transcript` and `brief` truncate at 120K characters by default and set
`truncated: true`, writing the full text to `transcript_file`. Raise or
remove the cap with `--max-chars N` (`0` = no limit), or read the file in
chunks. Above ~50K characters, summarize in ~40K chunks with ~2K overlap and
merge, rather than trying to hold the whole thing at once.

## Failure codes and what to do

| `code` | Meaning | Action |
|---|---|---|
| `no_captions` | Video has no subtitles | Fall back to `audio` + speech-to-text |
| `bot_check` | YouTube demands proof the caller is not a bot | Supply cookies (below); upgrade yt-dlp if it is old |
| `network_blocked` | Host cannot reach YouTube at all | Egress policy or proxy problem, not the video |
| `geo_blocked` | Blocked in the host's region | Route through `--proxy` |
| `age_restricted` / `members_only` / `private` | Needs a signed-in account | Cookies for an account with access |
| `unavailable` | Removed, or the ID is wrong | Ask the user to re-check the link |
| `live` | Stream has not finished | No complete transcript exists yet |
| `dependency_missing` | yt-dlp absent and auto-install failed | Run the install command in `hint` |
| `ffmpeg_missing` | Conversion or frames need ffmpeg | Install ffmpeg, or skip conversion |

### Bot checks — the common one on a server

A Hermes running on a VPS shares a datacenter IP, and YouTube challenges
those far more often than home connections. Fixes, in order of effort:

1. Upgrade yt-dlp — `uv pip install -U yt-dlp`. Extraction breaks whenever
   YouTube changes its player, and upstream ships fixes within days.
2. Cookies from a signed-in browser: export `cookies.txt` and either pass
   `--cookies /path/cookies.txt` or set `HERMES_YOUTUBE_COOKIES` once so
   every call inherits it. On a desktop, `--cookies-from-browser chrome`
   reads them directly.
3. A residential proxy via `--proxy URL` or `HERMES_YOUTUBE_PROXY`.

Configuration environment variables: `HERMES_YOUTUBE_COOKIES`,
`HERMES_YOUTUBE_COOKIES_FROM_BROWSER`, `HERMES_YOUTUBE_PROXY`,
`HERMES_YOUTUBE_DIR`.

## Output formats

Once the transcript is in hand, shape it to what was asked:

- **Chapters**: group by topic shifts, output a timestamped chapter list.
  Prefer the video's own `chapters` from `info`/`brief` when it has them.
- **Summary**: concise 5-10 sentence overview.
- **Chapter summaries**: chapters, each with a short paragraph.
- **Thread**: numbered posts, each under 280 characters.
- **Blog post**: title, sections, key takeaways.
- **Quotes**: notable quotes with timestamps.

See `references/output-formats.md` for worked examples.

### Example — chapters output

```
00:00 Introduction — host opens with the problem statement
03:45 Background — prior work and why existing solutions fall short
12:20 Core method — walkthrough of the proposed approach
24:10 Results — benchmark comparisons and key takeaways
31:55 Q&A — audience questions on scalability and next steps
```

## Captions-only path

`scripts/fetch_transcript.py` remains for caption-only use. It tries
`youtube-transcript-api` first and falls back to yt-dlp when that backend is
blocked:

```bash
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --text-only --timestamps
```

`youtube_media.py` is the better default — same captions, plus everything
else about the video.
