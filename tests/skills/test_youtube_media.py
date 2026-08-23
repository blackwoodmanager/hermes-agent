"""Tests for the youtube-content skill's yt-dlp backend.

Nothing here touches the network: yt-dlp is replaced with a stub module and
caption payloads are inline fixtures.
"""

import json
import sys
import types
from pathlib import Path

import pytest

SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2] / "skills" / "media" / "youtube-content" / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import yt_common  # noqa: E402
import youtube_media  # noqa: E402


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL123&index=2",
            "  https://m.youtube.com/watch?v=dQw4w9WgXcQ  ",
            "dQw4w9WgXcQ",
        ],
    )
    def test_recognized_forms(self, url):
        assert yt_common.extract_video_id(url) == "dQw4w9WgXcQ"

    def test_unrecognized_input_passes_through(self):
        """Non-video URLs go to yt-dlp untouched rather than being mangled."""
        url = "https://www.youtube.com/@somechannel"
        assert yt_common.extract_video_id(url) == url


class TestCanonicalUrl:
    def test_bare_id_becomes_watch_url(self):
        assert (
            yt_common.canonical_url("dQw4w9WgXcQ")
            == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        )

    def test_url_is_left_alone(self):
        url = "https://youtu.be/dQw4w9WgXcQ?t=30"
        assert yt_common.canonical_url(url) == url


class TestFormatTimestamp:
    @pytest.mark.parametrize(
        "seconds,expected",
        [(0, "0:00"), (9, "0:09"), (90, "1:30"), (600, "10:00"), (3725, "1:02:05")],
    )
    def test_formats(self, seconds, expected):
        assert yt_common.format_timestamp(seconds) == expected


class TestBuildYdlOpts:
    def test_noplaylist_is_on_by_default(self):
        """A `watch?v=...&list=...` URL must mean the video, not the playlist."""
        assert yt_common.build_ydl_opts()["noplaylist"] is True

    def test_never_writes_to_stdout(self):
        """The scripts print JSON on stdout; yt-dlp logging must not join it."""
        assert yt_common.build_ydl_opts()["logtostderr"] is True

    def test_explicit_cookies_win_over_env(self, monkeypatch):
        monkeypatch.setenv("HERMES_YOUTUBE_COOKIES", "/env/cookies.txt")
        opts = yt_common.build_ydl_opts(cookies="/explicit/cookies.txt")
        assert opts["cookiefile"] == "/explicit/cookies.txt"

    def test_env_cookies_are_picked_up(self, monkeypatch):
        monkeypatch.setenv("HERMES_YOUTUBE_COOKIES", "/env/cookies.txt")
        assert yt_common.build_ydl_opts()["cookiefile"] == "/env/cookies.txt"

    def test_cookie_file_suppresses_browser_cookies(self, monkeypatch):
        """yt-dlp rejects both at once, and the explicit file is the specific one."""
        monkeypatch.delenv("HERMES_YOUTUBE_COOKIES", raising=False)
        opts = yt_common.build_ydl_opts(
            cookies="/c.txt", cookies_from_browser="chrome"
        )
        assert opts["cookiefile"] == "/c.txt"
        assert "cookiesfrombrowser" not in opts

    def test_browser_cookies_shape(self, monkeypatch):
        monkeypatch.delenv("HERMES_YOUTUBE_COOKIES", raising=False)
        opts = yt_common.build_ydl_opts(cookies_from_browser="firefox")
        assert opts["cookiesfrombrowser"] == ("firefox", None, None, None)

    def test_env_proxy(self, monkeypatch):
        monkeypatch.setenv("HERMES_YOUTUBE_PROXY", "socks5://127.0.0.1:9050")
        assert yt_common.build_ydl_opts()["proxy"] == "socks5://127.0.0.1:9050"

    def test_player_clients(self):
        opts = yt_common.build_ydl_opts(player_clients=["tv", "mweb"])
        assert opts["extractor_args"] == {"youtube": {"player_client": ["tv", "mweb"]}}


class TestClassifyError:
    @pytest.mark.parametrize(
        "message,code",
        [
            ("ERROR: [youtube] abc: Sign in to confirm you're not a bot", "bot_check"),
            ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", "private"),
            ("This video is available to this channel's members on level: Join this channel", "members_only"),
            ("Video unavailable. This video has been removed by the uploader", "unavailable"),
            ("The uploader has not made this video available in your country", "geo_blocked"),
            ("Sign in to confirm your age. This video may be inappropriate for some users", "bot_check"),
            ("This live event will begin in 2 hours", "live"),
            ("Unable to connect to proxy: Tunnel connection failed: 403 Forbidden", "network_blocked"),
            ("Unsupported URL: https://example.com/", "unsupported"),
            ("something nobody has ever seen before", "extraction_failed"),
        ],
    )
    def test_codes(self, message, code):
        assert yt_common.classify_error(RuntimeError(message)).code == code

    def test_strips_yt_dlp_prefix(self):
        err = yt_common.classify_error(RuntimeError("ERROR: [youtube] abc: boom"))
        assert not err.message.startswith("ERROR:")
        assert "boom" in err.message

    def test_youtube_error_passes_through_unchanged(self):
        original = yt_common.YouTubeError("no_captions", "none", "hint")
        assert yt_common.classify_error(original) is original

    def test_network_failures_are_not_retried_with_other_clients(self):
        """A blocked egress is not a player-client problem — don't hammer it."""
        blocked = RuntimeError("Tunnel connection failed: 403 Forbidden")
        assert not yt_common.is_retryable_with_other_clients(blocked)

    def test_bot_check_is_retried_with_other_clients(self):
        bot = RuntimeError("Sign in to confirm you're not a bot")
        assert yt_common.is_retryable_with_other_clients(bot)

    def test_to_dict_omits_empty_hint(self):
        assert yt_common.YouTubeError("x", "y").to_dict() == {"error": "y", "code": "x"}


class TestPickCaptionTrack:
    INFO = {
        "subtitles": {
            "en": [{"ext": "vtt", "url": "en-manual.vtt"},
                   {"ext": "json3", "url": "en-manual.json3"}],
        },
        "automatic_captions": {
            "en": [{"ext": "json3", "url": "en-auto.json3"}],
            "ru": [{"ext": "json3", "url": "ru-auto.json3"}],
        },
    }

    def test_prefers_json3_over_vtt(self):
        track = yt_common.pick_caption_track(self.INFO, ["en"])
        assert track["url"] == "en-manual.json3"
        assert track["ext"] == "json3"

    def test_manual_beats_automatic_for_the_same_language(self):
        assert yt_common.pick_caption_track(self.INFO, ["en"])["kind"] == "manual"

    def test_requested_language_wins_over_manual_in_another_language(self):
        track = yt_common.pick_caption_track(self.INFO, ["ru"])
        assert track["language"] == "ru"
        assert track["kind"] == "automatic"

    def test_language_fallback_chain_order(self):
        track = yt_common.pick_caption_track(self.INFO, ["de", "ru", "en"])
        assert track["language"] == "ru"

    def test_regional_variant_matches_base_language(self):
        info = {"subtitles": {"en-US": [{"ext": "json3", "url": "u"}]},
                "automatic_captions": {}}
        assert yt_common.pick_caption_track(info, ["en"])["language"] == "en-US"

    def test_falls_back_to_any_track_when_language_missing(self):
        track = yt_common.pick_caption_track(self.INFO, ["ja"])
        assert track is not None

    def test_returns_none_when_there_are_no_captions(self):
        assert yt_common.pick_caption_track({"subtitles": {}, "automatic_captions": {}}) is None

    def test_skips_tracks_without_a_url(self):
        info = {"subtitles": {"en": [{"ext": "json3"}]}, "automatic_captions": {}}
        assert yt_common.pick_caption_track(info, ["en"]) is None


class TestParseCaptionPayload:
    def test_json3(self):
        payload = json.dumps(
            {
                "events": [
                    {"tStartMs": 0, "dDurationMs": 1500, "segs": [{"utf8": "hello "},
                                                                  {"utf8": "world"}]},
                    {"tStartMs": 1500, "dDurationMs": 1000, "segs": [{"utf8": "\n"}]},
                    {"tStartMs": 2500, "dDurationMs": 900, "segs": [{"utf8": "again"}]},
                ]
            }
        )
        segments = yt_common.parse_caption_payload(payload, "json3")
        assert [s["text"] for s in segments] == ["hello world", "again"]
        assert segments[0]["duration"] == 1.5
        assert segments[1]["start"] == 2.5

    def test_vtt(self):
        payload = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "first line\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "second <c>line</c>\n\n"
        )
        segments = yt_common.parse_caption_payload(payload, "vtt")
        assert [s["text"] for s in segments] == ["first line", "second line"]
        assert segments[1]["start"] == 2.0

    def test_vtt_drops_youtube_rolling_repeats(self):
        """Auto-captions repeat the previous cue as context on every new cue."""
        payload = (
            "WEBVTT\n\n"
            "00:00:00.000 --> 00:00:02.000\n"
            "the quick brown fox\n\n"
            "00:00:02.000 --> 00:00:04.000\n"
            "the quick brown fox\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "jumps over\n\n"
        )
        segments = yt_common.parse_caption_payload(payload, "vtt")
        assert [s["text"] for s in segments] == ["the quick brown fox", "jumps over"]

    def test_srv1_xml(self):
        payload = (
            '<transcript><text start="0.5" dur="2.0">hi &amp; bye</text>'
            '<text start="3.0" dur="1.0">done</text></transcript>'
        )
        segments = yt_common.parse_caption_payload(payload, "srv1")
        assert segments[0] == {"text": "hi & bye", "start": 0.5, "duration": 2.0}
        assert segments[1]["start"] == 3.0


class TestSegmentsToText:
    SEGMENTS = [
        {"text": "one", "start": 0.0, "duration": 1.0},
        {"text": "two", "start": 65.0, "duration": 1.0},
    ]

    def test_plain(self):
        assert yt_common.segments_to_text(self.SEGMENTS) == "one two"

    def test_timestamped(self):
        assert yt_common.segments_to_text(self.SEGMENTS, timestamps=True) == (
            "0:00 one\n1:05 two"
        )


class TestSummarizeMetadata:
    INFO = {
        "id": "abc12345678",
        "title": "A talk",
        "uploader": "Someone",
        "upload_date": "20260114",
        "duration": 3725,
        "view_count": 100,
        "description": "hello",
        "chapters": [{"title": "Intro", "start_time": 0, "end_time": 90}],
        "subtitles": {"en": []},
        "automatic_captions": {"ru": []},
    }

    def test_upload_date_is_iso(self):
        assert yt_common.summarize_metadata(self.INFO)["upload_date"] == "2026-01-14"

    def test_duration_is_human_readable(self):
        summary = yt_common.summarize_metadata(self.INFO)
        assert summary["duration"] == "1:02:05"
        assert summary["duration_seconds"] == 3725

    def test_chapters_carry_a_timestamp(self):
        chapter = yt_common.summarize_metadata(self.INFO)["chapters"][0]
        assert chapter["title"] == "Intro"
        assert chapter["start_ts"] == "0:00"

    def test_caption_languages_are_reported(self):
        captions = yt_common.summarize_metadata(self.INFO)["captions"]
        assert captions == {"manual": ["en"], "automatic": ["ru"]}

    def test_missing_fields_do_not_raise(self):
        assert yt_common.summarize_metadata({})["title"] == ""


class _StubYoutubeDL:
    """Minimal stand-in for yt_dlp.YoutubeDL used by extract_info."""

    calls: list = []
    behaviours: list = []

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def extract_info(self, url, download=False):
        type(self).calls.append(self.opts)
        behaviour = type(self).behaviours[len(type(self).calls) - 1]
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour


@pytest.fixture
def stub_yt_dlp(monkeypatch):
    module = types.ModuleType("yt_dlp")
    module.YoutubeDL = _StubYoutubeDL
    module.version = types.SimpleNamespace(__version__="2026.7.4")
    monkeypatch.setitem(sys.modules, "yt_dlp", module)
    _StubYoutubeDL.calls = []
    _StubYoutubeDL.behaviours = []
    return _StubYoutubeDL


class TestExtractInfo:
    def test_returns_info_on_first_attempt(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [{"id": "abc", "title": "t"}]
        assert yt_common.extract_info("dQw4w9WgXcQ")["title"] == "t"
        assert len(stub_yt_dlp.calls) == 1

    def test_bot_check_retries_with_alternate_player_clients(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [
            RuntimeError("Sign in to confirm you're not a bot"),
            {"id": "abc", "title": "t"},
        ]
        assert yt_common.extract_info("dQw4w9WgXcQ")["title"] == "t"
        assert len(stub_yt_dlp.calls) == 2
        assert stub_yt_dlp.calls[1]["extractor_args"]["youtube"]["player_client"]

    def test_network_failure_is_not_retried(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [RuntimeError("Tunnel connection failed: 403 Forbidden")]
        with pytest.raises(yt_common.YouTubeError) as excinfo:
            yt_common.extract_info("dQw4w9WgXcQ")
        assert excinfo.value.code == "network_blocked"
        assert len(stub_yt_dlp.calls) == 1

    def test_persistent_bot_check_surfaces_the_hint(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [
            RuntimeError("Sign in to confirm you're not a bot"),
            RuntimeError("Sign in to confirm you're not a bot"),
        ]
        with pytest.raises(yt_common.YouTubeError) as excinfo:
            yt_common.extract_info("dQw4w9WgXcQ")
        assert excinfo.value.code == "bot_check"
        assert "cookies" in excinfo.value.hint.lower()

    def test_playlist_result_collapses_to_the_first_video(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [
            {"_type": "playlist", "entries": [None, {"id": "abc", "title": "first"}]}
        ]
        assert yt_common.extract_info("https://youtube.com/playlist?list=x")["title"] == "first"

    def test_none_result_is_an_error(self, stub_yt_dlp):
        stub_yt_dlp.behaviours = [None]
        with pytest.raises(yt_common.YouTubeError):
            yt_common.extract_info("dQw4w9WgXcQ")


class TestEnsureYtDlp:
    def test_missing_dependency_reports_the_install_command(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "yt_dlp", None)
        monkeypatch.delitem(sys.modules, "yt_dlp")
        monkeypatch.setattr(
            yt_common, "_install_yt_dlp", lambda: False
        )
        real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

        def fake_import(name, *args, **kwargs):
            if name == "yt_dlp":
                raise ImportError("no yt_dlp")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        with pytest.raises(yt_common.YouTubeError) as excinfo:
            yt_common.ensure_yt_dlp()
        assert excinfo.value.code == "dependency_missing"
        assert "yt-dlp" in excinfo.value.hint


class TestDownloadCaptionTrack:
    def test_empty_parse_is_reported_as_empty_captions(self, stub_yt_dlp, monkeypatch):
        class _Response:
            @staticmethod
            def read():
                return b'{"events": []}'

        monkeypatch.setattr(stub_yt_dlp, "urlopen", lambda self, url: _Response(), raising=False)
        with pytest.raises(yt_common.YouTubeError) as excinfo:
            yt_common.download_caption_track(
                {"url": "u", "ext": "json3", "language": "en"}
            )
        assert excinfo.value.code == "empty_captions"


class TestCli:
    def test_check_reports_state_as_json(self, capsys):
        exit_code = youtube_media.main(["check"])
        payload = json.loads(capsys.readouterr().out)
        assert set(payload) >= {"ready", "yt_dlp_version", "ffmpeg", "media_dir", "notes"}
        assert exit_code == (0 if payload["ready"] else 1)

    def test_failures_are_structured_json_on_stdout(self, capsys, monkeypatch):
        def boom(*args, **kwargs):
            raise yt_common.YouTubeError("bot_check", "blocked", "use cookies")

        monkeypatch.setattr(youtube_media, "extract_info", boom)
        exit_code = youtube_media.main(["info", "dQw4w9WgXcQ"])
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 1
        assert payload == {"error": "blocked", "code": "bot_check", "hint": "use cookies"}

    def test_unexpected_exceptions_are_classified_not_raised(self, capsys, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("Video unavailable")

        monkeypatch.setattr(youtube_media, "extract_info", boom)
        assert youtube_media.main(["info", "dQw4w9WgXcQ"]) == 1
        assert json.loads(capsys.readouterr().out)["code"] == "unavailable"

    def test_frames_needs_a_source(self, capsys):
        assert youtube_media.main(["frames"]) == 1
        assert json.loads(capsys.readouterr().out)["code"] == "bad_arguments"

    def test_brief_reports_missing_captions_without_failing(self, capsys, monkeypatch):
        monkeypatch.setattr(
            youtube_media,
            "extract_info",
            lambda *a, **k: {"id": "abc", "title": "t", "duration": 60,
                             "subtitles": {}, "automatic_captions": {}},
        )
        exit_code = youtube_media.main(["brief", "dQw4w9WgXcQ"])
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["title"] == "t"
        assert payload["transcript"]["available"] is False
        assert payload["transcript"]["code"] == "no_captions"
        assert "audio" in payload["transcript"]["next_step"]

    def test_transcript_text_only_prints_bare_text(self, capsys, monkeypatch):
        monkeypatch.setattr(
            youtube_media,
            "extract_info",
            lambda *a, **k: {"id": "abc", "title": "t",
                             "subtitles": {"en": [{"ext": "json3", "url": "u"}]},
                             "automatic_captions": {}},
        )
        monkeypatch.setattr(
            youtube_media,
            "download_caption_track",
            lambda *a, **k: [{"text": "hello", "start": 0.0, "duration": 1.0}],
        )
        assert youtube_media.main(["transcript", "dQw4w9WgXcQ", "--text-only"]) == 0
        assert capsys.readouterr().out.strip() == "hello"

    def test_transcript_missing_captions_exits_nonzero(self, capsys, monkeypatch):
        monkeypatch.setattr(
            youtube_media,
            "extract_info",
            lambda *a, **k: {"id": "abc", "subtitles": {}, "automatic_captions": {}},
        )
        assert youtube_media.main(["transcript", "dQw4w9WgXcQ"]) == 1
        assert json.loads(capsys.readouterr().out)["code"] == "no_captions"

    def test_long_transcripts_are_truncated_and_saved(self, capsys, monkeypatch, tmp_path):
        monkeypatch.setattr(
            youtube_media,
            "extract_info",
            lambda *a, **k: {"id": "abc", "title": "t",
                             "subtitles": {"en": [{"ext": "json3", "url": "u"}]},
                             "automatic_captions": {}},
        )
        monkeypatch.setattr(
            youtube_media,
            "download_caption_track",
            lambda *a, **k: [{"text": "x" * 500, "start": 0.0, "duration": 1.0}],
        )
        exit_code = youtube_media.main(
            ["transcript", "dQw4w9WgXcQ", "--max-chars", "100", "--out", str(tmp_path)]
        )
        payload = json.loads(capsys.readouterr().out)
        assert exit_code == 0
        assert payload["truncated"] is True
        assert Path(payload["transcript_file"]).read_text(encoding="utf-8")
        assert len(payload["text"]) < 500


class TestLazyDepsRegistration:
    def test_yt_dlp_feature_is_declared(self):
        from tools.lazy_deps import LAZY_DEPS

        assert "media.yt_dlp" in LAZY_DEPS
        assert any("yt-dlp" in spec for spec in LAZY_DEPS["media.yt_dlp"])

    def test_yt_dlp_is_a_floor_not_an_exact_pin(self):
        """An exact pin would let `hermes update` downgrade a working yt-dlp
        over a stale one every time YouTube breaks extraction."""
        from tools.lazy_deps import LAZY_DEPS

        assert all("==" not in spec for spec in LAZY_DEPS["media.yt_dlp"])

    def test_skill_spec_matches_lazy_deps(self):
        from tools.lazy_deps import LAZY_DEPS

        assert yt_common.YT_DLP_SPEC in LAZY_DEPS["media.yt_dlp"]
