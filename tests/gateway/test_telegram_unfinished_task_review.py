from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from plugins.platforms.telegram.adapter import TelegramAdapter
from gateway.config import Platform


KEY = "69b6e6d7f2"
TOKEN = "abcdef012345"
OWNER = 171389200


def _adapter() -> TelegramAdapter:
    class Store:
        def __init__(self):
            self.switches = []
            self.current = {"telegram:dm:171389200": "current-session"}

        async def get_or_create_session(self, _source):
            session_key = "telegram:dm:171389200"
            return SimpleNamespace(
                session_key=session_key,
                session_id=self.current[session_key],
            )

        async def get(self, session_key):
            session_id = self.current.get(session_key)
            return (
                SimpleNamespace(session_key=session_key, session_id=session_id)
                if session_id
                else None
            )

        async def switch_session(
            self,
            session_key,
            target_session_id,
            *,
            expected_session_id=None,
        ):
            if (
                expected_session_id is not None
                and self.current.get(session_key) != expected_session_id
            ):
                return None
            self.switches.append((session_key, target_session_id))
            self.current[session_key] = target_session_id
            return SimpleNamespace(session_key=session_key, session_id=target_session_id)

    class Runner:
        def __init__(self):
            self.async_session_store = Store()
            self._release_running_agent_state = Mock()
            self._is_session_running = Mock(return_value=False)
            self._is_session_id_running = Mock(return_value=False)
            self._clear_conversation_scope = Mock()
            self._evict_cached_agent = Mock()
            self._recover_pending_task_review_actions = AsyncMock(return_value=0)
            self._schedule_resume_pending_sessions = Mock()

        async def message_handler(self, _event):
            return None

    adapter = object.__new__(TelegramAdapter)
    adapter.platform = Platform.TELEGRAM
    adapter.config = SimpleNamespace(extra={})
    runner = Runner()
    adapter._message_handler = runner.message_handler
    adapter._task_review_test_runner = runner
    adapter._group_approval_allows_message = lambda message: True
    adapter._active_sessions = {}

    def accepted(event, _session_key, *, interrupt_event=None):
        future = getattr(event, "_task_review_acceptance_future", None)
        if future is not None and not future.done():
            future.set_result(True)
        return True

    adapter._start_session_processing = Mock(side_effect=accepted)
    adapter.handle_message = AsyncMock()
    return adapter


def _install_handler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unfinished_task_review.py").write_text("# test placeholder\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", str(OWNER))


def _query(data: str, *, owner: bool = True):
    user_id = OWNER if owner else 999
    message = SimpleNamespace(
        chat_id=OWNER,
        chat=SimpleNamespace(type="private"),
        message_thread_id=None,
        message_id=456,
        text="📌 Незавершене завдання\n\nПоправить обзор задач",
    )
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=user_id, first_name="Andrew"),
        message=message,
        answer=AsyncMock(),
        edit_message_reply_markup=AsyncMock(),
    )


def _successful_process(*, applied: bool = True):
    payload = {
        "ok": True,
        "applied": applied,
        "key": KEY,
        "disposition": "open",
        "until": None,
        "outcome": {
            "title": "Поправить обзор задач",
            "session_id": "root-session",
            "session_link": "@session:default/root-session",
            "items": [
                {"id": "buttons", "content": "Добавить настоящие кнопки", "status": "in_progress"}
            ],
        },
    }

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps(payload).encode(), b""

    return Proc()


@pytest.mark.asyncio
async def test_continue_callback_is_owner_only_and_dispatches_agent_event(monkeypatch, tmp_path):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    seen_cmd = []

    async def fake_exec(*cmd, **_kwargs):
        seen_cmd.extend(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert "reserve" in seen_cmd
    assert "--disposition" not in seen_cmd
    assert seen_cmd[seen_cmd.index("--action-token") + 1] == TOKEN
    assert seen_cmd[seen_cmd.index("--session-key") + 1] == "telegram:dm:171389200"
    runner = adapter._task_review_test_runner
    assert runner.async_session_store.switches == [
        ("telegram:dm:171389200", "root-session")
    ]
    runner._clear_conversation_scope.assert_called_once_with(
        "telegram:dm:171389200", reason="task_review_continue"
    )
    runner._release_running_agent_state.assert_not_called()
    adapter._start_session_processing.assert_called_once()
    event = adapter._start_session_processing.call_args.args[0]
    assert event.source.chat_id == str(OWNER)
    assert event.source.user_id == str(OWNER)
    assert "Поправить обзор задач" in event.text
    assert "@session:default/root-session" in event.text
    assert KEY not in event.text
    query.answer.assert_awaited_once_with(text="▶️ Продовжую")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_continue_busy_route_releases_reservation_without_switching(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._is_session_running.return_value = True
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert commands == []
    assert runner.async_session_store.switches == []
    runner._release_running_agent_state.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.edit_message_reply_markup.assert_not_awaited()
    query.answer.assert_awaited_once_with(
        text="⏳ Поточна дія ще виконується; спробуйте ще раз"
    )


@pytest.mark.asyncio
async def test_continue_route_becoming_busy_after_reserve_releases_without_switch(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._is_session_running.side_effect = [False, True]
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert any("reserve" in cmd for cmd in commands)
    assert any("release" in cmd for cmd in commands)
    assert runner.async_session_store.switches == []
    runner._release_running_agent_state.assert_not_called()
    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_continue_releases_reservation_when_target_is_active_via_alias(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._is_session_id_running.return_value = True
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert any("reserve" in cmd for cmd in commands)
    assert any("release" in cmd for cmd in commands)
    assert runner.async_session_store.switches == []
    adapter._start_session_processing.assert_not_called()
    query.edit_message_reply_markup.assert_not_awaited()


@pytest.mark.asyncio
async def test_close_callback_is_deterministic_and_does_not_dispatch_agent(monkeypatch, tmp_path):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    query = _query(f"ur:x:{KEY}:{TOKEN}")
    seen_cmd = []

    async def fake_exec(*cmd, **_kwargs):
        seen_cmd.extend(cmd)
        proc = _successful_process()
        original = json.loads((await proc.communicate())[0])
        original["disposition"] = "closed"

        class CloseProc:
            returncode = 0

            async def communicate(self):
                return json.dumps(original).encode(), b""

        return CloseProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert seen_cmd[seen_cmd.index("--disposition") + 1] == "closed"
    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.answer.assert_awaited_once_with(text="✅ Закрито")
    query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)


@pytest.mark.asyncio
async def test_task_review_callback_rejects_non_owner_without_side_effect(monkeypatch):
    monkeypatch.setenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", str(OWNER))
    adapter = _adapter()
    query = _query(f"ur:d:{KEY}:{TOKEN}", owner=False)
    called = False

    async def fake_exec(*_cmd, **_kwargs):
        nonlocal called
        called = True
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert called is False
    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.answer.assert_awaited_once_with(text="⛔ Ця дія доступна лише власнику")


@pytest.mark.asyncio
async def test_task_review_callback_fails_closed_without_configured_owner(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_TASK_REVIEW_OWNER_CHAT_ID", raising=False)
    monkeypatch.delenv("TELEGRAM_HOME_CHANNEL", raising=False)
    adapter = _adapter()
    query = _query(f"ur:d:{KEY}:{TOKEN}")
    called = False

    async def fake_exec(*_cmd, **_kwargs):
        nonlocal called
        called = True
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert called is False
    query.answer.assert_awaited_once_with(text="❌ Власника огляду не налаштовано")


@pytest.mark.asyncio
async def test_replayed_task_review_callback_does_not_dispatch_twice(monkeypatch, tmp_path):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    query = _query(f"ur:c:{KEY}:{TOKEN}")

    async def fake_exec(*_cmd, **_kwargs):
        return _successful_process(applied=False)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.answer.assert_awaited_once_with(text="Цю дію вже виконано")


@pytest.mark.asyncio
async def test_continue_switch_failure_releases_reservation_and_keeps_keyboard(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    adapter._task_review_test_runner.async_session_store.switch_session = AsyncMock(return_value=None)
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._handle_callback_query(SimpleNamespace(callback_query=query), SimpleNamespace())

    assert any("reserve" in cmd for cmd in commands)
    assert any("release" in cmd for cmd in commands)
    adapter.handle_message.assert_not_awaited()
    adapter._start_session_processing.assert_not_called()
    query.edit_message_reply_markup.assert_not_awaited()
    query.answer.assert_awaited_once_with(text="❌ Не вдалося запустити продовження")


@pytest.mark.asyncio
async def test_continue_dispatch_rejection_rolls_route_back(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    adapter._start_session_processing.side_effect = None
    adapter._start_session_processing.return_value = False
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    assert runner.async_session_store.switches == [
        ("telegram:dm:171389200", "root-session"),
        ("telegram:dm:171389200", "current-session"),
    ]
    assert any("release" in cmd for cmd in commands)
    query.answer.assert_awaited_once_with(text="❌ Не вдалося запустити продовження")


@pytest.mark.asyncio
async def test_continue_rollback_does_not_overwrite_newer_route(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner

    def rebind_and_reject(_event, session_key, *, interrupt_event=None):
        runner.async_session_store.current[session_key] = "newer-session"
        return False

    adapter._start_session_processing.side_effect = rebind_and_reject
    query = _query(f"ur:c:{KEY}:{TOKEN}")

    async def fake_exec(*_cmd, **_kwargs):
        return _successful_process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    session_key = "telegram:dm:171389200"
    assert runner.async_session_store.current[session_key] == "newer-session"
    assert runner.async_session_store.switches == [
        (session_key, "root-session")
    ]
    query.answer.assert_awaited_once_with(text="❌ Не вдалося запустити продовження")


@pytest.mark.asyncio
async def test_continue_reserve_nonzero_reconciles_pending_row(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._recover_pending_task_review_actions.return_value = 1
    query = _query(f"ur:c:{KEY}:{TOKEN}")

    class FailedProc:
        returncode = 1

        async def communicate(self):
            return b"", b"child failed after durable reserve"

    async def fake_exec(*_cmd, **_kwargs):
        return FailedProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    runner._recover_pending_task_review_actions.assert_awaited_once_with(
        only_action_token=TOKEN
    )
    query.answer.assert_awaited_once_with(text="⏳ Запуск ще підтверджується")


@pytest.mark.asyncio
async def test_continue_reserve_nonzero_pending_status_is_not_reported_failed(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._recover_pending_task_review_actions.return_value = 0
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    class Proc:
        def __init__(self, returncode, stdout=b"", stderr=b""):
            self.returncode = returncode
            self._stdout = stdout
            self._stderr = stderr

        async def communicate(self):
            return self._stdout, self._stderr

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        if "status" in cmd:
            return Proc(
                0,
                json.dumps({"ok": True, "status": "pending"}).encode(),
            )
        return Proc(1, stderr=b"child failed after durable reserve")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    runner._recover_pending_task_review_actions.assert_awaited_once_with(
        only_action_token=TOKEN
    )
    assert any("status" in cmd for cmd in commands)
    query.answer.assert_awaited_once_with(text="⏳ Запуск ще підтверджується")


@pytest.mark.asyncio
async def test_continue_reserve_timeout_triggers_live_outbox_recovery(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._recover_pending_task_review_actions.return_value = 1
    query = _query(f"ur:c:{KEY}:{TOKEN}")

    class TimeoutProc:
        returncode = None

        def __init__(self):
            self.calls = 0
            self.kill = Mock()

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError
            return b"", b""

    async def fake_exec(*_cmd, **_kwargs):
        return TimeoutProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    runner._recover_pending_task_review_actions.assert_awaited_once_with(
        only_action_token=TOKEN
    )
    runner._schedule_resume_pending_sessions.assert_not_called()
    query.answer.assert_awaited_once_with(text="⏳ Запуск ще підтверджується")


@pytest.mark.asyncio
async def test_continue_reserve_timeout_keeps_ownership_when_status_unreadable(
    monkeypatch, tmp_path
):
    _install_handler(tmp_path, monkeypatch)
    adapter = _adapter()
    runner = adapter._task_review_test_runner
    runner._recover_pending_task_review_actions.return_value = 0
    query = _query(f"ur:c:{KEY}:{TOKEN}")
    commands = []

    class TimeoutProc:
        returncode = None

        def __init__(self):
            self.calls = 0

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            self.calls += 1
            if self.calls == 1:
                raise asyncio.TimeoutError
            return b"", b""

    class SuccessProc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"ok": True, "released": True}).encode(), b""

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return SuccessProc() if "release" in cmd else TimeoutProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    await adapter._handle_callback_query(
        SimpleNamespace(callback_query=query), SimpleNamespace()
    )

    runner._recover_pending_task_review_actions.assert_awaited_once_with(
        only_action_token=TOKEN
    )
    assert not any("release" in cmd for cmd in commands)
    query.answer.assert_awaited_once_with(text="⏳ Запуск ще підтверджується")


@pytest.mark.asyncio
async def test_runner_commits_reserved_action_and_resolves_acceptance(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    commands = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"ok": True, "applied": True}).encode(), b""

    async def fake_exec(*cmd, **_kwargs):
        commands.append(cmd)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    assert await runner._commit_unfinished_task_review_action(event) is True
    assert event._task_review_committed is True
    assert acceptance.result() is True
    assert any("action" in cmd and "--disposition" in cmd and "open" in cmd for cmd in commands)


@pytest.mark.asyncio
async def test_runner_commit_timeout_kills_child_and_reconciles_committed_status(
    monkeypatch, tmp_path
):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "unfinished_task_review_state.json").write_text(
        json.dumps(
            {
                "outcomes": {},
                "actions": {
                    TOKEN: {"key": KEY, "disposition": "open"},
                },
            }
        )
    )
    timed_out = SimpleNamespace(killed=False, calls=0, returncode=None)

    class TimeoutProc:
        @property
        def returncode(self):
            return timed_out.returncode

        def kill(self):
            timed_out.killed = True
            timed_out.returncode = 0
            raise ProcessLookupError

        async def communicate(self):
            timed_out.calls += 1
            if timed_out.calls == 1:
                raise asyncio.TimeoutError
            return b"", b""

    async def fake_exec(*_cmd, **_kwargs):
        return TimeoutProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    assert await runner._commit_unfinished_task_review_action(event) is True
    assert timed_out.killed is True
    assert timed_out.calls == 2
    assert event._task_review_committed is True
    assert acceptance.result() is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [(1, b""), (0, b"{not-json")],
)
async def test_runner_non_timeout_commit_failure_reconciles_durable_open(
    monkeypatch, tmp_path, returncode, stdout
):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "unfinished_task_review_state.json").write_text(
        json.dumps(
            {
                "actions": {
                    TOKEN: {"key": KEY, "disposition": "open"},
                },
                "pending_actions": {},
            }
        )
    )

    class Proc:
        async def communicate(self):
            return stdout, b"child failed after write"

    proc = Proc()
    proc.returncode = returncode

    async def fake_exec(*_cmd, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    assert await runner._commit_unfinished_task_review_action(event) is True
    assert event._task_review_committed is True
    assert acceptance.result() is True


@pytest.mark.asyncio
async def test_runner_commit_timeout_status_failure_is_marked_ambiguous(
    monkeypatch, tmp_path
):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class TimeoutProc:
        returncode = None

        def kill(self):
            self.returncode = -9

        async def communicate(self):
            if self.returncode is None:
                raise asyncio.TimeoutError
            return b"", b""

    async def fake_exec(*_cmd, **_kwargs):
        return TimeoutProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    runner._unfinished_task_review_action_status = AsyncMock(
        side_effect=OSError("status unavailable")
    )
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    with pytest.raises(RuntimeError, match="durable status could not be read"):
        await runner._commit_unfinished_task_review_action(event)
    assert event._task_review_commit_ambiguous is True
    assert isinstance(acceptance.exception(), RuntimeError)


@pytest.mark.asyncio
async def test_runner_does_not_continue_after_close_or_defer_wins_race(
    monkeypatch, tmp_path
):
    from gateway.run import GatewayRunner

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "unfinished_task_review_state.json").write_text(
        json.dumps(
            {
                "outcomes": {},
                "actions": {
                    TOKEN: {"key": KEY, "disposition": "closed"},
                },
            }
        )
    )

    class Proc:
        returncode = 0

        def __init__(self, payload):
            self.payload = payload

        async def communicate(self):
            return json.dumps(self.payload).encode(), b""

    async def fake_exec(*cmd, **_kwargs):
        if "status" in cmd:
            return Proc({"ok": True, "status": "committed_other"})
        return Proc({"ok": True, "applied": False})

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    with pytest.raises(RuntimeError, match="durable status committed_other"):
        await runner._commit_unfinished_task_review_action(event)
    assert isinstance(acceptance.exception(), RuntimeError)
    assert getattr(event, "_task_review_committed", False) is False


@pytest.mark.asyncio
async def test_runner_releases_any_uncommitted_task_review_on_early_rejection():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._release_unfinished_task_review_reservation = AsyncMock(return_value=True)
    acceptance = asyncio.get_running_loop().create_future()
    event = SimpleNamespace(
        _task_review_key=KEY,
        _task_review_action_token=TOKEN,
        _task_review_acceptance_future=acceptance,
    )

    assert await runner._reject_uncommitted_task_review(event, "early rejection") is True
    runner._release_unfinished_task_review_reservation.assert_awaited_once_with(event)
    assert isinstance(acceptance.exception(), RuntimeError)

    committed = SimpleNamespace(
        _task_review_action_token=TOKEN,
        _task_review_committed=True,
    )
    assert await runner._reject_uncommitted_task_review(committed, "ignored") is False
    runner._release_unfinished_task_review_reservation.assert_awaited_once_with(event)


@pytest.mark.asyncio
async def test_committed_task_review_failure_keeps_resume_marker():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = Mock()
    store = SimpleNamespace(
        _store=runner.session_store,
        mark_resume_pending=AsyncMock(return_value=True),
        clear_resume_pending=AsyncMock(return_value=True),
    )
    runner._async_session_store = store
    event = SimpleNamespace(
        _task_review_action_token=TOKEN,
        _task_review_committed=True,
    )

    assert await runner._arm_task_review_resume_marker(event, "telegram:dm:171389200") is True
    assert await runner._finalize_task_review_resume_marker(event, succeeded=False) is False
    store.mark_resume_pending.assert_awaited_once_with(
        "telegram:dm:171389200", f"task_review_continue:{TOKEN}"
    )
    store.clear_resume_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_uncommitted_task_review_failure_clears_resume_marker():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = Mock()
    store = SimpleNamespace(
        _store=runner.session_store,
        mark_resume_pending=AsyncMock(return_value=True),
        clear_resume_pending=AsyncMock(return_value=True),
    )
    runner._async_session_store = store
    event = SimpleNamespace(_task_review_action_token=TOKEN)

    assert await runner._arm_task_review_resume_marker(event, "telegram:dm:171389200") is True
    assert await runner._finalize_task_review_resume_marker(event, succeeded=False) is True
    store.clear_resume_pending.assert_awaited_once_with("telegram:dm:171389200")


@pytest.mark.asyncio
async def test_successful_task_review_turn_clears_resume_marker():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = Mock()
    store = SimpleNamespace(
        _store=runner.session_store,
        mark_resume_pending=AsyncMock(return_value=True),
        clear_resume_pending=AsyncMock(return_value=True),
    )
    runner._async_session_store = store
    event = SimpleNamespace(
        _task_review_action_token=TOKEN,
        _task_review_committed=True,
    )

    assert await runner._arm_task_review_resume_marker(
        event, "telegram:dm:171389200"
    ) is True
    assert await runner._finalize_task_review_resume_marker(
        event, succeeded=True
    ) is True
    store.clear_resume_pending.assert_awaited_once_with("telegram:dm:171389200")


@pytest.mark.asyncio
async def test_ambiguous_commit_failure_keeps_resume_marker():
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.session_store = Mock()
    store = SimpleNamespace(
        _store=runner.session_store,
        mark_resume_pending=AsyncMock(return_value=True),
        clear_resume_pending=AsyncMock(return_value=True),
    )
    runner._async_session_store = store
    event = SimpleNamespace(
        _task_review_action_token=TOKEN,
        _task_review_commit_ambiguous=True,
    )

    assert await runner._arm_task_review_resume_marker(
        event, "telegram:dm:171389200"
    ) is True
    assert await runner._finalize_task_review_resume_marker(
        event, succeeded=False
    ) is False
    store.clear_resume_pending.assert_not_awaited()


@pytest.mark.asyncio
async def test_runner_startup_reconciles_pending_action(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unfinished_task_review.py").write_text("# test placeholder\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    seen = []

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps({"ok": True, "accepted": [TOKEN]}).encode(), b""

    async def fake_exec(*cmd, **_kwargs):
        seen.extend(cmd)
        return Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    assert await runner._accept_pending_task_review_for_session(
        "root-session", "telegram:dm:171389200", TOKEN
    ) is True
    assert "accept-pending" in seen
    assert seen[seen.index("--session-id") + 1] == "root-session"
    assert seen[seen.index("--session-key") + 1] == "telegram:dm:171389200"
    assert seen[seen.index("--action-token") + 1] == TOKEN


@pytest.mark.asyncio
async def test_runner_recovers_pending_outbox_into_resume_pending(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner

    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "unfinished_task_review.py").write_text("# test placeholder\n")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    class Proc:
        returncode = 0

        async def communicate(self):
            return json.dumps(
                {
                    "ok": True,
                    "pending": [
                        {
                            "action_token": TOKEN,
                            "key": KEY,
                            "session_id": "root-session",
                            "session_key": "telegram:dm:171389200",
                        }
                    ],
                }
            ).encode(), b""

    async def fake_exec(*_cmd, **_kwargs):
        return Proc()

    class Store:
        def __init__(self):
            self.switches = []
            self.marks = []
            self.current_session_id = "current-session"

        def lookup_by_session_key(self, session_key):
            if session_key != "telegram:dm:171389200":
                return None
            return SimpleNamespace(
                session_key=session_key,
                session_id=self.current_session_id,
            )

        def switch_session(
            self,
            session_key,
            session_id,
            *,
            expected_session_id=None,
        ):
            self.switches.append(
                (session_key, session_id, expected_session_id)
            )
            if self.current_session_id != expected_session_id:
                return None
            self.current_session_id = session_id
            return SimpleNamespace(
                session_key=session_key,
                session_id=session_id,
                origin=SimpleNamespace(
                    platform="desktop",
                    chat_id="desktop-session",
                    chat_type="dm",
                    user_id="local-user",
                    user_name="Local",
                ),
            )

        def mark_resume_pending(self, session_key, reason):
            self.marks.append((session_key, reason))
            return True

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    runner = object.__new__(GatewayRunner)
    runner.session_store = Store()
    runner._is_session_running = Mock(return_value=False)
    runner._is_session_id_running = Mock(return_value=False)
    route_source = SimpleNamespace(
        platform=Platform.TELEGRAM,
        chat_id=str(OWNER),
        chat_type="dm",
        user_id=str(OWNER),
        user_name="Owner",
    )
    runner._session_source_for_key = Mock(return_value=route_source)
    runner._clear_conversation_scope = Mock()
    runner._evict_cached_agent = Mock()

    class RecoveryAdapter:
        def __init__(self):
            self._active_sessions = {}
            self.started = []

        def _start_session_processing(
            self, event, session_key, *, interrupt_event=None
        ):
            self.started.append((event, session_key, interrupt_event))
            return True

        def _release_session_guard(self, session_key, *, guard=None):
            if self._active_sessions.get(session_key) is guard:
                self._active_sessions.pop(session_key, None)

    adapter = RecoveryAdapter()
    runner.adapters = {Platform.TELEGRAM: adapter}

    assert await runner._recover_pending_task_review_actions(
        only_action_token="000000000000"
    ) == 0
    assert runner.async_session_store.switches == []

    recovered = await runner._recover_pending_task_review_actions(
        only_action_token=TOKEN
    )

    assert recovered == 1
    assert runner.async_session_store.switches == [
        (
            "telegram:dm:171389200",
            "root-session",
            "current-session",
        )
    ]
    assert runner.async_session_store.marks == [
        ("telegram:dm:171389200", f"task_review_continue:{TOKEN}")
    ]
    runner._clear_conversation_scope.assert_called_once_with(
        "telegram:dm:171389200", reason="task_review_continue"
    )
    runner._evict_cached_agent.assert_called_once_with("telegram:dm:171389200")
    assert len(adapter.started) == 1
    event, started_key, guard = adapter.started[0]
    assert started_key == "telegram:dm:171389200"
    assert guard is adapter._active_sessions[started_key]
    assert event._task_review_key == KEY
    assert event._task_review_action_token == TOKEN
    assert event.source is route_source

    # A second reconciliation sees the adapter guard from the first synthetic
    # turn and must not switch or dispatch the same route again.
    assert await runner._recover_pending_task_review_actions() == 0
    assert runner.async_session_store.switches == [
        (
            "telegram:dm:171389200",
            "root-session",
            "current-session",
        )
    ]
    assert len(adapter.started) == 1
