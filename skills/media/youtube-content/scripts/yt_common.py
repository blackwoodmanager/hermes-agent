"""Shared yt-dlp plumbing for the youtube-content skill scripts.

Everything that talks to YouTube goes through here so the CLI scripts stay
thin and the awkward parts — bot checks, cookie plumbing, caption formats,
error remediation — live in exactly one place.

Why yt-dlp and not just ``youtube-transcript-api``:

* ``youtube-transcript-api`` only ever returns captions, and only for videos
  that have them. It has no metadata, no audio, no video.
* Its single request path is the one YouTube blocks hardest from datacenter
  IPs, which is precisely where a self-hosted Hermes usually runs. The
  failure surfaces as an opaque ``RequestBlocked`` and the agent concludes
  "I can't do YouTube".

yt-dlp keeps working in more of those cases (multiple player clients,
cookies, proxy support), and when captions genuinely do not exist it can
still hand back the audio so the STT stack can produce a transcript.

Nothing here imports ``yt_dlp`` at module scope: the scripts must be able to
print a useful "run this to install it" message rather than dying on an
ImportError.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Iterable, Optional

# yt-dlp is deliberately NOT exact-pinned. YouTube changes its player often
# enough that a pinned yt-dlp becomes a broken yt-dlp; upstream ships the
# fix within days. The floor is the oldest release we test against. Keep in
# sync with LAZY_DEPS["media.yt_dlp"] in tools/lazy_deps.py.
YT_DLP_SPEC = "yt-dlp>=2026.7.4"

_VIDEO_ID = r"[a-zA-Z0-9_-]{11}"
_ID_PATTERNS = (
    re.compile(rf"(?:v=|youtu\.be/|shorts/|embed/|live/|/v/)({_VIDEO_ID})"),
    re.compile(rf"^({_VIDEO_ID})$"),
)


class YouTubeError(RuntimeError):
    """A failure worth reporting to the user with a remediation hint.

    ``code`` is a stable machine-readable slug so callers can branch on the
    failure (e.g. "no captions" means "fall back to audio + STT") without
    string-matching yt-dlp's prose.
    """

    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        payload = {"error": self.message, "code": self.code}
        if self.hint:
            payload["hint"] = self.hint
        return payload


# ─────────────────────────── URLs and timestamps ───────────────────────────


def extract_video_id(url_or_id: str) -> str:
    """Extract the 11-character video ID from any common YouTube URL form.

    Returns the input unchanged when nothing matches, so callers can still
    hand it to yt-dlp (playlist URLs, channel URLs, non-YouTube links).
    """
    candidate = (url_or_id or "").strip()
    for pattern in _ID_PATTERNS:
        match = pattern.search(candidate)
        if match:
            return match.group(1)
    return candidate


def canonical_url(url_or_id: str) -> str:
    """Return a watch URL for a bare video ID, or the input URL as given."""
    candidate = (url_or_id or "").strip()
    if re.fullmatch(_VIDEO_ID, candidate):
        return f"https://www.youtube.com/watch?v={candidate}"
    return candidate


def format_timestamp(seconds: float) -> str:
    """Convert seconds to H:MM:SS, or M:SS for anything under an hour."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ────────────────────────────── dependencies ───────────────────────────────


def _install_yt_dlp() -> bool:
    """Install yt-dlp into the interpreter running this script.

    Prefers Hermes' own lazy-install path when the repo is importable: it
    honours ``security.allow_lazy_installs`` and redirects to the durable
    package target on sealed Docker images, where writing to the venv fails.
    Falls back to pip, then to ``uv pip`` (the Docker venv is built with
    ``uv sync`` and has no pip).
    """
    try:
        from tools.lazy_deps import FeatureUnavailable, ensure  # type: ignore

        try:
            ensure("media.yt_dlp", prompt=False)
            return True
        except FeatureUnavailable:
            return False
    except (ModuleNotFoundError, ImportError):
        pass

    commands: list[list[str]] = [
        [sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", YT_DLP_SPEC],
    ]
    uv = shutil.which("uv")
    if uv:
        commands.append(
            [uv, "pip", "install", "--python", sys.executable, "--quiet",
             "--upgrade", YT_DLP_SPEC]
        )

    for command in commands:
        try:
            subprocess.check_call(command, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            return True
        except (subprocess.CalledProcessError, OSError):
            continue
    return False


def ensure_yt_dlp(auto_install: bool = True):
    """Import and return the ``yt_dlp`` module, installing it if needed."""
    try:
        import yt_dlp  # type: ignore

        return yt_dlp
    except ImportError:
        pass

    sealed = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS") == "1"
    if auto_install and not sealed and _install_yt_dlp():
        try:
            import yt_dlp  # type: ignore

            return yt_dlp
        except ImportError:
            pass

    raise YouTubeError(
        "dependency_missing",
        "yt-dlp is not installed, so YouTube metadata, captions and downloads "
        "are unavailable.",
        hint=f"Install it with: uv pip install --python {sys.executable} '{YT_DLP_SPEC}'",
    )


def yt_dlp_version() -> Optional[str]:
    """Installed yt-dlp version, or None when it is not importable."""
    try:
        import yt_dlp  # type: ignore

        return getattr(yt_dlp.version, "__version__", None)
    except Exception:
        return None


def ffmpeg_path() -> Optional[str]:
    """Path to ffmpeg, or None. Audio conversion and frames need it."""
    return shutil.which("ffmpeg")


# ──────────────────────────── extraction options ───────────────────────────

# YouTube's default (web) player client is the one that gets bot-checked
# first from datacenter IPs. When it fails we retry through clients that
# carry different signatures before giving up.
_FALLBACK_PLAYER_CLIENTS = ["tv", "mweb", "web_safari", "android"]


def build_ydl_opts(
    *,
    cookies: Optional[str] = None,
    cookies_from_browser: Optional[str] = None,
    proxy: Optional[str] = None,
    player_clients: Optional[Iterable[str]] = None,
    quiet: bool = True,
    extra: Optional[dict] = None,
) -> dict:
    """Assemble yt-dlp options, filling gaps from the environment.

    Env fallbacks (so a self-hosted deployment can configure this once and
    have every call inherit it):

    * ``HERMES_YOUTUBE_COOKIES`` — path to a Netscape cookies.txt
    * ``HERMES_YOUTUBE_COOKIES_FROM_BROWSER`` — e.g. ``chrome``, ``firefox``
    * ``HERMES_YOUTUBE_PROXY`` — proxy URL for YouTube traffic only
    """
    opts: dict[str, Any] = {
        "quiet": quiet,
        "no_warnings": quiet,
        "noprogress": True,
        # The scripts return JSON on stdout; yt-dlp must not write there.
        "logtostderr": True,
        "socket_timeout": 30,
        "retries": 3,
        "extractor_retries": 3,
        # A bare URL with a list= parameter should still mean "this video".
        "noplaylist": True,
        "ignoreerrors": False,
    }

    cookies = cookies or os.environ.get("HERMES_YOUTUBE_COOKIES", "").strip() or None
    if cookies:
        opts["cookiefile"] = cookies

    browser = (
        cookies_from_browser
        or os.environ.get("HERMES_YOUTUBE_COOKIES_FROM_BROWSER", "").strip()
        or None
    )
    if browser and not cookies:
        # yt-dlp wants a tuple: (browser, profile, keyring, container).
        opts["cookiesfrombrowser"] = (browser, None, None, None)

    proxy = proxy or os.environ.get("HERMES_YOUTUBE_PROXY", "").strip() or None
    if proxy:
        opts["proxy"] = proxy

    if player_clients:
        opts["extractor_args"] = {"youtube": {"player_client": list(player_clients)}}

    if extra:
        opts.update(extra)
    return opts


# ─────────────────────────── error classification ──────────────────────────

_ERROR_RULES: tuple[tuple[str, str, str, str], ...] = (
    (
        "sign in to confirm|not a bot|failed to extract any player response"
        "|please sign in|confirm your age",
        "bot_check",
        "YouTube is challenging this request — it wants proof the caller is not a bot. "
        "This is the usual outcome when Hermes runs on a VPS or in a datacenter.",
        "Supply cookies from a signed-in browser: export cookies.txt and pass "
        "--cookies /path/cookies.txt (or set HERMES_YOUTUBE_COOKIES). "
        "A residential proxy via HERMES_YOUTUBE_PROXY also clears it. "
        "If yt-dlp is more than a few weeks old, upgrade it first.",
    ),
    (
        "private video",
        "private",
        "This video is private.",
        "Only the owner or invited accounts can fetch it — cookies for such an "
        "account would be needed.",
    ),
    (
        "members-only|join this channel",
        "members_only",
        "This video is members-only.",
        "Pass cookies for an account with an active membership.",
    ),
    (
        "video unavailable|has been removed|no longer available|does not exist",
        "unavailable",
        "This video is unavailable or has been removed.",
        "Double-check the URL — a typo in the 11-character ID looks exactly like this.",
    ),
    (
        "available in your country|geo restricted|geo-restricted|blocked it in your country",
        "geo_blocked",
        "This video is geo-blocked for the host's location.",
        "Route the request through a proxy in an allowed region "
        "(--proxy or HERMES_YOUTUBE_PROXY).",
    ),
    (
        "age.restricted|inappropriate for some users|age.gated",
        "age_restricted",
        "This video is age-restricted.",
        "Pass cookies for a signed-in, age-verified account.",
    ),
    (
        "is live|live event will begin|premieres in",
        "live",
        "This is a live or upcoming stream, so there is no finished transcript yet.",
        "Wait for the stream to end, or download the live segment explicitly.",
    ),
    (
        "unable to connect to proxy|tunnel connection failed|connection refused"
        "|temporary failure in name resolution|network is unreachable"
        "|connection reset by peer|timed out",
        "network_blocked",
        "The host could not reach YouTube at all — the connection was refused, "
        "timed out, or a proxy rejected it.",
        "Check outbound network access from this machine. On a locked-down host "
        "or CI sandbox, youtube.com is often simply not on the egress allowlist; "
        "HERMES_YOUTUBE_PROXY can route around it.",
    ),
    (
        "unsupported url|unable to extract",
        "unsupported",
        "yt-dlp could not extract this URL.",
        "Confirm it is a video URL rather than a channel, playlist or search page. "
        "If it is a valid video, upgrade yt-dlp — extraction breaks when YouTube "
        "changes its player.",
    ),
)


def classify_error(exc: BaseException) -> YouTubeError:
    """Turn a raw yt-dlp exception into an actionable YouTubeError."""
    if isinstance(exc, YouTubeError):
        return exc

    raw = str(exc)
    # yt-dlp prefixes messages with "ERROR: [youtube] <id>: ..." — the
    # remediation reads better without it.
    cleaned = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*)?", "", raw).strip()
    lowered = cleaned.lower()

    for pattern, code, message, hint in _ERROR_RULES:
        if re.search(pattern, lowered):
            return YouTubeError(code, f"{message} (yt-dlp said: {cleaned})", hint)

    return YouTubeError("extraction_failed", cleaned or repr(exc))


def is_retryable_with_other_clients(exc: BaseException) -> bool:
    """Would retrying through a different player client plausibly help?"""
    return classify_error(exc).code in {"bot_check", "unsupported", "extraction_failed"}


# ───────────────────────────────── extraction ──────────────────────────────


def extract_info(url: str, opts: Optional[dict] = None, *, download: bool = False) -> dict:
    """Run yt-dlp's extractor, retrying through alternate player clients.

    Raises :class:`YouTubeError` with a remediation hint on failure.
    """
    yt_dlp = ensure_yt_dlp()
    base_opts = dict(opts or build_ydl_opts())
    target = canonical_url(url)

    attempts: list[dict] = [base_opts]
    if "extractor_args" not in base_opts:
        retry_opts = dict(base_opts)
        retry_opts["extractor_args"] = {
            "youtube": {"player_client": _FALLBACK_PLAYER_CLIENTS}
        }
        attempts.append(retry_opts)

    last_error: Optional[BaseException] = None
    for attempt in attempts:
        try:
            with yt_dlp.YoutubeDL(attempt) as ydl:
                info = ydl.extract_info(target, download=download)
            if info is None:
                raise YouTubeError(
                    "extraction_failed", "yt-dlp returned no information for this URL."
                )
            # A playlist/channel URL still yields entries despite noplaylist;
            # take the first video so the caller gets something usable.
            if info.get("_type") == "playlist" and info.get("entries"):
                entries = [e for e in info["entries"] if e]
                if entries:
                    info = entries[0]
            return info
        except YouTubeError:
            raise
        except Exception as exc:  # yt_dlp.utils.DownloadError and friends
            last_error = exc
            if not is_retryable_with_other_clients(exc):
                break

    raise classify_error(last_error or RuntimeError("unknown extraction failure"))


def summarize_metadata(info: dict) -> dict:
    """Project yt-dlp's very large info dict down to the useful fields."""
    duration = info.get("duration") or 0
    chapters = [
        {
            "title": chapter.get("title", ""),
            "start": chapter.get("start_time", 0),
            "start_ts": format_timestamp(chapter.get("start_time", 0) or 0),
            "end": chapter.get("end_time"),
        }
        for chapter in (info.get("chapters") or [])
    ]
    upload_date = info.get("upload_date") or ""
    if len(upload_date) == 8:  # YYYYMMDD -> YYYY-MM-DD
        upload_date = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"

    return {
        "video_id": info.get("id", ""),
        "title": info.get("title", ""),
        "url": info.get("webpage_url") or canonical_url(info.get("id", "")),
        "channel": info.get("uploader") or info.get("channel") or "",
        "channel_url": info.get("channel_url") or info.get("uploader_url") or "",
        "channel_followers": info.get("channel_follower_count"),
        "upload_date": upload_date,
        "duration_seconds": duration,
        "duration": format_timestamp(duration) if duration else "",
        "view_count": info.get("view_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "is_live": bool(info.get("is_live")),
        "was_live": bool(info.get("was_live")),
        "categories": info.get("categories") or [],
        "tags": info.get("tags") or [],
        "thumbnail": info.get("thumbnail") or "",
        "description": info.get("description") or "",
        "chapters": chapters,
        "captions": describe_caption_tracks(info),
    }


# ────────────────────────────────── captions ───────────────────────────────

# json3 carries per-segment start/duration, which is what we want. The
# others are ordered by how cheap they are to parse into the same shape.
_CAPTION_FORMAT_PRIORITY = ("json3", "srv3", "srv1", "vtt", "ttml")


def describe_caption_tracks(info: dict) -> dict:
    """Which caption languages exist, split by manual vs auto-generated."""
    return {
        "manual": sorted((info.get("subtitles") or {}).keys()),
        "automatic": sorted((info.get("automatic_captions") or {}).keys()),
    }


def _language_matches(track_language: str, wanted: str) -> bool:
    """Match ``en`` against ``en``, ``en-US``, ``en-orig`` and friends."""
    track_language = track_language.lower()
    wanted = wanted.lower()
    return track_language == wanted or track_language.startswith(f"{wanted}-")


def pick_caption_track(info: dict, languages: Optional[list[str]] = None) -> Optional[dict]:
    """Choose the best caption track available for this video.

    Preference order: a requested language as manual captions, the same
    language auto-generated, then whatever else exists (manual first).
    Returns ``{"language", "kind", "ext", "url", "name"}`` or None.
    """
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}

    def best_format(tracks: list) -> Optional[dict]:
        for ext in _CAPTION_FORMAT_PRIORITY:
            for track in tracks or []:
                if track.get("ext") == ext and track.get("url"):
                    return track
        for track in tracks or []:
            if track.get("url"):
                return track
        return None

    def search(source: dict, kind: str, wanted: Optional[str]) -> Optional[dict]:
        for language, tracks in source.items():
            if wanted is not None and not _language_matches(language, wanted):
                continue
            track = best_format(tracks)
            if track:
                return {
                    "language": language,
                    "kind": kind,
                    "ext": track.get("ext", ""),
                    "url": track["url"],
                    "name": track.get("name", ""),
                }
        return None

    for wanted in languages or []:
        for source, kind in ((manual, "manual"), (automatic, "automatic")):
            found = search(source, kind, wanted)
            if found:
                return found

    for source, kind in ((manual, "manual"), (automatic, "automatic")):
        found = search(source, kind, None)
        if found:
            return found
    return None


def _segments_from_json3(payload: str) -> list[dict]:
    data = json.loads(payload)
    segments = []
    for event in data.get("events") or []:
        text = "".join(seg.get("utf8", "") for seg in event.get("segs") or [])
        text = text.strip()
        if not text:
            continue
        start = (event.get("tStartMs") or 0) / 1000.0
        duration = (event.get("dDurationMs") or 0) / 1000.0
        segments.append({"text": text, "start": start, "duration": duration})
    return segments


_VTT_CUE = re.compile(
    r"(?P<start>\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>\d{2}:\d{2}:\d{2}[.,]\d{3})"
)
_VTT_TAG = re.compile(r"<[^>]+>")


def _vtt_seconds(stamp: str) -> float:
    hours, minutes, rest = stamp.split(":")
    seconds, _, millis = rest.replace(",", ".").partition(".")
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + int(millis or 0) / 1000.0


def _segments_from_vtt(payload: str) -> list[dict]:
    """Parse WebVTT/SRT into segments, dropping YouTube's rolling repeats.

    Auto-generated VTT repeats the previous cue's text as context on every
    new cue. Emitting those verbatim triples the transcript, so a cue whose
    text is already the tail of what we have is skipped.
    """
    segments: list[dict] = []
    lines = payload.splitlines()
    index = 0
    while index < len(lines):
        match = _VTT_CUE.search(lines[index])
        if not match:
            index += 1
            continue
        start = _vtt_seconds(match.group("start"))
        end = _vtt_seconds(match.group("end"))
        index += 1
        cue_lines = []
        while index < len(lines) and lines[index].strip():
            cue_lines.append(_VTT_TAG.sub("", lines[index]).strip())
            index += 1
        text = " ".join(part for part in cue_lines if part).strip()
        if not text:
            continue
        if segments and (segments[-1]["text"] == text or text in segments[-1]["text"]):
            continue
        if segments and segments[-1]["text"].endswith(text):
            continue
        segments.append({"text": text, "start": start, "duration": max(end - start, 0.0)})
    return segments


def parse_caption_payload(payload: str, ext: str) -> list[dict]:
    """Parse a downloaded caption body into ``{text, start, duration}`` segments."""
    if ext == "json3":
        return _segments_from_json3(payload)
    if ext in {"vtt", "srt"}:
        return _segments_from_vtt(payload)
    if ext in {"srv1", "srv3", "ttml"}:
        # XML with <text start=".." dur=".."> (srv1) or <p begin=..> (ttml).
        segments = []
        for match in re.finditer(
            r"<text(?P<attrs>[^>]*)>(?P<body>.*?)</text>", payload, re.DOTALL
        ):
            attrs = match.group("attrs")
            start_attr = re.search(r'start="([\d.]+)"', attrs)
            if not start_attr:
                continue
            dur_attr = re.search(r'dur="([\d.]+)"', attrs)
            text = re.sub(r"<[^>]+>", "", match.group("body"))
            text = (
                text.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .strip()
            )
            if not text:
                continue
            segments.append(
                {
                    "text": text,
                    "start": float(start_attr.group(1)),
                    "duration": float(dur_attr.group(1)) if dur_attr else 0.0,
                }
            )
        if segments:
            return segments
    # Unknown container: try both known parsers before giving up.
    try:
        return _segments_from_json3(payload)
    except Exception:
        return _segments_from_vtt(payload)


def download_caption_track(track: dict, opts: Optional[dict] = None) -> list[dict]:
    """Fetch a caption track chosen by :func:`pick_caption_track`."""
    yt_dlp = ensure_yt_dlp()
    with yt_dlp.YoutubeDL(dict(opts or build_ydl_opts())) as ydl:
        try:
            payload = ydl.urlopen(track["url"]).read().decode("utf-8", "replace")
        except Exception as exc:
            raise classify_error(exc) from exc
    segments = parse_caption_payload(payload, track.get("ext", ""))
    if not segments:
        raise YouTubeError(
            "empty_captions",
            f"The {track.get('language', '?')} caption track downloaded but parsed to "
            "zero segments.",
            hint="Try another language, or fall back to audio + speech-to-text.",
        )
    return segments


def segments_to_text(segments: list[dict], timestamps: bool = False) -> str:
    """Render segments as plain text, optionally one timestamped line each."""
    if timestamps:
        return "\n".join(
            f"{format_timestamp(seg['start'])} {seg['text']}" for seg in segments
        )
    return " ".join(seg["text"] for seg in segments)
