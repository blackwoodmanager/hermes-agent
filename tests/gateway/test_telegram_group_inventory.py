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
async def test_groups_inventory_lists_only_live_groups_with_safe_links_and_bound_nonces(tmp_path, monkeypatch):
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
    assert "Operations Team" in kwargs["text"]
    assert str(CHAT) in kwargs["text"]
    assert "administrator" in kwargs["text"]
    assert "https://t.me/operations_team" in kwargs["text"]
    assert "mentioned" in kwargs["text"]
    rows = kwargs["reply_markup"].inline_keyboard
    assert len(rows) == 1
    assert rows[0][0].text.startswith("🚪 Operations Team")
    assert rows[0][0].callback_data.startswith("gl:")
    assert str(CHAT) not in rows[0][0].callback_data
    nonce = rows[0][0].callback_data.split(":", 1)[1]
    saved = json.loads((tmp_path / "telegram_group_approvals.json").read_text())
    assert saved["leave_actions"][nonce]["owner_id"] == OWNER
    assert saved["leave_actions"][nonce]["owner_chat_id"] == OWNER
    assert saved["leave_actions"][nonce]["owner_message_id"] == 444
    assert saved["leave_actions"][nonce]["chat_id"] == str(CHAT)
    assert saved["leave_actions"][nonce]["status"] == "pending"


@pytest.mark.asyncio
async def test_groups_inventory_states_link_unavailable_and_skips_departed_bot(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _state(tmp_path)
    adapter = _adapter()
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, title="Private Team", username=None, invite_link=None
    )

    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert "link: unavailable" in adapter._bot.send_message.await_args.kwargs["text"].lower()

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
        id=CHAT, title="Private Team", username=None,
        invite_link="https://t.me/+existingInvite",
    )
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert "https://t.me/+existingInvite" in adapter._bot.send_message.await_args.kwargs["text"]

    adapter._bot.reset_mock()
    adapter._bot.get_chat.return_value = SimpleNamespace(
        id=CHAT, title="Private Team", username=None,
        invite_link="javascript:alert(1)",
    )
    await adapter._handle_groups_command(SimpleNamespace(effective_message=_message()), None)
    assert "link: unavailable" in adapter._bot.send_message.await_args.kwargs["text"]


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
