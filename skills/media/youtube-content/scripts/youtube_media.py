#!/usr/bin/env python3
"""YouTube metadata, captions, audio, video and frames — one CLI.

The agent-facing entry point of the youtube-content skill. Every subcommand
prints a single JSON object on stdout and exits 0 on success, or prints
``{"error": ..., "code": ..., "hint": ...}`` and exits 1 on failure, so the
caller never has to scrape human prose to decide what to do next.

    youtube_media.py check                      # environment / dependency report
    youtube_media.py info URL                   # title, channel, chapters, description
    youtube_media.py transcript URL [--language ru,en] [--timestamps] [--text-only]
    youtube_media.py brief URL                  # metadata + transcript in one call
    youtube_media.py audio URL                  # download audio for speech-to-text
    youtube_media.py video URL [--max-height 720]
    youtube_media.py frames URL [--count 12]    # evenly spaced JPEG stills

``brief`` is the one to reach for when a user simply pastes a link: it
returns everything readable about the video, and when the video has no
captions it says so in ``next_step`` rather than failing.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = str(Path(__file__).resolve().parent)
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from _hermes_home import media_dir
from yt_common import (
    YT_DLP_SPEC,
    YouTubeError,
    build_ydl_opts,
    canonical_url,
    classify_error,
    download_caption_track,
    ensure_yt_dlp,
    extract_info,
    extract_video_id,
    ffmpeg_path,
    format_timestamp,
    pick_caption_track,
    segments_to_text,
    summarize_metadata,
    yt_dlp_version,
)

# Transcripts long enough to blow a context window are truncated by default;
# the full text is always on disk, and --max-chars 0 disables the cap.
DEFAULT_MAX_CHARS = 120_000


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _fail(error: YouTubeError) -> int:
    _emit(error.to_dict())
    return 1


def _opts_from_args(args: argparse.Namespace, extra: Optional[dict] = None) -> dict:
    return build_ydl_opts(
        cookies=getattr(args, "cookies", None),
        cookies_from_browser=getattr(args, "cookies_from_browser", None),
        proxy=getattr(args, "proxy", None),
        extra=extra,
    )


def _output_dir(args: argparse.Namespace, video_id: str) -> Path:
    target = Path(args.out).expanduser() if getattr(args, "out", None) else media_dir(video_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _languages(args: argparse.Namespace) -> Optional[list[str]]:
    raw = getattr(args, "language", None)
    if not raw:
        return None
    return [part.strip() for part in raw.split(",") if part.strip()]


# ────────────────────────────────── commands ───────────────────────────────


def cmd_check(args: argparse.Namespace) -> int:
    """Report what is installed, without touching the network."""
    version = yt_dlp_version()
    ffmpeg = ffmpeg_path()
    ready = version is not None

    payload = {
        "ready": ready,
        "yt_dlp_version": version,
        "ffmpeg": ffmpeg,
        "python": sys.executable,
        "cookies": os.environ.get("HERMES_YOUTUBE_COOKIES") or None,
        "cookies_from_browser": os.environ.get("HERMES_YOUTUBE_COOKIES_FROM_BROWSER")
        or None,
        "proxy": os.environ.get("HERMES_YOUTUBE_PROXY") or None,
        "media_dir": str(media_dir()),
    }
    notes = []
    if not ready:
        notes.append(
            f"yt-dlp is missing — install it with: uv pip install --python "
            f"{sys.executable} '{YT_DLP_SPEC}'"
        )
    if not ffmpeg:
        notes.append(
            "ffmpeg is missing — audio downloads still work (native m4a), but "
            "format conversion, best-quality video merging and frame extraction "
            "do not."
        )
    payload["notes"] = notes
    _emit(payload)
    return 0 if ready else 1


def cmd_info(args: argparse.Namespace) -> int:
    info = extract_info(canonical_url(args.url), _opts_from_args(args))
    metadata = summarize_metadata(info)
    if not args.full_description and len(metadata["description"]) > 4000:
        metadata["description"] = metadata["description"][:4000] + "\n[... truncated]"
    _emit(metadata)
    return 0


def _fetch_transcript(args: argparse.Namespace, info: dict) -> dict:
    """Fetch captions for an already-extracted video, as a result dict.

    Never raises for the ordinary "this video has no captions" case — that
    is a routing decision for the caller, not an error.
    """
    track = pick_caption_track(info, _languages(args))
    if track is None:
        return {
            "available": False,
            "code": "no_captions",
            "reason": "This video has no captions in any language.",
            "next_step": (
                "Download the audio (`youtube_media.py audio URL`) and transcribe it "
                "with the Hermes speech-to-text stack."
            ),
        }

    opts = _opts_from_args(args)
    segments = download_caption_track(track, opts)
    max_chars = args.max_chars if args.max_chars is not None else DEFAULT_MAX_CHARS

    full_text = segments_to_text(segments, timestamps=False)
    timestamped = segments_to_text(segments, timestamps=True)
    truncated = False
    if max_chars and len(full_text) > max_chars:
        truncated = True

    result = {
        "available": True,
        "language": track["language"],
        "kind": track["kind"],
        "source": "youtube-captions",
        "segment_count": len(segments),
        "character_count": len(full_text),
        "truncated": truncated,
    }
    if segments:
        last = segments[-1]
        result["duration"] = format_timestamp(last["start"] + last["duration"])

    body = timestamped if args.timestamps else full_text
    if truncated:
        result["text"] = body[:max_chars] + "\n[... truncated — see transcript_file]"
    else:
        result["text"] = body

    if args.save or truncated:
        target = _output_dir(args, info.get("id", "") or extract_video_id(args.url))
        transcript_file = target / "transcript.txt"
        transcript_file.write_text(timestamped, encoding="utf-8")
        result["transcript_file"] = str(transcript_file)
    return result


def cmd_transcript(args: argparse.Namespace) -> int:
    info = extract_info(canonical_url(args.url), _opts_from_args(args))
    result = _fetch_transcript(args, info)

    if args.text_only:
        if not result.get("available"):
            print(result["reason"], file=sys.stderr)
            print(result["next_step"], file=sys.stderr)
            return 1
        print(result["text"])
        return 0

    result["video_id"] = info.get("id", "")
    result["title"] = info.get("title", "")
    _emit(result)
    return 0 if result.get("available") else 1


def cmd_brief(args: argparse.Namespace) -> int:
    """Metadata plus transcript in a single extraction — the default entry."""
    info = extract_info(canonical_url(args.url), _opts_from_args(args))
    payload = summarize_metadata(info)
    if len(payload["description"]) > 4000 and not args.full_description:
        payload["description"] = payload["description"][:4000] + "\n[... truncated]"

    try:
        payload["transcript"] = _fetch_transcript(args, info)
    except YouTubeError as exc:
        payload["transcript"] = dict(exc.to_dict(), available=False)
    _emit(payload)
    return 0


def _download(args: argparse.Namespace, fmt: str, extra: Optional[dict] = None) -> dict:
    """Run a yt-dlp download and report where the file landed."""
    yt_dlp = ensure_yt_dlp()
    url = canonical_url(args.url)
    video_id = extract_video_id(args.url)
    target = _output_dir(args, video_id)

    opts = _opts_from_args(
        args,
        extra={
            "format": fmt,
            "outtmpl": {"default": str(target / "%(title).120B [%(id)s].%(ext)s")},
            "restrictfilenames": True,
            "overwrites": False,
            "continuedl": True,
            **(extra or {}),
        },
    )
    if getattr(args, "max_filesize", None):
        opts["max_filesize"] = args.max_filesize

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info is None:
                raise YouTubeError("download_failed", "yt-dlp returned no result.")
            if info.get("_type") == "playlist" and info.get("entries"):
                info = [entry for entry in info["entries"] if entry][0]
            path = Path(ydl.prepare_filename(info))
    except YouTubeError:
        raise
    except Exception as exc:
        raise classify_error(exc) from exc

    # Post-processing (audio extraction, muxing) renames the output, so trust
    # the requested-downloads record when yt-dlp provides one.
    requested = (info.get("requested_downloads") or [{}])[0]
    final = requested.get("filepath") or requested.get("_filename")
    if final:
        path = Path(final)
    if not path.exists():
        # restrictfilenames may rewrite the template's punctuation, so match
        # on the video ID alone rather than the exact bracketed form.
        matches = sorted(target.glob(f"*{info.get('id', video_id)}*"))
        if matches:
            path = matches[0]

    return {
        "video_id": info.get("id", video_id),
        "title": info.get("title", ""),
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
        "duration_seconds": info.get("duration"),
        "duration": format_timestamp(info.get("duration") or 0),
    }


def cmd_audio(args: argparse.Namespace) -> int:
    """Download audio only — the input the STT stack wants."""
    extra: dict = {}
    if args.format:
        if not ffmpeg_path():
            raise YouTubeError(
                "ffmpeg_missing",
                f"Converting audio to {args.format} needs ffmpeg, which is not installed.",
                hint="Install ffmpeg, or drop --format to keep YouTube's native m4a "
                "(the STT stack accepts it).",
            )
        extra["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": args.format,
                "preferredquality": "0",
            }
        ]

    result = _download(args, "bestaudio[ext=m4a]/bestaudio/best", extra)
    result["kind"] = "audio"
    result["next_step"] = (
        "Transcribe this file with the Hermes speech-to-text stack "
        "(tools/transcription_tools.py -> transcribe_audio)."
    )
    _emit(result)
    return 0


def cmd_video(args: argparse.Namespace) -> int:
    height = args.max_height
    if ffmpeg_path():
        # Separate streams give the best quality but need ffmpeg to mux.
        fmt = (
            f"bestvideo[height<={height}]+bestaudio/"
            f"best[height<={height}]/best"
        )
        extra = {"merge_output_format": "mp4"}
    else:
        fmt = f"best[height<={height}][ext=mp4]/best[height<={height}]/best"
        extra = {}

    result = _download(args, fmt, extra)
    result["kind"] = "video"
    result["max_height"] = height
    _emit(result)
    return 0


def cmd_frames(args: argparse.Namespace) -> int:
    """Extract evenly spaced stills so a vision model can look at the video."""
    ffmpeg = ffmpeg_path()
    if not ffmpeg:
        raise YouTubeError(
            "ffmpeg_missing",
            "Frame extraction needs ffmpeg, which is not installed.",
            hint="Install ffmpeg (apt install ffmpeg / brew install ffmpeg), then retry.",
        )

    if args.from_file:
        source = Path(args.from_file).expanduser()
        if not source.exists():
            raise YouTubeError("file_not_found", f"No such file: {source}")
        video_id = source.stem
        duration = _probe_duration(source)
        target = _output_dir(args, video_id)
    else:
        download = _download(
            args,
            f"best[height<={args.max_height}][ext=mp4]/best[height<={args.max_height}]/best",
        )
        source = Path(download["path"])
        video_id = download["video_id"]
        duration = download.get("duration_seconds") or _probe_duration(source)
        target = source.parent

    if not duration:
        raise YouTubeError(
            "unknown_duration",
            "Could not determine the video duration, so frames cannot be spaced evenly.",
        )

    frames_dir = target / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    count = max(1, args.count)
    # Sample inside the video rather than at 0 and at the very last frame,
    # where seeking often lands on black.
    step = duration / (count + 1)
    frames = []
    for index in range(1, count + 1):
        position = step * index
        out_path = frames_dir / f"frame_{index:03d}.jpg"
        command = [
            ffmpeg, "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{position:.2f}", "-i", str(source),
            "-frames:v", "1", "-q:v", "3", str(out_path),
        ]
        try:
            subprocess.run(command, check=True, capture_output=True, timeout=120)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            detail = getattr(exc, "stderr", b"") or b""
            raise YouTubeError(
                "frame_extraction_failed",
                f"ffmpeg failed at {format_timestamp(position)}: "
                f"{detail.decode('utf-8', 'replace').strip()[:300]}",
            ) from exc
        if out_path.exists():
            frames.append(
                {
                    "path": str(out_path),
                    "timestamp": format_timestamp(position),
                    "seconds": round(position, 2),
                }
            )

    _emit(
        {
            "video_id": video_id,
            "source": str(source),
            "duration": format_timestamp(duration),
            "frame_count": len(frames),
            "frames": frames,
            "next_step": "Read these image files with the vision tool to describe the video.",
        }
    )
    return 0


def _probe_duration(path: Path) -> float:
    """Duration in seconds via ffprobe, or 0.0 when it cannot be determined."""
    import shutil

    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return 0.0
    try:
        output = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, timeout=60,
        )
        return float(output.stdout.decode().strip())
    except Exception:
        return 0.0


# ─────────────────────────────────── parser ────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="youtube_media.py",
        description="Fetch YouTube metadata, captions, audio, video and frames.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_network_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--cookies", help="Netscape cookies.txt for a signed-in account")
        sub.add_argument(
            "--cookies-from-browser",
            help="Read cookies from an installed browser (chrome, firefox, edge, ...)",
        )
        sub.add_argument("--proxy", help="Proxy URL for YouTube requests")

    def add_transcript_flags(sub: argparse.ArgumentParser) -> None:
        sub.add_argument(
            "--language", "-l",
            help="Preferred caption languages, best first (e.g. ru,en). Default: any",
        )
        sub.add_argument("--timestamps", "-t", action="store_true",
                         help="Prefix each line with its timestamp")
        sub.add_argument("--max-chars", type=int, default=None,
                         help=f"Truncate at N characters (default {DEFAULT_MAX_CHARS}; 0 = no limit)")
        sub.add_argument("--save", action="store_true",
                         help="Always write the full transcript to a file")
        sub.add_argument("--out", help="Output directory (default: $HERMES_HOME/media/youtube/<id>)")

    check = subparsers.add_parser("check", help="Report dependency and configuration state")
    check.set_defaults(func=cmd_check)

    info = subparsers.add_parser("info", help="Video metadata, chapters and description")
    info.add_argument("url", help="YouTube URL or video ID")
    info.add_argument("--full-description", action="store_true",
                      help="Do not truncate the description")
    add_network_flags(info)
    info.set_defaults(func=cmd_info)

    transcript = subparsers.add_parser("transcript", help="Captions as text or JSON")
    transcript.add_argument("url", help="YouTube URL or video ID")
    transcript.add_argument("--text-only", action="store_true",
                            help="Print the transcript alone, no JSON wrapper")
    add_transcript_flags(transcript)
    add_network_flags(transcript)
    transcript.set_defaults(func=cmd_transcript)

    brief = subparsers.add_parser(
        "brief", help="Metadata and transcript together — use this for a pasted link"
    )
    brief.add_argument("url", help="YouTube URL or video ID")
    brief.add_argument("--full-description", action="store_true",
                       help="Do not truncate the description")
    add_transcript_flags(brief)
    add_network_flags(brief)
    brief.set_defaults(func=cmd_brief)

    audio = subparsers.add_parser("audio", help="Download audio for speech-to-text")
    audio.add_argument("url", help="YouTube URL or video ID")
    audio.add_argument("--format", choices=["mp3", "m4a", "opus", "wav", "flac"],
                       help="Convert to this codec (needs ffmpeg). Default: native m4a")
    audio.add_argument("--out", help="Output directory")
    audio.add_argument("--max-filesize", type=int, help="Abort above this many bytes")
    add_network_flags(audio)
    audio.set_defaults(func=cmd_audio)

    video = subparsers.add_parser("video", help="Download the video file")
    video.add_argument("url", help="YouTube URL or video ID")
    video.add_argument("--max-height", type=int, default=720,
                       help="Cap the vertical resolution (default 720)")
    video.add_argument("--out", help="Output directory")
    video.add_argument("--max-filesize", type=int, help="Abort above this many bytes")
    add_network_flags(video)
    video.set_defaults(func=cmd_video)

    frames = subparsers.add_parser("frames", help="Extract evenly spaced stills")
    frames.add_argument("url", nargs="?", default="", help="YouTube URL or video ID")
    frames.add_argument("--from-file", help="Use an already-downloaded file instead")
    frames.add_argument("--count", type=int, default=12, help="How many frames (default 12)")
    frames.add_argument("--max-height", type=int, default=480,
                        help="Resolution cap for the working download (default 480)")
    frames.add_argument("--out", help="Output directory")
    frames.add_argument("--max-filesize", type=int, help="Abort above this many bytes")
    add_network_flags(frames)
    frames.set_defaults(func=cmd_frames)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "frames" and not args.url and not args.from_file:
        _emit({"error": "frames needs either a URL or --from-file.", "code": "bad_arguments"})
        return 1
    try:
        return args.func(args)
    except YouTubeError as exc:
        return _fail(exc)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — always report as structured JSON
        return _fail(classify_error(exc))


if __name__ == "__main__":
    sys.exit(main())
