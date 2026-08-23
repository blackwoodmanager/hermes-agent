"""web_extract_tool points at the youtube-content skill for YouTube links.

A YouTube watch page is a JavaScript shell, so every extract backend returns
near-empty boilerplate for it. Without a pointer the model reads that as
"this link cannot be opened" and tells the user Hermes cannot do YouTube.
"""

import json

import pytest

from agent import web_search_registry
from agent.web_search_provider import WebSearchProvider
from tools import web_tools


class TestYoutubeExtractHint:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtube.com/watch?v=dQw4w9WgXcQ&t=10",
            "https://m.youtube.com/watch?v=dQw4w9WgXcQ",
            "https://youtu.be/dQw4w9WgXcQ",
            "https://www.youtube.com/shorts/dQw4w9WgXcQ",
            "https://www.youtube.com/live/dQw4w9WgXcQ",
            "https://www.youtube.com/embed/dQw4w9WgXcQ",
            "  https://www.youtube.com/watch?v=dQw4w9WgXcQ  ",
        ],
    )
    def test_video_urls_get_the_hint(self, url):
        assert web_tools.youtube_extract_hint(url) == web_tools.YOUTUBE_EXTRACT_HINT

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/watch?v=dQw4w9WgXcQ",
            "https://www.youtube.com/@somechannel",
            "https://www.youtube.com/results?search_query=cats",
            "",
        ],
    )
    def test_non_video_urls_get_nothing(self, url):
        assert web_tools.youtube_extract_hint(url) is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://youtube.com.evil.example/watch?v=x",
            "https://notyoutube.com/watch?v=x",
            "https://youtu.be.evil.example/x",
        ],
    )
    def test_lookalike_domains_do_not_match(self, url):
        assert web_tools.youtube_extract_hint(url) is None

    def test_non_string_input(self):
        assert web_tools.youtube_extract_hint(None) is None

    def test_hint_names_the_helper_script(self):
        assert "youtube_media.py" in web_tools.YOUTUBE_EXTRACT_HINT


class _FakeExtractProvider(WebSearchProvider):
    @property
    def name(self) -> str:
        return "youtube-hint-test"

    @property
    def display_name(self) -> str:
        return "YouTube Hint Test"

    def is_available(self) -> bool:
        return True

    def supports_extract(self) -> bool:
        return True

    async def extract(self, urls, **kwargs):
        return [
            {"url": url, "title": "Some video", "content": "Enable JavaScript"}
            for url in urls
        ]


@pytest.fixture
def extract_provider(monkeypatch):
    with web_search_registry._lock:
        previous = dict(web_search_registry._providers)
        web_search_registry._providers.clear()

    provider = _FakeExtractProvider()
    web_search_registry.register_provider(provider)
    monkeypatch.setattr(web_tools, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(
        web_tools, "_load_web_config", lambda: {"extract_backend": provider.name}
    )

    async def _safe(_url):
        return True

    monkeypatch.setattr(web_tools, "async_is_safe_url", _safe)
    yield provider

    with web_search_registry._lock:
        web_search_registry._providers.clear()
        web_search_registry._providers.update(previous)


@pytest.mark.asyncio
async def test_extract_result_carries_the_hint(extract_provider):
    results = json.loads(
        await web_tools.web_extract_tool(["https://www.youtube.com/watch?v=dQw4w9WgXcQ"])
    )["results"]
    assert results[0]["hint"] == web_tools.YOUTUBE_EXTRACT_HINT
    # The extracted content is still returned — the hint adds, never replaces.
    assert results[0]["content"] == "Enable JavaScript"


@pytest.mark.asyncio
async def test_non_youtube_results_are_unchanged(extract_provider):
    results = json.loads(
        await web_tools.web_extract_tool(["https://example.com/article"])
    )["results"]
    assert "hint" not in results[0]


@pytest.mark.asyncio
async def test_mixed_batch_hints_only_the_youtube_entry(extract_provider):
    results = json.loads(
        await web_tools.web_extract_tool(
            ["https://example.com/a", "https://youtu.be/dQw4w9WgXcQ"]
        )
    )["results"]
    assert "hint" not in results[0]
    assert "hint" in results[1]
