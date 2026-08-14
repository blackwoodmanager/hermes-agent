from __future__ import annotations

import importlib.util
import json
import multiprocessing
import os
import sqlite3
import time
from pathlib import Path

import pytest
from types import SimpleNamespace

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "ops" / "unfinished_task_review.py"
spec = importlib.util.spec_from_file_location("unfinished_task_review", SCRIPT)
review = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(review)


def _mutate_worker(home: str, key: str, delay: float) -> None:
    import os

    os.environ["HERMES_TASK_REVIEW_STATE_PATH"] = str(Path(home) / "review-state.json")

    def mutate(data: dict) -> None:
        outcomes = data.setdefault("outcomes", {})
        time.sleep(delay)
        outcomes[key] = {"disposition": "closed"}

    review._mutate_state(mutate)


def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TASK_REVIEW_STATE_PATH", str(tmp_path / "review-state.json"))
    con = sqlite3.connect(tmp_path / "state.db")
    con.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, chat_type TEXT,
            title TEXT, profile_name TEXT, archived INTEGER DEFAULT 0,
            parent_session_id TEXT, session_key TEXT, started_at TEXT, ended_at TEXT,
            end_reason TEXT, last_activity_at TEXT, model_config TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
            tool_name TEXT, content TEXT, timestamp REAL, display_kind TEXT
        );
        INSERT INTO sessions
            (id, source, chat_id, chat_type, title, profile_name, archived, parent_session_id)
        VALUES ('S', 'desktop', NULL, NULL, 'test', 'default', 0, NULL);
        """
    )
    return con


def _user(con: sqlite3.Connection, text: str, ts: float) -> None:
    con.execute(
        "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('S','user',?,?)",
        (text, ts),
    )
    con.commit()


def _todo(con: sqlite3.Connection, todos: list[dict], ts: float) -> None:
    con.execute(
        "INSERT INTO messages(session_id,role,tool_name,content,timestamp) VALUES ('S','tool','todo',?,?)",
        (json.dumps({"todos": todos}), ts),
    )
    con.commit()


def test_valid_empty_snapshot_clears_old_outcomes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "old task", 1)
    _todo(con, [{"id": "verify", "content": "Verify old", "status": "pending"}], 2)
    _todo(con, [], 3)

    assert review.snapshot()["outcomes"] == []


def test_direct_same_id_replacement_starts_new_generation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "old request", 1)
    _todo(con, [{"id": "verify", "content": "Verify old", "status": "pending"}], 2)
    old_key = review.snapshot()["outcomes"][0]["key"]
    review.set_disposition(old_key, "closed", None)

    _user(con, "new request", 3)
    _todo(con, [{"id": "verify", "content": "Verify new", "status": "pending"}], 4)
    visible = review.snapshot()["outcomes"]

    assert len(visible) == 1
    assert visible[0]["key"] != old_key
    assert visible[0]["title"] == "new request"
    assert visible[0]["items"][0]["content"] == "Verify new"


def test_compression_continuation_supersedes_root_todos_and_is_resume_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    con.execute(
        "UPDATE sessions SET archived=1, ended_at='2026-08-15T00:00:00+00:00', "
        "end_reason='compression', started_at='2026-08-14T20:00:00+00:00' WHERE id='S'"
    )
    con.execute(
        """INSERT INTO sessions
           (id, source, chat_id, chat_type, title, profile_name, archived,
            parent_session_id, started_at, ended_at, end_reason, last_activity_at)
           VALUES ('C', 'desktop', NULL, NULL, 'continued', 'default', 0,
                   'S', '2026-08-15T00:00:01+00:00', NULL, NULL,
                   '2026-08-15T00:01:00+00:00')"""
    )
    con.execute(
        """INSERT INTO sessions
           (id, source, chat_id, chat_type, title, profile_name, archived,
            parent_session_id, started_at, ended_at, end_reason,
            last_activity_at, model_config)
           VALUES ('D', 'desktop', NULL, NULL, 'delegate', 'default', 0,
                   'S', '2026-08-15T00:00:02+00:00', NULL, NULL,
                   '2026-08-15T00:02:00+00:00', '{"_delegate_from":"S"}')"""
    )
    _user(con, "old root task", 1)
    _todo(con, [{"id": "work", "content": "Old work", "status": "pending"}], 2)
    con.execute(
        "INSERT INTO messages(session_id,role,tool_name,content,timestamp) VALUES ('C','tool','todo',?,?)",
        (json.dumps({"todos": []}), 3),
    )
    con.execute(
        "INSERT INTO messages(session_id,role,tool_name,content,timestamp) VALUES ('D','tool','todo',?,?)",
        (
            json.dumps(
                {"todos": [{"id": "sub", "content": "Delegate-only work", "status": "pending"}]}
            ),
            3.5,
        ),
    )
    con.commit()

    assert review.snapshot()["outcomes"] == []

    con.execute(
        "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('C','user',?,?)",
        ("new continuation task", 4),
    )
    con.execute(
        "INSERT INTO messages(session_id,role,tool_name,content,timestamp) VALUES ('C','tool','todo',?,?)",
        (json.dumps({"todos": [{"id": "work", "content": "New work", "status": "pending"}]}), 5),
    )
    con.commit()

    visible = review.snapshot()["outcomes"]
    assert len(visible) == 1
    assert visible[0]["session_id"] == "C"
    assert visible[0]["session_link"] == "@session:default/C"
    assert visible[0]["title"] == "new continuation task"
    assert visible[0]["items"][0]["content"] == "New work"


def test_user_visible_reset_child_is_a_separate_review_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    con.execute(
        "UPDATE sessions SET session_key='desktop:owner', "
        "ended_at='2026-08-15T00:00:00+00:00', end_reason='session_reset' WHERE id='S'"
    )
    con.execute(
        """INSERT INTO sessions
           (id, source, title, profile_name, archived, parent_session_id,
            session_key, started_at, model_config)
           VALUES ('R', 'desktop', 'reset conversation', 'default', 0, 'S',
                   'desktop:owner', '2026-08-15T00:00:01+00:00',
                   '{"_reset_from":"S"}')"""
    )
    con.execute(
        "INSERT INTO messages(session_id,role,content,timestamp) VALUES ('R','user',?,?)",
        ("new reset task", 3),
    )
    con.execute(
        "INSERT INTO messages(session_id,role,tool_name,content,timestamp) VALUES ('R','tool','todo',?,?)",
        (json.dumps({"todos": [{"id": "work", "content": "Reset work", "status": "pending"}]}), 4),
    )
    con.commit()

    visible = review.snapshot()["outcomes"]
    assert len(visible) == 1
    assert visible[0]["session_id"] == "R"
    assert visible[0]["title"] == "new reset task"


def test_non_archived_ended_session_remains_reviewable_and_reopenable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    con.execute(
        "UPDATE sessions SET ended_at='2026-08-15T00:00:00+00:00', "
        "end_reason='agent_close', archived=0 WHERE id='S'"
    )
    _user(con, "unfinished ended task", 1)
    _todo(con, [{"id": "work", "content": "Resume me", "status": "pending"}], 2)

    visible = review.snapshot()["outcomes"]
    assert len(visible) == 1
    assert visible[0]["session_id"] == "S"
    assert visible[0]["items"][0]["content"] == "Resume me"


def test_concurrent_state_mutations_do_not_lose_updates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HERMES_TASK_REVIEW_STATE_PATH", str(tmp_path / "review-state.json"))
    ctx = multiprocessing.get_context("fork")
    first = ctx.Process(target=_mutate_worker, args=(str(tmp_path), "first", 0.2))
    second = ctx.Process(target=_mutate_worker, args=(str(tmp_path), "second", 0.0))
    first.start()
    time.sleep(0.03)
    second.start()
    first.join(3)
    second.join(3)

    assert first.exitcode == 0
    assert second.exitcode == 0
    state = json.loads((tmp_path / "review-state.json").read_text())
    assert set(state["outcomes"]) == {"first", "second"}


def test_date_only_until_is_ten_am_kyiv_across_dst() -> None:
    due = review._parse_until("2026-10-25")

    assert due.isoformat() == "2026-10-25T10:00:00+02:00"
    assert getattr(due.tzinfo, "key", None) == "Europe/Kyiv"


def test_render_cards_has_three_clear_buttons_and_no_visible_keys() -> None:
    snapshot = {
        "outcomes": [
            {
                "key": "69b6e6d7f2",
                "title": "Поправить обзор задач",
                "items": [
                    {"id": "fix", "content": "Добавить нормальные кнопки", "status": "in_progress"}
                ],
            }
        ]
    }

    token = "abcdef012345"
    cards = review.render_task_cards(snapshot, {"69b6e6d7f2": token})

    assert len(cards) == 1
    card = cards[0]
    assert "69b6e6d7f2" not in card["text"]
    assert [button["text"] for button in card["buttons"]] == [
        "▶️ Продолжить",
        "⏸ Отложить",
        "✅ Закрыть",
    ]
    assert all("69b6e6d7f2" not in button["text"] for button in card["buttons"])
    assert [button["callback_data"] for button in card["buttons"]] == [
        f"ur:c:69b6e6d7f2:{token}",
        f"ur:d:69b6e6d7f2:{token}",
        f"ur:x:69b6e6d7f2:{token}",
    ]
    assert all(len(button["callback_data"].encode()) <= 64 for button in card["buttons"])


def test_default_defer_uses_next_ten_am_kyiv() -> None:
    before = review.datetime(2026, 8, 15, 9, 30, tzinfo=review.REVIEW_TZ)
    after = review.datetime(2026, 8, 15, 10, 30, tzinfo=review.REVIEW_TZ)

    assert review._next_daily_review(before).isoformat() == "2026-08-15T10:00:00+03:00"
    assert review._next_daily_review(after).isoformat() == "2026-08-16T10:00:00+03:00"


def test_action_token_is_first_decision_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    key = review.snapshot()["outcomes"][0]["key"]

    token = review._review_action_token(key, "daily:2026-08-15")
    first = review.set_disposition(key, "closed", None, action_token=token)
    replay = review.set_disposition(key, "deferred", None, action_token=token)

    assert first["applied"] is True
    assert replay["applied"] is False
    state = json.loads((tmp_path / "review-state.json").read_text())
    assert state["outcomes"][key]["disposition"] == "closed"


def test_continue_reservation_commits_only_after_acceptance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    outcome = review.snapshot()["outcomes"][0]
    key = outcome["key"]
    token = review._review_action_token(key, "daily:2026-08-15")

    route_session_key = "telegram:dm:171389200"
    reserved = review.reserve_action(key, token, session_key=route_session_key)
    replay = review.reserve_action(key, token, session_key=route_session_key)
    state = json.loads((tmp_path / "review-state.json").read_text())
    assert reserved["applied"] is True
    assert replay["applied"] is False
    assert token in state["pending_actions"]
    assert state["pending_actions"][token]["session_key"] == route_session_key
    assert review.list_pending_actions()["pending"] == [
        {
            "action_token": token,
            "key": key,
            "session_id": outcome["session_id"],
            "session_key": route_session_key,
        }
    ]
    assert token not in state["actions"]
    assert key not in state.get("outcomes", {})

    assert review.release_action(key, token)["released"] is True
    assert review.reserve_action(key, token, session_key=route_session_key)["applied"] is True
    committed = review.set_disposition(key, "open", None, action_token=token)
    state = json.loads((tmp_path / "review-state.json").read_text())
    assert committed["applied"] is True
    assert token in state["actions"]
    assert token not in state["pending_actions"]


def test_startup_reconcile_accepts_pending_for_matching_session_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    outcome = review.snapshot()["outcomes"][0]
    key = outcome["key"]
    token = review._review_action_token(key, "daily:2026-08-15")
    route_session_key = "telegram:dm:171389200"
    review.reserve_action(key, token, session_key=route_session_key)

    assert review.accept_pending_for_session(
        "wrong-session", route_session_key, token
    )["accepted"] == []
    assert review.accept_pending_for_session(
        outcome["session_id"], "telegram:dm:alias", token
    )["accepted"] == []
    assert review.accept_pending_for_session(
        outcome["session_id"], route_session_key, "000000000000"
    )["accepted"] == []
    recovered = review.accept_pending_for_session(
        outcome["session_id"], route_session_key, token
    )
    state = json.loads((tmp_path / "review-state.json").read_text())
    assert recovered["accepted"] == [token]
    assert state["actions"][token]["recovered"] is True
    assert token not in state["pending_actions"]
    assert review.action_status(key, token)["status"] == "committed_open"


def test_only_one_pending_continue_is_allowed_per_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    outcome = review.snapshot()["outcomes"][0]
    key = outcome["key"]
    token = review._review_action_token(key, "daily:2026-08-15")
    route = "telegram:dm:171389200"
    state_path = tmp_path / "review-state.json"
    state = json.loads(state_path.read_text())
    state.setdefault("pending_actions", {})["111111111111"] = {
        "key": "other-key",
        "session_id": "other-session",
        "session_key": route,
        "status": "pending",
    }
    state_path.write_text(json.dumps(state))

    result = review.reserve_action(key, token, session_key=route)

    assert result["applied"] is False
    assert result["reason"] == "route_pending"
    assert review.action_status(key, token)["status"] == "absent"


def test_next_review_rotates_token_after_continue_and_stale_card_stays_inert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    key = review.snapshot()["outcomes"][0]["key"]
    old_token = review._review_action_token(key, "daily:2026-08-15")
    assert review.set_disposition(key, "open", None, action_token=old_token)["applied"] is True

    new_token = review._review_action_token(key, "daily:2026-08-16")
    assert new_token != old_token
    assert review.set_disposition(key, "closed", None, action_token=old_token)["applied"] is False
    assert review.set_disposition(key, "closed", None, action_token=new_token)["applied"] is True


def test_expired_defer_gets_new_action_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    key = review.snapshot()["outcomes"][0]["key"]
    old_token = review._review_action_token(key, "daily:2026-08-14")
    deferred = review.set_disposition(
        key,
        "deferred",
        "2020-01-01T10:00:00+02:00",
        action_token=old_token,
    )
    assert deferred["applied"] is True
    assert review.action_status(key, old_token)["status"] == "committed_other"
    assert review.snapshot()["outcomes"][0]["key"] == key

    new_token = review._review_action_token(key, "daily:2026-08-15")
    assert new_token != old_token
    assert review.set_disposition(key, "closed", None, action_token=old_token)["applied"] is False
    assert review.set_disposition(key, "closed", None, action_token=new_token)["applied"] is True


def test_stale_visible_snapshot_cannot_issue_card_after_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    con = _home(tmp_path, monkeypatch)
    _user(con, "task", 1)
    _todo(con, [{"id": "work", "content": "Do work", "status": "pending"}], 2)
    stale_snapshot = review.snapshot()
    key = stale_snapshot["outcomes"][0]["key"]
    review.set_disposition(key, "closed", None)

    token = review._review_action_token(key, "daily:2026-08-15")
    assert token == ""
    assert review.render_task_cards(stale_snapshot, {key: token}) == []


def test_restart_identity_rejects_non_gateway_or_recycled_pid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "gateway_state.json").write_text(
        json.dumps(
            {
                "kind": "hermes-gateway",
                "gateway_state": "running",
                "pid": os.getpid(),
                "start_time": 123,
            }
        )
    )
    monkeypatch.setattr("gateway.status.get_runtime_status_running_pid", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="does not match a live Hermes gateway"):
        review._gateway_boot_identity()


def test_telegram_token_loads_from_hermes_home_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:test-token\n")

    assert review._telegram_token() == "123456:test-token"


@pytest.mark.asyncio
async def test_delivery_sends_real_buttons_once_and_records_message_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram

    class FakeButton:
        def __init__(self, text, callback_data):
            self.text = text
            self.callback_data = callback_data

        def to_dict(self):
            return {"text": self.text, "callback_data": self.callback_data}

    class FakeMarkup:
        def __init__(self, rows):
            self.inline_keyboard = rows

        def to_dict(self):
            return {"inline_keyboard": [[button.to_dict() for button in row] for row in self.inline_keyboard]}

    monkeypatch.setattr(telegram, "InlineKeyboardButton", FakeButton)
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", FakeMarkup)
    monkeypatch.setenv("HERMES_TASK_REVIEW_DELIVERY_STATE_PATH", str(tmp_path / "delivery.json"))
    monkeypatch.setenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", "171389200")
    monkeypatch.setenv("HERMES_TASK_REVIEW_STATE_PATH", str(tmp_path / "review.json"))
    monkeypatch.setattr(review, "_delivery_id", lambda reason, force=False: "daily:2026-08-15")
    monkeypatch.setattr(
        review,
        "snapshot",
        lambda: {
            "outcomes": [
                {
                    "key": "69b6e6d7f2",
                    "title": "Поправить обзор задач",
                    "items": [{"content": "Добавить кнопки", "status": "in_progress"}],
                }
            ]
        },
    )

    class FakeBot:
        def __init__(self):
            self.calls = []

        async def send_message(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(message_id=777)

    bot = FakeBot()
    first = await review.deliver_task_review("daily", bot=bot)
    second = await review.deliver_task_review("daily", bot=bot)

    assert first["sent"] == [{"route_key": "69b6e6d7f2", "message_id": 777}]
    assert second["sent"] == []
    assert len(bot.calls) == 1
    sent = bot.calls[0]
    assert sent["chat_id"] == 171389200
    assert "69b6e6d7f2" not in sent["text"]
    keyboard_payload = sent["reply_markup"].to_dict()["inline_keyboard"]
    labels = [button["text"] for button in keyboard_payload[0]]
    assert labels == ["▶️ Продолжить", "⏸ Отложить", "✅ Закрыть"]
    callback_parts = keyboard_payload[0][0]["callback_data"].split(":")
    assert callback_parts[:3] == ["ur", "c", "69b6e6d7f2"]
    assert len(callback_parts[3]) == 12
    state = json.loads((tmp_path / "delivery.json").read_text())
    assert state["deliveries"]["daily:2026-08-15"]["sent"]["69b6e6d7f2"]["message_id"] == 777


@pytest.mark.asyncio
async def test_ambiguous_telegram_send_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram

    monkeypatch.setattr(telegram, "InlineKeyboardButton", lambda text, callback_data: (text, callback_data))
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", lambda rows: rows)
    monkeypatch.setenv("HERMES_TASK_REVIEW_DELIVERY_STATE_PATH", str(tmp_path / "delivery.json"))
    monkeypatch.setenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", "171389200")
    monkeypatch.setenv("HERMES_TASK_REVIEW_STATE_PATH", str(tmp_path / "review.json"))
    monkeypatch.setattr(review, "_delivery_id", lambda reason, force=False: "daily:2026-08-15")
    monkeypatch.setattr(
        review,
        "snapshot",
        lambda: {
            "outcomes": [{"key": "69b6e6d7f2", "title": "Task", "items": []}]
        },
    )

    class AmbiguousBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, **_kwargs):
            self.calls += 1
            raise TimeoutError("Telegram acceptance unknown")

    bot = AmbiguousBot()
    with pytest.raises(TimeoutError):
        await review.deliver_task_review("daily", bot=bot)
    with pytest.raises(RuntimeError, match="automatic retry suppressed"):
        await review.deliver_task_review("daily", bot=bot)

    assert bot.calls == 1
    state = json.loads((tmp_path / "delivery.json").read_text())
    attempt = state["deliveries"]["daily:2026-08-15"]["attempts"]["69b6e6d7f2"]
    assert attempt["status"] == "ambiguous"


@pytest.mark.asyncio
async def test_definitive_telegram_rejection_remains_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import telegram
    from telegram.error import BadRequest

    monkeypatch.setattr(telegram, "InlineKeyboardButton", lambda text, callback_data: (text, callback_data))
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", lambda rows: rows)
    monkeypatch.setenv("HERMES_TASK_REVIEW_DELIVERY_STATE_PATH", str(tmp_path / "delivery.json"))
    monkeypatch.setenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", "171389200")
    monkeypatch.setenv("HERMES_TASK_REVIEW_STATE_PATH", str(tmp_path / "review.json"))
    monkeypatch.setattr(review, "_delivery_id", lambda reason, force=False: "daily:2026-08-15")
    monkeypatch.setattr(
        review,
        "snapshot",
        lambda: {"outcomes": [{"key": "69b6e6d7f2", "title": "Task", "items": []}]},
    )

    class RejectedBot:
        def __init__(self):
            self.calls = 0

        async def send_message(self, **_kwargs):
            self.calls += 1
            raise BadRequest("message is rejected")

    bot = RejectedBot()
    with pytest.raises(BadRequest):
        await review.deliver_task_review("daily", bot=bot)
    with pytest.raises(BadRequest):
        await review.deliver_task_review("daily", bot=bot)

    assert bot.calls == 2
    state = json.loads((tmp_path / "delivery.json").read_text())
    attempt = state["deliveries"]["daily:2026-08-15"]["attempts"]["69b6e6d7f2"]
    assert attempt["status"] == "failed"
