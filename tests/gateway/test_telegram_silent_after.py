"""Regression tests for Telegram's configured silent-after cutoff."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(*, mode="important", extra=None):
    config = PlatformConfig(
        enabled=True,
        token="fake-token",
        extra={
            "silent_after": "21:00",
            "silent_after_timezone": "Europe/Kyiv",
            **(extra or {}),
        },
    )
    adapter = TelegramAdapter(config)
    adapter._notifications_mode = mode
    bot = MagicMock()
    bot.id = 777
    bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=42))
    bot.get_chat = AsyncMock()
    bot.get_chat_member = AsyncMock()
    adapter._bot = bot
    return adapter


def test_silent_after_uses_configured_timezone_boundary():
    adapter = _adapter()

    assert adapter._is_silent_after_now(
        datetime(2026, 8, 14, 17, 59, tzinfo=timezone.utc)
    ) is False  # 20:59 Europe/Kyiv
    assert adapter._is_silent_after_now(
        datetime(2026, 8, 14, 18, 0, tzinfo=timezone.utc)
    ) is True  # 21:00 Europe/Kyiv
    assert adapter._is_silent_after_now(
        datetime(2026, 8, 14, 20, 59, tzinfo=timezone.utc)
    ) is True  # 23:59 Europe/Kyiv
    assert adapter._is_silent_after_now(
        datetime(2026, 8, 14, 21, 1, tzinfo=timezone.utc)
    ) is False  # 00:01 next day: the daily cutoff resets


def test_silent_after_overrides_notify_and_all_modes(monkeypatch):
    adapter = _adapter(mode="all")
    monkeypatch.setattr(adapter, "_is_silent_after_now", lambda: True)

    assert adapter._notification_kwargs({"notify": True}) == {
        "disable_notification": True
    }


def test_before_cutoff_preserves_existing_notification_policy(monkeypatch):
    adapter = _adapter(mode="important")
    monkeypatch.setattr(adapter, "_is_silent_after_now", lambda: False)

    assert adapter._notification_kwargs({"notify": True}) == {}
    assert adapter._notification_kwargs(None) == {"disable_notification": True}


def test_invalid_or_missing_silent_after_fails_open_to_existing_policy():
    missing = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    invalid = _adapter(extra={"silent_after": "25:99"})
    now = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)

    assert missing._is_silent_after_now(now) is False
    assert invalid._is_silent_after_now(now) is False


@pytest.mark.asyncio
async def test_owner_approval_card_is_silent_after_cutoff(monkeypatch):
    adapter = _adapter(extra={"group_approval_owner_id": 171389200})
    monkeypatch.setattr(adapter, "_is_silent_after_now", lambda: True)
    request = {"title": "Test group", "chat_id": "-100123"}

    delivered = await adapter._deliver_group_approval_request("nonce", request)

    assert delivered is True
    assert adapter._bot.send_message.await_args.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_control_send_forces_silent_after_cutoff(monkeypatch):
    adapter = _adapter(mode="all")
    monkeypatch.setattr(adapter, "_is_silent_after_now", lambda: True)

    await adapter._send_message_with_thread_fallback(
        chat_id=171389200,
        text="Control message",
    )

    assert adapter._bot.send_message.await_args.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_standalone_sender_uses_same_quiet_policy(monkeypatch):
    from plugins.platforms.telegram import adapter as telegram_adapter
    from tools import send_message_tool

    pconfig = PlatformConfig(
        enabled=True,
        token="fake-token",
        extra={
            "silent_after": "21:00",
            "silent_after_timezone": "Europe/Kyiv",
        },
    )
    monkeypatch.setattr(
        telegram_adapter.TelegramAdapter,
        "_is_silent_after_now",
        lambda self: True,
    )
    standalone = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(send_message_tool, "_send_telegram", standalone)

    await telegram_adapter._standalone_send(pconfig, "123", "hello")

    assert standalone.await_args.kwargs["disable_notification"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("media_name", "bot_method"),
    [(None, "send_message"), ("image.jpg", "send_photo")],
)
async def test_standalone_rest_payload_is_silent_for_text_and_media(
    monkeypatch, tmp_path, media_name, bot_method
):
    import telegram
    from tools.send_message_tool import _send_telegram

    bot = MagicMock()
    for method in (
        "send_message", "send_photo", "send_video", "send_voice",
        "send_audio", "send_document",
    ):
        setattr(bot, method, AsyncMock(return_value=SimpleNamespace(message_id=42)))
    monkeypatch.setattr(telegram, "Bot", MagicMock(return_value=bot))
    monkeypatch.setattr(
        "gateway.platforms.base.resolve_proxy_url", lambda *args, **kwargs: None
    )

    media_files = None
    message = "hello"
    if media_name:
        path = tmp_path / media_name
        path.write_bytes(b"not-a-real-image")
        media_files = [(str(path), False)]
        message = ""

    result = await _send_telegram(
        "fake-token",
        "123",
        message,
        media_files=media_files,
        disable_notification=True,
    )

    assert result["success"] is True
    call = getattr(bot, bot_method).await_args
    assert call.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_send_message_tool_dispatch_propagates_quiet_policy(monkeypatch):
    from gateway.config import Platform
    from tools import send_message_tool

    pconfig = PlatformConfig(
        enabled=True,
        token="fake-token",
        extra={
            "silent_after": "21:00",
            "silent_after_timezone": "Europe/Kyiv",
        },
    )
    monkeypatch.setattr(
        TelegramAdapter,
        "_is_silent_after_now",
        lambda self: True,
    )
    standalone = AsyncMock(return_value={"success": True})
    monkeypatch.setattr(send_message_tool, "_send_telegram", standalone)

    await send_message_tool._send_to_platform(
        Platform.TELEGRAM,
        pconfig,
        "123",
        "hello",
    )

    assert standalone.await_args.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_media_cache_failure_reply_is_silent_after_cutoff(monkeypatch):
    adapter = _adapter(mode="all")
    monkeypatch.setattr(adapter, "_is_silent_after_now", lambda: True)
    message = SimpleNamespace(reply_text=AsyncMock())
    event = SimpleNamespace(text="")

    await adapter._surface_media_cache_failure(
        message,
        event,
        "document",
        RuntimeError("download failed"),
    )

    assert message.reply_text.await_args.kwargs["disable_notification"] is True


@pytest.mark.asyncio
async def test_standalone_text_retry_rechecks_quiet_policy(monkeypatch):
    import telegram
    from tools import send_message_tool

    bot = MagicMock()
    bot.send_message = AsyncMock(
        side_effect=[RuntimeError("502 Bad Gateway"), SimpleNamespace(message_id=42)]
    )
    monkeypatch.setattr(telegram, "Bot", MagicMock(return_value=bot))
    monkeypatch.setattr(
        "gateway.platforms.base.resolve_proxy_url", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(send_message_tool.asyncio, "sleep", AsyncMock())
    states = iter([{}, {"disable_notification": True}])

    result = await send_message_tool._send_telegram(
        "fake-token",
        "123",
        "hello",
        notification_kwargs_factory=lambda: next(states),
    )

    assert result["success"] is True
    assert [
        call.kwargs.get("disable_notification")
        for call in bot.send_message.await_args_list
    ] == [None, True]


@pytest.mark.asyncio
async def test_standalone_media_thread_retry_rechecks_quiet_policy(
    monkeypatch, tmp_path
):
    import telegram
    from tools import send_message_tool

    bot = MagicMock()
    bot.send_photo = AsyncMock(
        side_effect=[
            RuntimeError("Message thread not found"),
            SimpleNamespace(message_id=42),
        ]
    )
    monkeypatch.setattr(telegram, "Bot", MagicMock(return_value=bot))
    monkeypatch.setattr(
        "gateway.platforms.base.resolve_proxy_url", lambda *args, **kwargs: None
    )
    path = tmp_path / "image.jpg"
    path.write_bytes(b"not-a-real-image")
    states = iter([{}, {"disable_notification": True}])

    result = await send_message_tool._send_telegram(
        "fake-token",
        "123",
        "",
        media_files=[(str(path), False)],
        thread_id="99",
        notification_kwargs_factory=lambda: next(states),
    )

    assert result["success"] is True
    assert [
        call.kwargs.get("disable_notification")
        for call in bot.send_photo.await_args_list
    ] == [None, True]


@pytest.mark.asyncio
async def test_live_media_retry_rechecks_quiet_policy(monkeypatch):
    adapter = _adapter(mode="all")
    send_fn = AsyncMock(
        side_effect=[RuntimeError("stale reply"), SimpleNamespace(message_id=42)]
    )
    monkeypatch.setattr(
        adapter,
        "_should_retry_without_dm_topic_reply_anchor",
        lambda *args, **kwargs: True,
    )
    states = iter([{}, {"disable_notification": True}])
    monkeypatch.setattr(
        adapter,
        "_notification_kwargs",
        lambda metadata: next(states),
    )

    await adapter._send_with_dm_topic_reply_anchor_retry(
        send_fn,
        {"chat_id": 123, "reply_to_message_id": 7},
        {"notify": True},
        7,
        "photo",
    )

    assert [
        call.kwargs.get("disable_notification")
        for call in send_fn.await_args_list
    ] == [None, True]


@pytest.mark.asyncio
async def test_live_control_retry_rechecks_quiet_policy(monkeypatch):
    adapter = _adapter(mode="all")
    adapter._bot.send_message = AsyncMock(
        side_effect=[RuntimeError("thread missing"), SimpleNamespace(message_id=42)]
    )
    monkeypatch.setattr(adapter, "_is_bad_request_error", lambda error: True)
    monkeypatch.setattr(adapter, "_is_thread_not_found_error", lambda error: True)
    states = iter([{}, {"disable_notification": True}])
    monkeypatch.setattr(
        adapter,
        "_notification_kwargs",
        lambda metadata: next(states),
    )

    await adapter._send_message_with_thread_fallback(
        chat_id=123,
        text="control",
        message_thread_id=99,
    )

    assert [
        call.kwargs.get("disable_notification")
        for call in adapter._bot.send_message.await_args_list
    ] == [None, True]
