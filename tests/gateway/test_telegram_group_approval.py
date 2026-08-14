"""Telegram owner approval flow for newly joined groups."""

import asyncio
import json
import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import yaml

from gateway.config import Platform, PlatformConfig, load_gateway_config
from plugins.platforms.telegram.adapter import TelegramAdapter


@pytest.fixture(autouse=True)
def _clear_telegram_allowlist_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ALLOWED_CHATS", raising=False)


def _make_adapter(extra=None):
    adapter = TelegramAdapter(
        PlatformConfig(enabled=True, token="test-token", extra=extra or {})
    )
    adapter._bot = AsyncMock()
    adapter._bot.id = 777
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=9001)
    adapter._bot.get_chat_member.return_value = SimpleNamespace(
        status="administrator"
    )
    return adapter


def _write_pending(
    hermes_home, *, nonce="nonce123", message_id=9001, created_at=None
):
    state = {
        "pending": {
            nonce: {
                "chat_id": "-100222",
                "title": "New Team",
                "owner_id": 171389200,
                "owner_chat_id": 171389200,
                "owner_message_id": message_id,
                "created_at": created_at or datetime.now(timezone.utc).isoformat(),
            }
        },
        "rejected": {},
    }
    (hermes_home / "telegram_group_approvals.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    return nonce


def _group_message(chat_id=-100222):
    return SimpleNamespace(chat=SimpleNamespace(id=chat_id, type="supergroup"))


@pytest.mark.parametrize(
    ("state", "legacy_allowlisted"),
    [
        ({"pending": {}, "rejected": {}, "approved": {}}, False),
        (
            {
                "pending": {"nonce": {"chat_id": "-100222"}},
                "rejected": {},
                "approved": {},
            },
            True,
        ),
        (
            {
                "pending": {},
                "rejected": {"-100222": {"title": "Rejected"}},
                "approved": {},
            },
            True,
        ),
    ],
)
def test_unknown_pending_and_rejected_groups_fail_closed(
    tmp_path, monkeypatch, state, legacy_allowlisted
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telegram_group_approvals.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    allowlist = ["-100222"] if legacy_allowlisted else []
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "allowed_chats": allowlist,
            "group_allowed_chats": allowlist,
        }
    )

    assert adapter._group_approval_allows_message(_group_message()) is False


def test_approved_group_passes_hard_gate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telegram_group_approvals.json").write_text(
        json.dumps(
            {
                "pending": {},
                "rejected": {},
                "approved": {"-100222": {
                    "title": "Approved",
                    "safety_prompt": "mandatory safety",
                    "synchronized": True,
                }},
            }
        ),
        encoding="utf-8",
    )
    adapter = _make_adapter({
        "group_approval_enabled": True,
        "allowed_chats": ["-100222"],
        "group_allowed_chats": ["-100222"],
        "channel_prompts": {"-100222": "mandatory safety"},
    })
    adapter._group_approval_synchronized.add("-100222")

    assert adapter._group_approval_allows_message(_group_message()) is True


@pytest.mark.parametrize(
    "raw_state",
    ["{not-json", json.dumps({"pending": [], "rejected": {}, "approved": {}})],
)
def test_unreadable_group_approval_state_fails_closed(
    tmp_path, monkeypatch, raw_state
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "telegram_group_approvals.json").write_text(
        raw_state, encoding="utf-8"
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "allowed_chats": ["-100222"],
            "group_allowed_chats": ["-100222"],
        }
    )

    assert adapter._group_approval_allows_message(_group_message()) is False


def test_group_membership_handler_is_registered(monkeypatch):
    factory = Mock(side_effect=lambda callback, kind: (callback, kind))
    factory.MY_CHAT_MEMBER = -1
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.ChatMemberHandler", factory
    )
    app = SimpleNamespace(add_handler=Mock())
    adapter = _make_adapter()

    adapter._register_group_approval_handler(app)

    callback, kind = app.add_handler.call_args.args[0]
    assert callback == adapter._handle_my_chat_member
    assert kind == -1


@pytest.mark.asyncio
async def test_new_group_membership_requests_owner_approval(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    callback_data = []
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(
            text=text, callback_data=callback_data
        ),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
            "allowed_chats": ["-100111"],
        }
    )
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(
                id=-100222,
                type="supergroup",
                title="New Team",
            ),
            old_chat_member=SimpleNamespace(status="left"),
            new_chat_member=SimpleNamespace(status="administrator"),
            from_user=SimpleNamespace(id=555, full_name="Inviter"),
        )
    )

    await adapter._handle_my_chat_member(update, SimpleNamespace())

    adapter._bot.send_message.assert_awaited_once()
    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == 171389200
    assert "New Team" in kwargs["text"]
    assert "-100222" in kwargs["text"]
    assert "доступ до повідомлень" in kwargs["text"]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert [button.text for button in rows[0]] == ["✅", "❌"]
    callback_data.extend(
        button.callback_data
        for row in rows
        for button in row
    )
    assert len(callback_data) == 2
    assert callback_data[0].startswith("ga:a:")
    assert callback_data[1].startswith("ga:d:")
    nonce = callback_data[0].split(":", 2)[2]
    assert nonce == callback_data[1].split(":", 2)[2]
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text(
            encoding="utf-8"
        )
    )
    assert state["pending"][nonce]["owner_message_id"] == 9001
    assert state["pending"][nonce]["owner_id"] == 171389200
    assert state["pending"][nonce]["owner_chat_id"] == 171389200


@pytest.mark.asyncio
async def test_owner_approval_persists_and_activates_group(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "allowed_chats": "-100111",
                    "group_allowed_chats": "-100111",
                    "channel_prompts": {"-100111": "existing prompt"},
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
            "allowed_chats": ["-100111"],
            "group_allowed_chats": ["-100111"],
            "channel_prompts": {"-100111": "existing prompt"},
        }
    )
    nonce = _write_pending(hermes_home)
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200, first_name="Andrew"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))["telegram"]
    assert saved["allowed_chats"] == "-100111,-100222"
    assert saved["group_allowed_chats"] == "-100111,-100222"
    assert saved["channel_prompts"]["-100222"] == "shared-group safety prompt"
    assert adapter._telegram_allowed_chats() == {"-100111", "-100222"}
    assert adapter._telegram_group_allowed_chats() == {"-100111", "-100222"}
    assert os.environ["TELEGRAM_ALLOWED_CHATS"] == "-100111,-100222"
    query.answer.assert_awaited_once_with(text="Group approved")


@pytest.mark.parametrize(
    ("initial", "settings_path"),
    [
        (
            {"telegram": {}},
            ("telegram",),
        ),
        (
            {"platforms": {"telegram": {}}},
            ("platforms", "telegram"),
        ),
        (
            {"platforms": {"telegram": {"extra": {}}}},
            ("platforms", "telegram", "extra"),
        ),
        (
            {"gateway": {"platforms": {"telegram": {}}}},
            ("gateway", "platforms", "telegram"),
        ),
    ],
)
def test_group_approval_updates_active_config_schema(
    tmp_path, monkeypatch, initial, settings_path
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    settings = initial
    for segment in settings_path:
        if segment == "extra" and segment not in settings:
            break
        settings = settings[segment]
    if isinstance(settings, dict):
        settings.update(
            {
                "allowed_chats": ["-100111"],
                "group_allowed_chats": "-100111",
                "channel_prompts": {"-100111": "existing prompt"},
            }
        )
    config_path = hermes_home / "config.yaml"
    config_path.write_text(yaml.safe_dump(initial), encoding="utf-8")
    adapter = _make_adapter()

    adapter._approve_telegram_group("-100222", "new prompt")

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    target = saved
    for segment in settings_path:
        target = target[segment]
    assert set(str(target["allowed_chats"]).split(",")) == {
        "-100111",
        "-100222",
    }
    assert set(str(target["group_allowed_chats"]).split(",")) == {
        "-100111",
        "-100222",
    }
    assert target["channel_prompts"] == {
        "-100111": "existing prompt",
        "-100222": "new prompt",
    }
    if settings_path[0] != "telegram":
        assert "telegram" not in saved
    runtime = load_gateway_config().platforms[Platform.TELEGRAM].extra
    runtime_allowed = runtime["allowed_chats"]
    if isinstance(runtime_allowed, str):
        runtime_allowed = runtime_allowed.split(",")
    runtime_group_allowed = runtime["group_allowed_chats"]
    if isinstance(runtime_group_allowed, str):
        runtime_group_allowed = runtime_group_allowed.split(",")
    assert set(runtime_allowed) == {"-100111", "-100222"}
    assert set(runtime_group_allowed) == {"-100111", "-100222"}


def test_group_approval_ignores_empty_legacy_block_when_nested_schema_is_active(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {},
                "platforms": {
                    "telegram": {
                        "extra": {
                            "allowed_chats": "-100111",
                            "group_allowed_chats": "-100111",
                            "channel_prompts": {"-100111": "existing prompt"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    _make_adapter()._approve_telegram_group("-100222", "new prompt")

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert saved["telegram"] == {}
    target = saved["platforms"]["telegram"]["extra"]
    assert target["allowed_chats"] == "-100111,-100222"
    assert target["group_allowed_chats"] == "-100111,-100222"


def test_group_approval_ignores_field_present_empty_legacy_placeholders(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "allowed_chats": "",
                    "group_allowed_chats": "",
                    "channel_prompts": {},
                },
                "platforms": {
                    "telegram": {
                        "extra": {
                            "allowed_chats": "-100111",
                            "group_allowed_chats": "-100111",
                            "channel_prompts": {"-100111": "existing prompt"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    adapter = _make_adapter()
    projection = adapter._approve_telegram_group("-100222", "new prompt")
    adapter._apply_telegram_group_runtime_projection(projection)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    for target in (
        saved["telegram"],
        saved["platforms"]["telegram"]["extra"],
    ):
        assert set(target["allowed_chats"].split(",")) == {
            "-100111", "-100222"
        }
        assert set(target["group_allowed_chats"].split(",")) == {
            "-100111", "-100222"
        }
        assert target["channel_prompts"] == {
            "-100111": "existing prompt",
            "-100222": "new prompt",
        }


def test_group_approval_preserves_numeric_scalar_allowlists(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        "telegram:\n"
        "  allowed_chats: -100111\n"
        "  group_allowed_chats: -100111\n"
        "  channel_prompts:\n"
        "    '-100111': existing prompt\n",
        encoding="utf-8",
    )

    _make_adapter()._approve_telegram_group("-100222", "new prompt")

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))["telegram"]
    assert saved["allowed_chats"] == "-100111,-100222"
    assert saved["group_allowed_chats"] == "-100111,-100222"
    assert saved["channel_prompts"]["-100222"] == "new prompt"


def test_group_approval_keeps_mixed_schema_runtime_fields_consistent(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "gateway": {
                    "platforms": {
                        "telegram": {
                            "extra": {
                                "allowed_chats": "-100001",
                                "group_allowed_chats": "-100001",
                                "channel_prompts": {"-100001": "gateway prompt"},
                            }
                        }
                    }
                },
                "platforms": {
                    "telegram": {
                        "extra": {
                            "allowed_chats": "-100111",
                            "group_allowed_chats": "-100111",
                            "channel_prompts": {"-100111": "platform prompt"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    effective = load_gateway_config().platforms[Platform.TELEGRAM].extra
    effective_allowed = effective["allowed_chats"]
    if isinstance(effective_allowed, str):
        effective_allowed = effective_allowed.split(",")
    effective_group_allowed = effective["group_allowed_chats"]
    if isinstance(effective_group_allowed, str):
        effective_group_allowed = effective_group_allowed.split(",")
    adapter = _make_adapter(dict(effective))

    adapter._approve_telegram_group("-100222", "new prompt")

    reloaded = load_gateway_config().platforms[Platform.TELEGRAM].extra
    allowed = reloaded["allowed_chats"]
    if isinstance(allowed, str):
        allowed = allowed.split(",")
    group_allowed = reloaded["group_allowed_chats"]
    if isinstance(group_allowed, str):
        group_allowed = group_allowed.split(",")
    assert set(allowed) == {*effective_allowed, "-100222"}
    assert set(group_allowed) == {*effective_group_allowed, "-100222"}
    assert reloaded["channel_prompts"]["-100222"] == "new prompt"


@pytest.mark.asyncio
async def test_non_owner_cannot_approve_group(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "allowed_chats": "-100111",
                    "group_allowed_chats": "-100111",
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "allowed_chats": ["-100111"],
            "group_allowed_chats": ["-100111"],
        }
    )
    nonce = _write_pending(hermes_home)
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_thread_id=None,
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=999, first_name="Mallory"),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))["telegram"]
    assert saved["allowed_chats"] == "-100111"
    query.answer.assert_awaited_once_with(
        text="⛔ Only the configured owner may decide"
    )
    query.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_replayed_or_non_pending_callback_is_rejected(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    _write_pending(hermes_home, nonce="different")
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    query = SimpleNamespace(
        data="ga:a:stale",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(text="This group request has expired")
    assert adapter._telegram_allowed_chats() == set()


@pytest.mark.asyncio
async def test_expired_approval_card_is_removed_without_membership_lookup(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    nonce = _write_pending(
        hermes_home,
        created_at="2000-01-01T00:00:00+00:00",
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(text="This group request has expired")
    assert adapter._bot is not None
    adapter._bot.get_chat_member.assert_not_awaited()
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert nonce not in state["pending"]
    assert state["approved"] == {}


@pytest.mark.asyncio
async def test_send_failure_keeps_retryable_pending_state(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    adapter._bot.send_message.side_effect = OSError("network down")
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=-100222, type="supergroup", title="New Team"),
            new_chat_member=SimpleNamespace(status="administrator"),
            from_user=SimpleNamespace(id=555, full_name="Inviter"),
        )
    )

    await adapter._handle_my_chat_member(update, SimpleNamespace())

    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(state["pending"]) == 1
    request = next(iter(state["pending"].values()))
    assert request["owner_message_id"] is None

    adapter._bot.send_message.side_effect = None
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=9010)
    await adapter._recover_pending_group_approvals()

    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text(
            encoding="utf-8"
        )
    )
    request = next(iter(state["pending"].values()))
    assert request["owner_message_id"] == 9010


@pytest.mark.asyncio
async def test_disabled_feature_rejects_existing_pending_callback(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": False,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(text="Group approval is disabled")


@pytest.mark.asyncio
async def test_missing_safety_prompt_keeps_request_pending(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
        }
    )
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(text="Could not save group approval")
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert nonce in state["pending"]


@pytest.mark.asyncio
async def test_bot_left_before_approval_consumes_request(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="left")
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(
        text="Bot is no longer a member of this group"
    )
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert state["pending"] == {}


@pytest.mark.asyncio
async def test_config_transaction_failure_keeps_request_pending(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    original = "telegram:\n  allowed_chats: -100111\n"
    config_path.write_text(original, encoding="utf-8")
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )

    def fail_transaction(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("utils.atomic_roundtrip_yaml_mutate", fail_transaction)
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)

    query.answer.assert_awaited_once_with(
        text="Decision saved; finalization will retry"
    )
    assert config_path.read_text(encoding="utf-8") == original
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert nonce in state["pending"]
    assert state["pending"][nonce]["decision"] == "a"


@pytest.mark.asyncio
async def test_concurrent_approve_and_deny_consume_nonce_once(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    gate = asyncio.Event()

    async def delayed_membership(*_args, **_kwargs):
        await gate.wait()
        return SimpleNamespace(status="administrator")

    adapter._bot.get_chat_member.side_effect = delayed_membership

    def make_query(choice):
        return SimpleNamespace(
            data=f"ga:{choice}:{nonce}",
            message=SimpleNamespace(
                chat_id=171389200,
                chat=SimpleNamespace(type="private"),
                message_id=9001,
            ),
            from_user=SimpleNamespace(id=171389200),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(),
        )

    approve = make_query("a")
    deny = make_query("d")
    approve_task = asyncio.create_task(
        adapter._handle_group_approval_callback(approve, approve.data)
    )
    await asyncio.sleep(0)
    deny_task = asyncio.create_task(
        adapter._handle_group_approval_callback(deny, deny.data)
    )
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(approve_task, deny_task)

    answers = {
        approve.answer.await_args.kwargs["text"],
        deny.answer.await_args.kwargs["text"],
    }
    assert answers == {"Group approved", "This group request has expired"}
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert state["pending"] == {}
    assert state["rejected"] == {}


@pytest.mark.asyncio
async def test_final_state_failure_leaves_nonce_claimed_and_not_replayable(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    nonce = _write_pending(hermes_home)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    original_save = adapter._save_group_approval_state
    save_calls = 0

    def fail_final_save(state):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("disk full")
        original_save(state)

    adapter._save_group_approval_state = fail_final_save
    query = SimpleNamespace(
        data=f"ga:a:{nonce}",
        message=SimpleNamespace(
            chat_id=171389200,
            chat=SimpleNamespace(type="private"),
            message_id=9001,
        ),
        from_user=SimpleNamespace(id=171389200),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )

    await adapter._handle_group_approval_callback(query, query.data)
    await adapter._handle_group_approval_callback(query, query.data)

    answers = [call.kwargs["text"] for call in query.answer.await_args_list]
    assert answers == [
        "Decision saved; finalization will retry",
        "This group request has expired",
    ]
    state = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text()
    )
    assert state["pending"][nonce]["decision"] == "a"


def test_group_approval_uses_fresh_yaml_not_stale_runtime_allowlists(
    tmp_path, monkeypatch
):
    """A revoked runtime entry must not be resurrected by a later approval."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "allowed_chats": "-100333",
                    "group_allowed_chats": "-100333",
                    "channel_prompts": {"-100333": "fresh prompt"},
                }
            }
        ),
        encoding="utf-8",
    )
    adapter = _make_adapter(
        {
            "allowed_chats": ["-100111"],
            "group_allowed_chats": ["-100111"],
            "channel_prompts": {"-100111": "stale prompt"},
        }
    )

    projection = adapter._approve_telegram_group("-100222", "new prompt")
    adapter._apply_telegram_group_runtime_projection(projection)

    saved = yaml.safe_load(config_path.read_text(encoding="utf-8"))["telegram"]
    assert set(saved["allowed_chats"].split(",")) == {"-100222", "-100333"}
    assert set(saved["group_allowed_chats"].split(",")) == {
        "-100222",
        "-100333",
    }
    assert set(saved["channel_prompts"]) == {"-100222", "-100333"}
    assert adapter._telegram_allowed_chats() == {"-100222", "-100333"}
    assert adapter._telegram_group_allowed_chats() == {"-100222", "-100333"}


@pytest.mark.asyncio
async def test_config_io_runs_off_loop_but_runtime_projection_applies_on_loop(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n")
    adapter = _make_adapter({"group_approval_enabled": True})
    loop_thread = threading.get_ident()
    io_thread = None
    apply_thread = None
    original_io = adapter._approve_telegram_group
    original_apply = adapter._apply_telegram_group_runtime_projection

    def tracked_io(chat_id, prompt):
        nonlocal io_thread
        io_thread = threading.get_ident()
        return original_io(chat_id, prompt)

    def tracked_apply(projection):
        nonlocal apply_thread
        apply_thread = threading.get_ident()
        return original_apply(projection)

    adapter._approve_telegram_group = tracked_io
    adapter._apply_telegram_group_runtime_projection = tracked_apply
    state = {
        "pending": {
            "claimed": {
                "chat_id": "-100222",
                "decision": "a",
                "safety_prompt": "shared safety",
            }
        },
        "approved": {},
        "rejected": {},
        "leave_actions": {},
    }

    await adapter._finalize_claimed_group_approval(
        state, "claimed", state["pending"]["claimed"]
    )

    assert io_thread is not None and io_thread != loop_thread
    assert apply_thread == loop_thread
    assert adapter._telegram_allowed_chats() == {"-100222"}


@pytest.mark.asyncio
async def test_recovery_finalizes_claimed_approval_after_restart(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    (hermes_home / "config.yaml").write_text("telegram: {}\n", encoding="utf-8")
    state = {
        "pending": {
            "claimed": {
                "chat_id": "-100222",
                "title": "New Team",
                "owner_message_id": 9001,
                "decision": "a",
                "safety_prompt": "persisted safety prompt",
            }
        },
        "rejected": {},
        "approved": {},
    }
    (hermes_home / "telegram_group_approvals.json").write_text(
        json.dumps(state), encoding="utf-8"
    )
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
        }
    )

    needs_retry = await adapter._recover_pending_group_approvals()

    assert needs_retry is False
    recovered = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text(encoding="utf-8")
    )
    assert recovered["pending"] == {}
    assert recovered["approved"]["-100222"]["safety_prompt"] == (
        "persisted safety prompt"
    )
    assert adapter._telegram_allowed_chats() == {"-100222"}
    assert adapter._telegram_group_allowed_chats() == {"-100222"}


@pytest.mark.asyncio
async def test_initial_notification_failure_retries_in_process(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
            "group_approval_channel_prompt": "shared-group safety prompt",
        }
    )
    adapter._group_approval_retry_delays = (0,)
    adapter._bot.send_message.side_effect = [
        OSError("network down"),
        SimpleNamespace(message_id=9010),
    ]
    update = SimpleNamespace(
        my_chat_member=SimpleNamespace(
            chat=SimpleNamespace(id=-100222, type="supergroup", title="New Team"),
            new_chat_member=SimpleNamespace(status="administrator"),
            from_user=SimpleNamespace(id=555, full_name="Inviter"),
        )
    )

    await adapter._handle_my_chat_member(update, SimpleNamespace())
    await asyncio.gather(*list(adapter._group_approval_retry_tasks))

    assert adapter._bot.send_message.await_count == 2
    recovered = json.loads(
        (hermes_home / "telegram_group_approvals.json").read_text(encoding="utf-8")
    )
    assert next(iter(recovered["pending"].values()))["owner_message_id"] == 9010


@pytest.mark.asyncio
async def test_recovery_notification_failure_gets_bounded_retry(
    tmp_path, monkeypatch
):
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_pending(hermes_home, message_id=None)
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
        }
    )
    adapter._group_approval_retry_delays = (0, 0)
    adapter._bot.send_message.side_effect = OSError("network down")

    adapter._schedule_group_approval_recovery(immediate=True)
    await asyncio.gather(*list(adapter._group_approval_retry_tasks))

    # One immediate recovery plus exactly two bounded retries.
    assert adapter._bot.send_message.await_count == 3
    assert not adapter._group_approval_retry_tasks


@pytest.mark.asyncio
async def test_owner_notification_error_is_redacted(caplog):
    adapter = _make_adapter(
        {
            "group_approval_enabled": True,
            "group_approval_owner_id": "171389200",
        }
    )
    token = "123456789:AAExampleTelegramBotToken_1234567890"
    adapter._bot.send_message.side_effect = RuntimeError(
        f"https://api.telegram.org/bot{token}/sendMessage failed"
    )

    delivered = await adapter._deliver_group_approval_request(
        "nonce", {"chat_id": "-100222", "title": "New Team"}
    )

    assert delivered is False
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_disconnect_bounds_hung_approval_task_gather(monkeypatch):
    adapter = _make_adapter()
    blocker = asyncio.Event()

    async def ignores_cancellation():
        try:
            await blocker.wait()
        except asyncio.CancelledError:
            await blocker.wait()

    task = asyncio.create_task(ignores_cancellation())
    await asyncio.sleep(0)
    adapter._group_approval_retry_tasks.add(task)
    observed = []

    async def bounded(awaitable, timeout, step):
        observed.append((timeout, step))
        if hasattr(awaitable, "close"):
            awaitable.close()
        return False

    monkeypatch.setattr(adapter, "_await_disconnect_step", bounded)
    # Stop after the enrollment cleanup seam; the rest of disconnect is unrelated.
    adapter._release_platform_lock = Mock(side_effect=RuntimeError("stop here"))

    with pytest.raises(RuntimeError, match="stop here"):
        await adapter.disconnect()

    assert observed and observed[0][0] > 0
    assert observed[0][1] == "group-approval retry-task cancel"
    blocker.set()
    await task
