"""Security tests for owner-only Telegram group inventory and leave controls."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml

from gateway.config import PlatformConfig
from plugins.platforms.telegram.adapter import TelegramAdapter


OWNER = 171389200
CHAT = -100222


def _adapter(extra=None):
    values = {
        "group_approval_enabled": True,
        "group_approval_owner_id": str(OWNER),
        "allowed_chats": [str(CHAT)],
        "group_allowed_chats": [str(CHAT)],
        "channel_prompts": {str(CHAT): "Observe quietly; answer only when mentioned."},
    }
    values.update(extra or {})
    adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test", extra=values))
    adapter._bot = AsyncMock()
    adapter._bot.id = 777
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, title="Operations Team", username="operations_team", invite_link=None
    )
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    adapter._bot.send_message.return_value = SimpleNamespace(message_id=444)
    return adapter


def _message(user_id=OWNER, chat_id=OWNER, chat_type="private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(id=chat_id, type=chat_type),
        chat_id=chat_id,
    )


def _state(home, **extra):
    state = {
        "pending": {},
        "rejected": {},
        "approved": {
            str(CHAT): {
                "chat_id": str(CHAT),
                "title": "Operations Team",
                "safety_prompt": "Observe quietly; answer only when mentioned.",
                "synchronized": True,
            }
        },
        "leave_actions": {},
    }
    state.update(extra)
    (home / "telegram_group_approvals.json").write_text(json.dumps(state), encoding="utf-8")
    return state


def test_group_role_prefers_title_over_forbidden_terms_inside_prompt():
    adapter = _adapter({
        "channel_prompts": {
            str(CHAT): "Never expose unrelated financial data; answer only when mentioned."
        }
    })
    assert adapter._group_inventory_behavior(str(CHAT), "Зйомка 18.08") == "координатор съёмки"


@pytest.fixture(autouse=True)
def _telegram_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_ALLOWED_CHATS", raising=False)
    monkeypatch.delenv("TELEGRAM_GROUP_ALLOWED_CHATS", raising=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user_id", "chat_id", "chat_type"),
    [(999, OWNER, "private"), (OWNER, -10, "group"), (OWNER, 999, "private")],
)
async def test_groups_inventory_is_owner_private_dm_only(tmp_path, monkeypatch, user_id, chat_id, chat_type):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    adapter = _adapter()

    await adapter._handle_groups_command(
        SimpleNamespace(effective_message=_message(user_id, chat_id, chat_type)),
        SimpleNamespace(),
    )

    adapter._bot.get_chat.assert_not_awaited()
    adapter._bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_groups_inventory_is_compact_linked_and_has_one_leave_menu_button(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )
    adapter = _adapter()

    await adapter._handle_groups_command(
        SimpleNamespace(effective_message=_message()), SimpleNamespace()
    )

    kwargs = adapter._bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == OWNER
    assert kwargs["parse_mode"] == "HTML"
    assert '<a href="https://t.me/operations_team">Operations Team</a>' in kwargs["text"]
    assert str(CHAT) not in kwargs["text"]
    assert "administrator" not in kwargs["text"]
    assert "ассистент по обращению" in kwargs["text"]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].text == "🚪 Выйти из группы"
    assert rows[0][0].callback_data.startswith("gm:")
    panel_nonce = rows[0][0].callback_data.split(":", 1)[1]
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["leave_actions"] == {}
    panel = saved["inventory_panels"][panel_nonce]
    assert panel["owner_id"] == OWNER
    assert panel["owner_chat_id"] == OWNER
    assert panel["owner_message_id"] == 444
    assert panel["status"] == "active"
    assert panel["groups"][0]["chat_id"] == str(CHAT)


@pytest.mark.asyncio
async def test_groups_inventory_uses_basic_group_desktop_deep_link_and_skips_departed_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    adapter = _adapter()
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, type="group", title="Private Team", username=None, invite_link=None
    )

    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert 'href="tg://openmessage?chat_id=-100222"' in adapter._bot.send_message.await_args.kwargs["text"]

    adapter._bot.reset_mock()
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="left")
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert "No live approved or allowlisted groups" in adapter._bot.send_message.await_args.kwargs["text"]
    assert adapter._bot.send_message.await_args.kwargs.get("reply_markup") is None


@pytest.mark.asyncio
async def test_groups_inventory_excludes_rejected_stale_allowlist(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["approved"] = {}
    state["rejected"] = {str(CHAT): {"chat_id": str(CHAT)}}
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()

    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)

    adapter._bot.get_chat.assert_not_awaited()
    assert "No live approved or allowlisted groups" in adapter._bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_groups_inventory_uses_existing_safe_invite_but_rejects_unsafe_link(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    adapter = _adapter()
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, type="supergroup", title="Private Team", username=None,
        invite_link="https://t.me/+existingInvite",
    )
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert "https://t.me/+existingInvite" in adapter._bot.send_message.await_args.kwargs["text"]

    adapter._bot.reset_mock()
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, type="supergroup", title="Private Team", username=None,
        invite_link="javascript:alert(1)",
    )
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert 'href="https://t.me/c/222/1"' in adapter._bot.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_leave_menu_button_opens_group_picker_with_fresh_bound_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )
    adapter = _adapter()
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    command_markup = adapter._bot.send_message.await_args.kwargs["reply_markup"]
    panel_data = command_markup.inline_keyboard[0][0].callback_data
    panel_nonce = panel_data.split(":", 1)[1]
    query = SimpleNamespace(
        data=panel_data,
        from_user=SimpleNamespace(id=OWNER),
        message=SimpleNamespace(
            chat_id=OWNER, message_id=444,
            chat=SimpleNamespace(id=OWNER, type="private"),
        ),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

    query.answer.assert_awaited_once()
    picker = query.edit_message_text.await_args.kwargs
    assert picker["text"] == "Из какой группы выйти?"
    rows = picker["reply_markup"].inline_keyboard
    assert rows[0][0].text == "Operations Team"
    assert rows[0][0].callback_data.startswith("gl:")
    assert rows[-1][0].text == "↩️ Назад"
    assert rows[-1][0].callback_data == f"gb:{panel_nonce}"
    leave_nonce = rows[0][0].callback_data.split(":", 1)[1]
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    action = saved["leave_actions"][leave_nonce]
    assert action["owner_id"] == OWNER
    assert action["owner_chat_id"] == OWNER
    assert action["owner_message_id"] == 444
    assert action["chat_id"] == str(CHAT)
    assert action["status"] == "pending"

    back = SimpleNamespace(
        data=f"gb:{panel_nonce}",
        from_user=SimpleNamespace(id=OWNER),
        message=query.message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    await adapter._handle_callback_query(SimpleNamespace(callback_query=back), None)
    restored = back.edit_message_text.await_args.kwargs
    assert restored["parse_mode"] == "HTML"
    assert "Operations Team" in restored["text"]
    assert restored["reply_markup"].inline_keyboard[0][0].text == "🚪 Выйти из группы"
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["leave_actions"][leave_nonce]["status"] == "expired"


@pytest.mark.asyncio
async def test_leave_menu_rejects_wrong_owner_and_wrong_message(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["inventory_panels"] = {
        "panel": {
            "owner_id": OWNER, "owner_chat_id": OWNER,
            "owner_message_id": 444, "status": "active", "groups": [],
            "summary_text": "summary",
        }
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    for user_id, message_id in ((999, 444), (OWNER, 445)):
        query = SimpleNamespace(
            data="gm:panel", from_user=SimpleNamespace(id=user_id),
            message=SimpleNamespace(
                chat_id=OWNER, message_id=message_id,
                chat=SimpleNamespace(id=OWNER, type="private"),
            ),
            answer=AsyncMock(), edit_message_text=AsyncMock(),
        )
        await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)
        query.answer.assert_awaited_once_with(text="Это меню устарело")
        query.edit_message_text.assert_not_awaited()
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["leave_actions"] == {}


@pytest.mark.asyncio
async def test_reopening_picker_expires_old_leave_nonce(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["inventory_panels"] = {
        "panel": {
            "owner_id": OWNER, "owner_chat_id": OWNER,
            "owner_message_id": 444, "status": "active",
            "groups": [{"chat_id": str(CHAT), "title": "Operations Team"}],
            "summary_text": "summary",
        }
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardButton",
        lambda text, callback_data: SimpleNamespace(text=text, callback_data=callback_data),
    )
    monkeypatch.setattr(
        "plugins.platforms.telegram.adapter.InlineKeyboardMarkup",
        lambda rows: SimpleNamespace(inline_keyboard=rows),
    )
    adapter = _adapter()
    query = SimpleNamespace(
        data="gm:panel", from_user=SimpleNamespace(id=OWNER),
        message=SimpleNamespace(
            chat_id=OWNER, message_id=444,
            chat=SimpleNamespace(id=OWNER, type="private"),
        ),
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    await adapter._handle_group_inventory_panel_callback(query, query.data)
    first_data = query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    await adapter._handle_group_inventory_panel_callback(query, query.data)
    second_data = query.edit_message_text.await_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
    assert first_data != second_data
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["leave_actions"][first_data.split(":", 1)[1]]["status"] == "expired"
    assert saved["leave_actions"][second_data.split(":", 1)[1]]["status"] == "pending"

    old = _leave_query(first_data.split(":", 1)[1])
    await adapter._handle_group_leave_callback(old, first_data)
    adapter._bot.leave_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_empty_inventory_refresh_expires_previous_panel_and_actions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["inventory_panels"] = {
        "old": {
            "owner_id": OWNER, "owner_chat_id": OWNER,
            "owner_message_id": 444, "status": "active",
        }
    }
    state["leave_actions"] = {
        "old-action": {
            "chat_id": str(CHAT), "owner_id": OWNER,
            "owner_chat_id": OWNER, "owner_message_id": 444,
            "status": "pending", "panel_nonce": "old",
        }
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="left")

    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)

    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["inventory_panels"]["old"]["status"] == "expired"
    assert saved["leave_actions"]["old-action"]["status"] == "expired"


def _write_config(home):
    (home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "telegram": {
                    "allowed_chats": f"-100111,{CHAT}",
                    "group_allowed_chats": f"-100111,{CHAT}",
                    "channel_prompts": {"-100111": "keep", str(CHAT): "remove"},
                },
                "platforms": {
                    "telegram": {
                        "extra": {
                            "allowed_chats": ["-100111", str(CHAT)],
                            "group_allowed_chats": ["-100111", str(CHAT)],
                            "channel_prompts": {"-100111": "keep", str(CHAT): "remove"},
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _leave_query(nonce, *, user_id=OWNER, chat_id=OWNER, message_id=444, chat_type="private"):
    return SimpleNamespace(
        data=f"gl:{nonce}",
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            chat=SimpleNamespace(id=chat_id, type=chat_type),
        ),
        answer=AsyncMock(),
        edit_message_text=AsyncMock(),
    )


@pytest.mark.asyncio
async def test_valid_leave_callback_blocks_cleans_every_existing_target_then_leaves_once(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path)
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "pending",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    query = _leave_query("secret")

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), None)

    adapter._bot.leave_chat.assert_awaited_once_with(chat_id=CHAT)
    saved_state = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert str(CHAT) in saved_state["rejected"]
    assert str(CHAT) not in saved_state["approved"]
    assert saved_state["leave_actions"]["secret"]["status"] == "completed"
    config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    for target in (config["telegram"], config["platforms"]["telegram"]["extra"]):
        assert str(CHAT) not in str(target["allowed_chats"])
        assert str(CHAT) not in str(target["group_allowed_chats"])
        assert str(CHAT) not in target["channel_prompts"]
        assert "-100111" in str(target["allowed_chats"])
    assert str(CHAT) not in adapter._telegram_allowed_chats()
    query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        _leave_query("secret", user_id=999),
        _leave_query("secret", chat_id=999),
        _leave_query("secret", message_id=445),
        _leave_query("secret", chat_type="group"),
        _leave_query("unknown"),
    ],
)
async def test_leave_callback_rejects_wrong_binding_and_unknown_nonce(tmp_path, monkeypatch, query):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "pending",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()

    await adapter._handle_group_leave_callback(query, query.data)

    adapter._bot.leave_chat.assert_not_awaited()
    unchanged = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert unchanged["leave_actions"]["secret"]["status"] == "pending"


@pytest.mark.asyncio
async def test_leave_callback_is_one_shot_and_departed_bot_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "completed",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    await adapter._handle_group_leave_callback(_leave_query("secret"), "gl:secret")
    adapter._bot.leave_chat.assert_not_awaited()

    state["leave_actions"]["secret"]["status"] = "pending"
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="left")
    await adapter._handle_group_leave_callback(_leave_query("secret"), "gl:secret")
    adapter._bot.leave_chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_leave_failure_retains_blocked_recoverable_claim_and_recovery_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path)
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "pending",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    adapter._schedule_group_approval_recovery = lambda **kwargs: None
    adapter._bot.leave_chat.side_effect = RuntimeError("network")

    await adapter._handle_group_leave_callback(_leave_query("secret"), "gl:secret")

    failed = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert failed["leave_actions"]["secret"]["status"] == "claimed"
    assert failed["leave_actions"]["secret"]["stage"] == "config_cleaned"
    assert str(CHAT) in failed["rejected"]
    assert adapter._is_effectively_approved_group(str(CHAT)) is False

    adapter._bot.leave_chat.side_effect = None
    adapter._bot.get_chat_member.return_value = SimpleNamespace(status="administrator")
    assert await adapter._recover_pending_group_approvals() is False
    recovered = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert recovered["leave_actions"]["secret"]["status"] == "completed"
    assert adapter._bot.leave_chat.await_count == 2


@pytest.mark.asyncio
async def test_config_cleanup_failure_never_leaves_stale_authorization_and_retries(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path)
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "pending",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter()
    adapter._schedule_group_approval_recovery = lambda **kwargs: None
    real_remove = adapter._remove_telegram_group
    adapter._remove_telegram_group = lambda chat_id: (_ for _ in ()).throw(OSError("locked"))

    await adapter._handle_group_leave_callback(_leave_query("secret"), "gl:secret")

    failed = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert failed["leave_actions"]["secret"]["status"] == "claimed"
    assert str(CHAT) in failed["rejected"]
    assert adapter._is_effectively_approved_group(str(CHAT)) is False
    adapter._bot.leave_chat.assert_not_awaited()

    adapter._remove_telegram_group = real_remove
    assert await adapter._recover_pending_group_approvals() is False
    recovered = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert recovered["leave_actions"]["secret"]["status"] == "completed"
    adapter._bot.leave_chat.assert_awaited_once_with(chat_id=CHAT)


@pytest.mark.asyncio
async def test_leave_claim_blocks_stale_allowlist_when_approval_feature_is_disabled(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _write_config(tmp_path)
    state = _state(tmp_path)
    state["leave_actions"]["secret"] = {
        "chat_id": str(CHAT), "owner_id": OWNER, "owner_chat_id": OWNER,
        "owner_message_id": 444, "status": "pending",
    }
    (tmp_path / "telegram_group_approvals.json").write_text(json.dumps(state))
    adapter = _adapter({"group_approval_enabled": False})
    adapter._schedule_group_approval_recovery = lambda **kwargs: None
    adapter._remove_telegram_group = lambda chat_id: (_ for _ in ()).throw(
        OSError("locked")
    )

    await adapter._handle_group_leave_callback(_leave_query("secret"), "gl:secret")

    failed = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert failed["leave_actions"]["secret"]["status"] == "claimed"
    assert str(CHAT) in failed["rejected"]
    assert adapter._is_effectively_approved_group(str(CHAT)) is False
    assert adapter._bot is not None
    adapter._bot.leave_chat.assert_not_awaited()


def test_leave_projection_ignores_empty_top_level_routing_placeholders(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
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
                            "allowed_chats": ["-100111", str(CHAT)],
                            "group_allowed_chats": ["-100111", str(CHAT)],
                            "channel_prompts": {
                                "-100111": "keep", str(CHAT): "remove"
                            },
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    adapter = _adapter({"group_approval_enabled": False})

    projection = adapter._remove_telegram_group(str(CHAT))
    adapter._apply_telegram_group_runtime_projection(projection)

    assert adapter._telegram_allowed_chats() == {"-100111"}
    assert adapter._telegram_group_allowed_chats() == {"-100111"}
    assert adapter.config.extra["channel_prompts"] == {"-100111": "keep"}
