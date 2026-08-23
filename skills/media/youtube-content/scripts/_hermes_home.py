"""Resolve HERMES_HOME for the standalone youtube-content scripts.

Skill scripts may run outside the Hermes process (system Python, nix env,
CI) where ``hermes_constants`` is not importable. This module provides the
same ``get_hermes_home()`` contract without requiring it on ``sys.path``,
mirroring ``skills/productivity/google-workspace/scripts/_hermes_home.py``.
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    from hermes_constants import display_hermes_home as display_hermes_home
    from hermes_constants import get_hermes_home as get_hermes_home
except (ModuleNotFoundError, ImportError):

    def get_hermes_home() -> Path:
        """Return the Hermes home directory (default: ~/.hermes)."""
        val = os.environ.get("HERMES_HOME", "").strip()
        return Path(val) if val else Path.home() / ".hermes"

    def display_hermes_home() -> str:
        """Return a user-friendly ``~/``-shortened display string."""
        home = get_hermes_home()
        try:
            return "~/" + str(home.relative_to(Path.home()))
        except ValueError:
            return str(home)


def media_dir(video_id: str = "") -> Path:
    """Working directory for downloaded YouTube media.

    ``HERMES_YOUTUBE_DIR`` overrides the default
    ``$HERMES_HOME/media/youtube``. Per-video subdirectories keep repeat
    runs on the same video from colliding.
    """
    override = os.environ.get("HERMES_YOUTUBE_DIR", "").strip()
    base = Path(override) if override else get_hermes_home() / "media" / "youtube"
    return base / video_id if video_id else base
