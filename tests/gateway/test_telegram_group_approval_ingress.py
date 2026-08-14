"""Regression tests for all-ingress enrollment gating and rehydration."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


CHAT = "-100222"


def _adapter(tmp_path, state, extra=None):
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state), encoding="utf-8")
    values = {"group_approval_enabled": True}
    values.update(extra or {})
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra=values))
    adapter._bot = AsyncMock()
    adapter._bot.id = 777
    return adapter


def _rejected_state():
    return {
        "pending": {}, "approved": {},
        "rejected": {CHAT: {"chat_id": CHAT}}, "leave_actions": {},
    }


@pytest.mark.asyncio
async def test_rejected_still_allowlisted_group_callback_cannot_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(
        tmp_path, _rejected_state(),
        {"allowed_chats": [CHAT], "group_allowed_chats": [CHAT]},
    )
    adapter._handle_model_picker_callback = AsyncMock()
    query = SimpleNamespace(
        data="mp:test",
        message=SimpleNamespace(
            chat_id=int(CHAT), chat=SimpleNamespace(id=int(CHAT), type="supergroup")
        ),
        from_user=SimpleNamespace(id=1, first_name="User"),
        answer=AsyncMock(),
    )

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

    adapter._handle_model_picker_callback.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="This group is not authorized")


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", ["message_reaction", "message_edited"])
async def test_rejected_group_reaction_and_edit_platform_events_do_not_dispatch(
    tmp_path, monkeypatch, event_type
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(
        tmp_path, _rejected_state(),
        {"allowed_chats": [CHAT], "group_allowed_chats": [CHAT]},
    )
    handler = AsyncMock()
    adapter._platform_event_handler = handler
    adapter._normalize_platform_event = lambda update: {
        "platform": "telegram", "event_type": event_type, "payload": {}
    }
    adapter._source_for_platform_event_auth = lambda update: SimpleNamespace(
        chat_id=CHAT, chat_type="supergroup"
    )
    monkeypatch.setattr("hermes_cli.lifecycle.has_hook", lambda name: True)

    await adapter._on_platform_update(SimpleNamespace(), None)

    handler.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["text", "photo", "media_group"])
async def test_delayed_batches_revalidate_and_drop_after_group_rejection(
    tmp_path, monkeypatch, kind
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter = _adapter(
        tmp_path, _rejected_state(),
        {"allowed_chats": [CHAT], "group_allowed_chats": [CHAT]},
    )
    adapter.handle_message = AsyncMock()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    event = SimpleNamespace(
        source=SimpleNamespace(chat_id=CHAT, chat_type="supergroup"),
        text="queued",
        media_urls=["photo.jpg"],
    )
    if kind == "text":
        adapter._pending_text_batches["batch"] = event
        await adapter._flush_text_batch("batch")
    elif kind == "photo":
        adapter._pending_photo_batches["batch"] = event
        await adapter._flush_photo_batch("batch")
    else:
        adapter._media_group_events["batch"] = event
        await adapter._flush_media_group_event("batch")

    adapter.handle_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_restart_rehydrates_approved_projection_before_authorizing(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    state = {
        "pending": {}, "rejected": {}, "leave_actions": {},
        "approved": {CHAT: {"chat_id": CHAT, "safety_prompt": "mandatory safety"}},
    }
    adapter = _adapter(tmp_path, state)

    assert adapter._is_effectively_approved_group(CHAT) is False
    assert await adapter._rehydrate_approved_groups() is True
    assert adapter._is_effectively_approved_group(CHAT) is True
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["approved"][CHAT]["synchronized"] is True
    assert CHAT in adapter._telegram_allowed_chats()
    assert CHAT in adapter._telegram_group_allowed_chats()
    assert adapter.config.extra["channel_prompts"][CHAT] == "mandatory safety"


@pytest.mark.asyncio
async def test_restart_migrates_legacy_approved_prompt_before_authorizing(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    state = {
        "pending": {}, "rejected": {}, "leave_actions": {},
        "approved": {
            CHAT: {
                "chat_id": CHAT,
                "prompt": "legacy mandatory safety",
            }
        },
    }
    adapter = _adapter(tmp_path, state)

    assert adapter._is_effectively_approved_group(CHAT) is False
    assert await adapter._rehydrate_approved_groups() is True
    assert adapter._is_effectively_approved_group(CHAT) is True
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["approved"][CHAT]["prompt"] == "legacy mandatory safety"
    assert saved["approved"][CHAT]["safety_prompt"] == "legacy mandatory safety"
    assert saved["approved"][CHAT]["synchronized"] is True
    assert adapter.config.extra["channel_prompts"][CHAT] == "legacy mandatory safety"


@pytest.mark.asyncio
async def test_restart_keeps_unsynchronized_approved_group_blocked(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    state = {
        "pending": {}, "rejected": {}, "leave_actions": {},
        "approved": {CHAT: {"chat_id": CHAT, "safety_prompt": ""}},
    }
    adapter = _adapter(
        tmp_path, state,
        {"allowed_chats": [CHAT], "group_allowed_chats": [CHAT],
         "channel_prompts": {CHAT: "stale"}},
    )

    assert await adapter._rehydrate_approved_groups() is False
    assert adapter._is_effectively_approved_group(CHAT) is False
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["approved"][CHAT]["synchronized"] is False
