#!/usr/bin/env python3
"""
Fetch a YouTube video transcript and output it as structured JSON.

Usage:
    uv run python3 fetch_transcript.py <url_or_video_id> [--language en,tr] [--timestamps]

Output (JSON):
    {
        "video_id": "...",
        "language": "en",
        "segments": [{"text": "...", "start": 0.0, "duration": 2.5}, ...],
        "full_text": "complete transcript as plain text",
        "timestamped_text": "00:00 first line\n00:05 second line\n..."
    }

Two backends, tried in order:

1. ``youtube-transcript-api`` — small and fast, but its single request path
   is the one YouTube blocks hardest from datacenter IPs, which is exactly
   where a self-hosted Hermes usually runs.
2. ``yt-dlp`` via ``yt_common`` — more extraction routes, plus cookie and
   proxy support, so it frequently succeeds where the first backend is
   blocked.

This script covers captions only. For metadata, chapters, audio for
speech-to-text, video files or frames, use ``youtube_media.py``.

Install dependencies:  uv pip install youtube-transcript-api yt-dlp
"""

import argparse
import json
import re
import sys
from pathlib import Path

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


def extract_video_id(url_or_id: str) -> str:
    """Extract the 11-character video ID from various YouTube URL formats."""
    url_or_id = url_or_id.strip()
    patterns = [
        r'(?:v=|youtu\.be/|shorts/|embed/|live/)([a-zA-Z0-9_-]{11})',
        r'^([a-zA-Z0-9_-]{11})$',
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    return url_or_id


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS or MM:SS format."""
    total = int(seconds)
    h, remainder = divmod(total, 3600)
    m, s = divmod(remainder, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _fetch_via_transcript_api(video_id: str, languages: list = None):
    """Fetch through youtube-transcript-api. Raises on any failure."""
    from youtube_transcript_api import YouTubeTranscriptApi

    api = YouTubeTranscriptApi()
    result = api.fetch(video_id, languages=languages) if languages else api.fetch(video_id)

    # v1.x returns FetchedTranscriptSnippet objects; normalize to dicts
    return [
        {"text": seg.text, "start": seg.start, "duration": seg.duration}
        for seg in result
    ]


def _fetch_via_yt_dlp(video_id: str, languages: list = None):
    """Fetch through yt-dlp's caption tracks. Raises YouTubeError on failure."""
    from yt_common import (
        YouTubeError,
        build_ydl_opts,
        canonical_url,
        download_caption_track,
        extract_info,
        pick_caption_track,
    )

    opts = build_ydl_opts()
    info = extract_info(canonical_url(video_id), opts)
    track = pick_caption_track(info, languages)
    if track is None:
        raise YouTubeError(
            "no_captions",
            "This video has no captions in any language.",
            hint="Download the audio with youtube_media.py and transcribe it "
            "with the Hermes speech-to-text stack.",
        )
    return download_caption_track(track, opts)


def fetch_transcript(video_id: str, languages: list = None):
    """Fetch transcript segments for a video, trying both backends.

    Returns a list of dicts with 'text', 'start', and 'duration' keys.
    """
    try:
        return _fetch_via_transcript_api(video_id, languages)
    except ImportError:
        first_error = RuntimeError(
            "youtube-transcript-api is not installed "
            "(uv pip install youtube-transcript-api)"
        )
    except Exception as exc:
        first_error = exc

    try:
        return _fetch_via_yt_dlp(video_id, languages)
    except Exception as exc:
        # yt-dlp's classifier produces the more actionable message of the two
        # (bot check, geo block, no captions), so it is the one to surface.
        raise exc from first_error


def main():
    parser = argparse.ArgumentParser(description="Fetch YouTube transcript as JSON")
    parser.add_argument("url", help="YouTube URL or video ID")
    parser.add_argument("--language", "-l", default=None,
                        help="Comma-separated language codes (e.g. en,tr). Default: auto")
    parser.add_argument("--timestamps", "-t", action="store_true",
                        help="Include timestamped text in output")
    parser.add_argument("--text-only", action="store_true",
                        help="Output plain text instead of JSON")
    args = parser.parse_args()

    video_id = extract_video_id(args.url)
    languages = [l.strip() for l in args.language.split(",")] if args.language else None

    try:
        segments = fetch_transcript(video_id, languages)
    except Exception as e:
        payload = getattr(e, "to_dict", None)
        if callable(payload):
            print(json.dumps(payload(), ensure_ascii=False))
            sys.exit(1)
        error_msg = str(e)
        if "disabled" in error_msg.lower():
            print(json.dumps({"error": "Transcripts are disabled for this video."}))
        elif "no transcript" in error_msg.lower():
            print(json.dumps({"error": "No transcript found. Try specifying a language with --language."}))
        else:
            print(json.dumps({"error": error_msg}))
        sys.exit(1)

    full_text = " ".join(seg["text"] for seg in segments)
    timestamped = "\n".join(
        f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
    )

    if args.text_only:
        print(timestamped if args.timestamps else full_text)
        return

    result = {
        "video_id": video_id,
        "segment_count": len(segments),
        "duration": format_timestamp(segments[-1]["start"] + segments[-1]["duration"]) if segments else "0:00",
        "full_text": full_text,
    }
    if args.timestamps:
        result["timestamped_text"] = timestamped

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
