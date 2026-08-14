#!/usr/bin/env python3
"""Collect and disposition unfinished Hermes outcomes.

Read-only snapshots come from the latest structured todo tool result in
owner-visible root sessions.  A compacted transcript can retain the full JSON
row as inactive while replacing the active display row with ``[todo] updated``;
both carry the same tool-call id, so structured inactive rows remain canonical
hydration evidence.  Related todo items are grouped by the result in which they
first appeared, mapping a multi-step plan back to one accepted outcome.  Review
dispositions live in a separate sidecar; transcripts are never rewritten.
"""
from __future__ import annotations

import argparse
import asyncio

import hashlib
import json
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from zoneinfo import ZoneInfo

_msvcrt = None
try:
    import fcntl as _fcntl
except ImportError:  # Windows
    _fcntl = None
    import msvcrt as _msvcrt

OPEN_STATUSES = {"pending", "in_progress"}
REVIEW_TZ = ZoneInfo(os.environ.get("HERMES_TASK_REVIEW_TIMEZONE", "Europe/Kyiv"))


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME", "~/.hermes")).expanduser().resolve()


def _owner_telegram_chat_id() -> int:
    raw = (
        os.environ.get("HERMES_TASK_REVIEW_OWNER_CHAT_ID", "").strip()
        or os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    )
    if not raw:
        try:
            from dotenv import dotenv_values

            values = dotenv_values(hermes_home() / ".env")
            raw = str(
                values.get("HERMES_TASK_REVIEW_OWNER_CHAT_ID")
                or values.get("TELEGRAM_HOME_CHANNEL")
                or ""
            ).strip()
        except (ImportError, OSError):
            raw = ""
    if not re.fullmatch(r"[1-9][0-9]*", raw):
        raise RuntimeError("unfinished-task review owner chat is not configured")
    return int(raw)


def state_path() -> Path:
    override = os.environ.get("HERMES_TASK_REVIEW_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return hermes_home() / "unfinished_task_review_state.json"


def delivery_state_path() -> Path:
    override = os.environ.get("HERMES_TASK_REVIEW_DELIVERY_STATE_PATH", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return hermes_home() / "unfinished_task_review_delivery.json"


def _read_state() -> dict[str, Any]:
    path = state_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("outcomes", {}), dict):
            return data
    except (OSError, ValueError, TypeError):
        pass
    return {"version": 1, "outcomes": {}}


def _write_state(data: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


@contextmanager
def _exclusive_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        os.chmod(lock_path, 0o600)
        if _fcntl is not None:
            _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_EX)
        else:
            assert _msvcrt is not None
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_LOCK, 1)
        try:
            yield
        finally:
            if _fcntl is not None:
                _fcntl.flock(lock_file.fileno(), _fcntl.LOCK_UN)
            else:
                assert _msvcrt is not None
                lock_file.seek(0)
                _msvcrt.locking(lock_file.fileno(), _msvcrt.LK_UNLCK, 1)


@contextmanager
def _state_lock() -> Iterator[None]:
    lock_path = state_path().with_suffix(state_path().suffix + ".lock")
    with _exclusive_lock(lock_path):
        yield


def _mutate_state(mutator: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    """Serialize sidecar read-modify-replace across cron/reply processes."""
    with _state_lock():
        data = _read_state()
        mutator(data)
        _write_state(data)
        return data


def _parse_todos(raw: str | None) -> list[dict[str, str]] | None:
    try:
        data = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return None
    todos = data.get("todos") if isinstance(data, dict) else None
    if not isinstance(todos, list):
        return None
    result: list[dict[str, str]] = []
    for item in todos:
        if not isinstance(item, dict):
            continue
        item_id = str(item.get("id") or "").strip()
        content = str(item.get("content") or "").strip()
        status = str(item.get("status") or "").strip()
        if item_id and content and status:
            result.append({"id": item_id, "content": content, "status": status})
    return result


def _outcome_key(session_id: str, first_message_id: int) -> str:
    raw = f"{session_id}:{first_message_id}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:10]


def _nearest_user_prompt(con: sqlite3.Connection, session_id: str, before_id: int) -> str:
    row = con.execute(
        """SELECT content FROM messages
           WHERE session_id=? AND role='user' AND id < ?
             AND COALESCE(display_kind, '') NOT IN ('context_compaction', 'system')
           ORDER BY id DESC LIMIT 1""",
        (session_id, before_id),
    ).fetchone()
    text = str(row[0] or "").strip() if row else ""
    text = " ".join(text.split())
    if len(text) > 280:
        text = text[:277].rstrip() + "…"
    return text


def _visible_session(row: sqlite3.Row) -> bool:
    source = str(row["source"] or "")
    if source == "desktop":
        return True
    return (
        source == "telegram"
        and str(row["chat_type"] or "") == "dm"
        and str(row["chat_id"] or "") == str(_owner_telegram_chat_id())
    )


def _deferred_is_active(record: dict[str, Any], now: datetime) -> bool:
    if record.get("disposition") != "deferred":
        return False
    try:
        return datetime.fromisoformat(str(record.get("until"))) > now
    except (ValueError, TypeError):
        return False


def _parse_until(value: str) -> datetime:
    try:
        if len(value) == 10:
            return datetime.combine(date.fromisoformat(value), time(hour=10), tzinfo=REVIEW_TZ)
        due = datetime.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"invalid --until ISO date/datetime: {value}") from exc
    if due.tzinfo is None:
        return due.replace(tzinfo=REVIEW_TZ)
    return due.astimezone(REVIEW_TZ)


def _next_daily_review(now: datetime | None = None) -> datetime:
    """Return the next 10:00 Europe/Kyiv boundary strictly after ``now``."""
    current = (now or datetime.now(REVIEW_TZ)).astimezone(REVIEW_TZ)
    candidate = datetime.combine(current.date(), time(hour=10), tzinfo=REVIEW_TZ)
    if candidate <= current:
        candidate = datetime.combine(current.date() + timedelta(days=1), time(hour=10), tzinfo=REVIEW_TZ)
    return candidate


def render_task_cards(
    review_snapshot: dict[str, Any],
    action_tokens: dict[str, str],
) -> list[dict[str, Any]]:
    """Render one Telegram card per outcome; routing keys stay callback-only."""
    cards: list[dict[str, Any]] = []
    outcomes = review_snapshot.get("outcomes", [])
    for outcome in outcomes if isinstance(outcomes, list) else []:
        if not isinstance(outcome, dict):
            continue
        key = str(outcome.get("key") or "").strip()
        title = " ".join(str(outcome.get("title") or "Незавершене завдання").split())
        if not key or len(key.encode("utf-8")) > 48:
            continue
        token = str(action_tokens.get(key) or "").strip()
        if not re.fullmatch(r"[0-9a-f]{12}", token):
            continue
        suffix = f":{token}"
        lines = ["📌 Незавершене завдання", "", title]
        items = outcome.get("items", [])
        if isinstance(items, list):
            visible_items = [
                " ".join(str(item.get("content") or "").split())
                for item in items
                if isinstance(item, dict) and str(item.get("content") or "").strip()
            ]
            if visible_items:
                lines.extend(["", *[f"• {content}" for content in visible_items[:5]]])
        cards.append(
            {
                "route_key": key,
                "text": "\n".join(lines),
                "buttons": [
                    {"text": "▶️ Продолжить", "callback_data": f"ur:c:{key}{suffix}"},
                    {"text": "⏸ Отложить", "callback_data": f"ur:d:{key}{suffix}"},
                    {"text": "✅ Закрыть", "callback_data": f"ur:x:{key}{suffix}"},
                ],
            }
        )
    return cards


def _gateway_boot_identity() -> str:
    path = hermes_home() / "gateway_state.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        pid = int(data["pid"])
        start_time = int(data["start_time"])
        if data.get("gateway_state") != "running" or data.get("kind") != "hermes-gateway":
            raise ValueError("gateway is not running")
        from gateway.status import get_runtime_status_running_pid

        validated_pid = get_runtime_status_running_pid(data, expected_home=hermes_home())
        if validated_pid != pid:
            raise ValueError("persisted PID does not match a live Hermes gateway")
    except (ImportError, KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(f"gateway boot identity unavailable: {exc}") from exc
    return f"{start_time}:{pid}"


def _delivery_id(reason: str, *, force: bool = False) -> str:
    now = datetime.now(REVIEW_TZ)
    if reason == "daily":
        base = f"daily:{now.date().isoformat()}"
    elif reason == "restart":
        base = f"restart:{_gateway_boot_identity()}"
    else:
        base = f"manual:{now.isoformat()}"
    return f"{base}:force:{now.timestamp()}" if force else base


def _read_json_state(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else default
    except (OSError, TypeError, ValueError):
        return default


def _write_json_state(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _review_action_token(key: str, delivery_id: str) -> str:
    """Reuse one token until claimed; rotate on the next delivery after a claim."""
    selected = ""

    def mutate(data: dict[str, Any]) -> None:
        nonlocal selected
        tokens = data.setdefault("review_tokens", {})
        actions = data.setdefault("actions", {})
        dispositions = data.get("outcomes", {})
        disposition = dispositions.get(key, {}) if isinstance(dispositions, dict) else {}
        if disposition.get("disposition") == "closed" or _deferred_is_active(
            disposition, datetime.now(REVIEW_TZ)
        ):
            selected = ""
            return
        existing = tokens.get(key) if isinstance(tokens, dict) else None
        existing_token = str(existing.get("token") or "") if isinstance(existing, dict) else ""
        existing_delivery = str(existing.get("delivery_id") or "") if isinstance(existing, dict) else ""
        if existing_token and (existing_token not in actions or existing_delivery == delivery_id):
            selected = existing_token
            return
        selected = hashlib.sha256(f"{key}:{delivery_id}".encode("utf-8")).hexdigest()[:12]
        tokens[key] = {
            "token": selected,
            "delivery_id": delivery_id,
            "created_at": datetime.now(REVIEW_TZ).isoformat(),
        }

    _mutate_state(mutate)
    return selected


def _telegram_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        try:
            from dotenv import dotenv_values

            token = str(dotenv_values(hermes_home() / ".env").get("TELEGRAM_BOT_TOKEN") or "").strip()
        except (ImportError, OSError):
            token = ""
    if not token:
        from gateway.config import Platform, load_gateway_config

        platform_config = load_gateway_config().platforms.get(Platform.TELEGRAM)
        token = str(getattr(platform_config, "token", "") or "").strip()
    return token


async def deliver_task_review(
    reason: str,
    *,
    force: bool = False,
    bot: Any = None,
) -> dict[str, Any]:
    """Send one owner-DM Telegram card per outcome, with durable dedupe."""
    if reason not in {"daily", "restart", "manual"}:
        raise ValueError(f"unsupported delivery reason: {reason}")
    delivery_id = _delivery_id(reason, force=force)
    review_snapshot = snapshot()
    raw_outcomes = review_snapshot.get("outcomes", [])
    action_tokens = {
        str(outcome["key"]): _review_action_token(str(outcome["key"]), delivery_id)
        for outcome in raw_outcomes if isinstance(raw_outcomes, list) and isinstance(outcome, dict) and outcome.get("key")
    }
    cards = render_task_cards(review_snapshot, action_tokens)
    if not cards:
        cards = [{"route_key": "_empty", "text": "✅ Незавершених задач немає.", "buttons": []}]

    if bot is None:
        token = _telegram_token()
        if not token:
            raise RuntimeError("Telegram bot token is not configured")
        from telegram import Bot

        bot = Bot(token=token)

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    state_file = delivery_state_path()
    lock_file = state_file.with_suffix(state_file.suffix + ".lock")
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    sent_now: list[dict[str, Any]] = []
    with _exclusive_lock(lock_file):
        state = _read_json_state(state_file, {"version": 1, "deliveries": {}})
        deliveries = state.setdefault("deliveries", {})
        record = deliveries.setdefault(delivery_id, {"reason": reason, "sent": {}})
        sent = record.setdefault("sent", {})
        attempts = record.setdefault("attempts", {})
        for card in cards:
            route_key = str(card["route_key"])
            if route_key in sent:
                continue
            prior_attempt = attempts.get(route_key)
            if isinstance(prior_attempt, dict) and prior_attempt.get("status") in {"pending", "ambiguous"}:
                raise RuntimeError(
                    f"ambiguous prior Telegram delivery for {route_key}; automatic retry suppressed"
                )
            attempts[route_key] = {
                "status": "pending",
                "started_at": datetime.now(REVIEW_TZ).isoformat(),
            }
            record["updated_at"] = datetime.now(REVIEW_TZ).isoformat()
            _write_json_state(state_file, state)
            buttons = card.get("buttons", [])
            reply_markup = None
            if buttons:
                reply_markup = InlineKeyboardMarkup(
                    [[InlineKeyboardButton(button["text"], callback_data=button["callback_data"]) for button in buttons]]
                )
            try:
                msg = await bot.send_message(
                    chat_id=_owner_telegram_chat_id(),
                    text=str(card["text"]),
                    reply_markup=reply_markup,
                    disable_notification=datetime.now(REVIEW_TZ).hour >= 21,
                )
            except BaseException as exc:
                try:
                    from telegram.error import BadRequest, Forbidden

                    definitive = isinstance(exc, (BadRequest, Forbidden))
                except ImportError:
                    definitive = False
                attempts[route_key] = {
                    **attempts[route_key],
                    "status": "failed" if definitive else "ambiguous",
                    "error_type": type(exc).__name__,
                    "updated_at": datetime.now(REVIEW_TZ).isoformat(),
                }
                _write_json_state(state_file, state)
                raise
            message_id = int(getattr(msg, "message_id"))
            attempts[route_key] = {
                **attempts[route_key],
                "status": "sent",
                "message_id": message_id,
                "updated_at": datetime.now(REVIEW_TZ).isoformat(),
            }
            sent[route_key] = {"message_id": message_id, "sent_at": datetime.now(REVIEW_TZ).isoformat()}
            record["updated_at"] = datetime.now(REVIEW_TZ).isoformat()
            _write_json_state(state_file, state)
            sent_now.append({"route_key": route_key, "message_id": message_id})
        record["completed_at"] = datetime.now(REVIEW_TZ).isoformat()
        _write_json_state(state_file, state)
    return {"ok": True, "delivery_id": delivery_id, "sent": sent_now}


def snapshot(*, include_hidden: bool = False) -> dict[str, Any]:
    db_path = hermes_home() / "state.db"
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    all_session_rows = con.execute(
        """SELECT sessions.id, sessions.source, sessions.chat_id,
                  sessions.chat_type, sessions.title, sessions.profile_name,
                  sessions.archived, sessions.parent_session_id,
                  sessions.session_key, sessions.started_at, sessions.ended_at,
                  sessions.end_reason,
                  sessions.last_activity_at, sessions.model_config,
                  (SELECT MAX(messages.timestamp) FROM messages
                   WHERE messages.session_id = sessions.id) AS latest_message_at
           FROM sessions"""
    ).fetchall()
    sessions_by_id = {str(row["id"]): row for row in all_session_rows}
    children: dict[str, list[sqlite3.Row]] = {}
    for row in all_session_rows:
        parent_id = row["parent_session_id"]
        if parent_id:
            children.setdefault(str(parent_id), []).append(row)

    def model_config(row: sqlite3.Row) -> dict[str, Any]:
        try:
            config = json.loads(str(row["model_config"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return config if isinstance(config, dict) else {}

    def child_is_implicit_continuation(row: sqlite3.Row) -> bool:
        config = model_config(row)
        return (
            config.get("_branched_from") is None
            and config.get("_delegate_from") is None
            and str(row["source"] or "") != "tool"
        )

    def activity_value(value: Any) -> float:
        if value is None or value == "":
            return float("-inf")
        if isinstance(value, (int, float)):
            return float(value)
        text = str(value)
        try:
            return float(text)
        except ValueError:
            try:
                return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
            except ValueError:
                return float("-inf")

    def logical_root(row: sqlite3.Row) -> bool:
        if row["parent_session_id"] is None:
            return True
        config = model_config(row)
        if config.get("_branched_from") is not None or config.get("_reset_from") is not None:
            return True
        parent = sessions_by_id.get(str(row["parent_session_id"]))
        if parent is None:
            return False
        if (
            str(parent["end_reason"] or "") == "branched"
            and row["started_at"] not in (None, "")
            and parent["ended_at"] not in (None, "")
            and activity_value(row["started_at"]) >= activity_value(parent["ended_at"])
        ):
            return True
        return (
            str(parent["end_reason"] or "")
            in {"session_reset", "session_switch", "idle", "daily", "suspended", "resume_pending_expired"}
            and bool(str(row["session_key"] or ""))
            and str(row["session_key"]) == str(parent["session_key"] or "")
        )

    def continuation_rank(row: sqlite3.Row) -> tuple[int, float, float, str]:
        lifecycle_rank = (
            2 if str(row["end_reason"] or "") == "compression"
            else 1 if row["ended_at"] is None
            else 0
        )
        last_active = max(
            activity_value(row["last_activity_at"]),
            activity_value(row["latest_message_at"]),
            activity_value(row["started_at"]),
        )
        return lifecycle_rank, last_active, activity_value(row["started_at"]), str(row["id"])

    lineages: dict[str, dict[str, Any]] = {}
    member_to_root: dict[str, str] = {}
    for root in all_session_rows:
        if not logical_root(root) or not _visible_session(root):
            continue
        members = [root]
        current = root
        seen = {str(root["id"])}
        while str(current["end_reason"] or "") == "compression":
            candidates = [
                child for child in children.get(str(current["id"]), [])
                if str(child["id"]) not in seen and child_is_implicit_continuation(child)
            ]
            if not candidates:
                break
            current = max(candidates, key=continuation_rank)
            seen.add(str(current["id"]))
            members.append(current)
        tip = members[-1]
        if bool(tip["archived"]):
            continue
        root_id = str(root["id"])
        lineages[root_id] = {"root": root, "tip": tip, "members": members}
        for member in members:
            member_to_root[str(member["id"])] = root_id

    if not lineages:
        return {"version": 1, "generated_at": datetime.now(REVIEW_TZ).isoformat(), "outcomes": []}

    member_ids = tuple(member_to_root)
    placeholders = ",".join("?" for _ in member_ids)
    rows = con.execute(
        f"""SELECT id, session_id, content, timestamp
             FROM messages
             WHERE role='tool' AND tool_name='todo'
               AND session_id IN ({placeholders})
             ORDER BY id""",
        member_ids,
    ).fetchall()

    histories: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        root_id = member_to_root.get(str(row["session_id"]))
        if root_id:
            histories.setdefault(root_id, []).append(row)

    review_state = _read_state().get("outcomes", {})
    now = datetime.now(REVIEW_TZ)
    outcomes: list[dict[str, Any]] = []

    for root_session_id, todo_rows in histories.items():
        first_seen: dict[str, tuple[int, str]] = {}
        latest_by_id: dict[str, dict[str, str]] = {}
        latest_timestamp = 0.0
        previous_by_id: dict[str, dict[str, str]] = {}

        for row in todo_rows:
            parsed = _parse_todos(row["content"])
            if parsed is None:
                continue
            current_by_id = {item["id"]: item for item in parsed}
            latest_by_id = current_by_id
            latest_timestamp = float(row["timestamp"] or 0.0)
            for item in parsed:
                previous = previous_by_id.get(item["id"])
                # A full todo result is a complete snapshot. An ID starts a new
                # generation when it was absent from the previous snapshot OR
                # when merge=false directly reuses the same generic ID for a
                # different accepted outcome.
                if previous is None or previous["content"] != item["content"]:
                    first_seen[item["id"]] = (int(row["id"]), str(row["session_id"]))
            previous_by_id = current_by_id

        groups: dict[tuple[int, str], list[dict[str, str]]] = {}
        for item_id, item in latest_by_id.items():
            if item["status"] in OPEN_STATUSES and item_id in first_seen:
                groups.setdefault(first_seen[item_id], []).append(item)

        lineage = lineages[root_session_id]
        root_session = lineage["root"]
        tip_session = lineage["tip"]
        tip_session_id = str(tip_session["id"])
        for (first_message_id, first_session_id), items in groups.items():
            key = _outcome_key(root_session_id, first_message_id)
            disposition = review_state.get(key, {}) if isinstance(review_state, dict) else {}
            hidden_reason = None
            if disposition.get("disposition") == "closed":
                hidden_reason = "closed"
            elif _deferred_is_active(disposition, now):
                hidden_reason = "deferred"
            if hidden_reason and not include_hidden:
                continue

            title = _nearest_user_prompt(con, first_session_id, first_message_id)
            if not title:
                title = str(tip_session["title"] or root_session["title"] or "").strip()
            if not title:
                active = next((x for x in items if x["status"] == "in_progress"), items[0])
                title = active["content"]

            outcomes.append(
                {
                    "key": key,
                    "title": title,
                    "session_id": tip_session_id,
                    "session_link": f"@session:{tip_session['profile_name'] or root_session['profile_name'] or 'default'}/{tip_session_id}",
                    "source": root_session["source"],
                    "chat_id": root_session["chat_id"],
                    "updated_at": datetime.fromtimestamp(latest_timestamp).astimezone().isoformat(),
                    "items": sorted(items, key=lambda x: (x["status"] != "in_progress", x["id"])),
                    "disposition": disposition or None,
                    "hidden_reason": hidden_reason,
                }
            )

    outcomes.sort(key=lambda x: x["updated_at"], reverse=True)
    return {"version": 1, "generated_at": now.isoformat(), "outcomes": outcomes}


def reserve_action(key: str, action_token: str, *, session_key: str) -> dict[str, Any]:
    session_key = session_key.strip()
    if not session_key:
        raise SystemExit("missing route session key")
    current = snapshot(include_hidden=True)
    outcome = next((item for item in current["outcomes"] if item["key"] == key), None)
    if outcome is None:
        raise SystemExit(f"unknown outcome key: {key}")
    reserved = True
    reason = "reserved"

    def mutate(data: dict[str, Any]) -> None:
        nonlocal reserved, reason
        token_records = data.get("review_tokens", {})
        token_record = token_records.get(key) if isinstance(token_records, dict) else None
        actions = data.setdefault("actions", {})
        if (
            not isinstance(token_record, dict)
            or token_record.get("token") != action_token
            or action_token in actions
        ):
            reserved = False
            reason = "stale"
            return
        pending = data.setdefault("pending_actions", {})
        existing = pending.get(action_token)
        if existing:
            reserved = False
            reason = "already_pending"
            return
        if any(
            isinstance(record, dict)
            and str(record.get("session_key") or "") == session_key
            for record in pending.values()
        ):
            reserved = False
            reason = "route_pending"
            return
        pending[action_token] = {
            "key": key,
            "disposition": "open",
            "session_id": outcome.get("session_id"),
            "session_key": session_key,
            "status": "pending",
            "updated_at": datetime.now(REVIEW_TZ).isoformat(),
        }

    _mutate_state(mutate)
    return {
        "ok": True,
        "applied": reserved,
        "reason": reason,
        "key": key,
        "outcome": {
            "title": outcome.get("title"),
            "session_id": outcome.get("session_id"),
            "session_link": outcome.get("session_link"),
            "items": outcome.get("items", []),
        },
    }


def list_pending_actions() -> dict[str, Any]:
    with _state_lock():
        pending = _read_state().get("pending_actions", {})
    records: list[dict[str, str]] = []
    for token, record in pending.items() if isinstance(pending, dict) else []:
        if not isinstance(record, dict):
            continue
        key = str(record.get("key") or "")
        session_id = str(record.get("session_id") or "")
        session_key = str(record.get("session_key") or "")
        if not re.fullmatch(r"[0-9a-f]{12}", str(token)) or not key or not session_id or not session_key:
            continue
        records.append(
            {
                "action_token": str(token),
                "key": key,
                "session_id": session_id,
                "session_key": session_key,
            }
        )
    records.sort(key=lambda item: item["action_token"])
    return {"ok": True, "pending": records}


def release_action(key: str, action_token: str) -> dict[str, Any]:
    released = False

    def mutate(data: dict[str, Any]) -> None:
        nonlocal released
        actions = data.get("actions", {})
        pending = data.get("pending_actions", {})
        if action_token in actions or not isinstance(pending, dict):
            return
        record = pending.get(action_token)
        if isinstance(record, dict) and record.get("key") == key:
            pending.pop(action_token, None)
            released = True

    _mutate_state(mutate)
    return {"ok": True, "released": released, "key": key}


def action_status(key: str, action_token: str) -> dict[str, Any]:
    with _state_lock():
        data = _read_state()
    actions = data.get("actions", {})
    action = actions.get(action_token) if isinstance(actions, dict) else None
    pending = data.get("pending_actions", {})
    reservation = pending.get(action_token) if isinstance(pending, dict) else None
    if isinstance(action, dict) and str(action.get("key") or "") == key:
        status = (
            "committed_open"
            if str(action.get("disposition") or "") == "open"
            else "committed_other"
        )
    elif isinstance(reservation, dict) and str(reservation.get("key") or "") == key:
        status = "pending"
    else:
        status = "absent"
    return {"ok": True, "key": key, "action_token": action_token, "status": status}


def accept_pending_for_session(
    session_id: str, session_key: str, action_token: str
) -> dict[str, Any]:
    accepted: list[str] = []

    def mutate(data: dict[str, Any]) -> None:
        pending = data.get("pending_actions", {})
        if not isinstance(pending, dict):
            return
        actions = data.setdefault("actions", {})
        outcomes = data.setdefault("outcomes", {})
        token_records = data.get("review_tokens", {})
        record = pending.get(action_token)
        if (
            not isinstance(record, dict)
            or str(record.get("session_id") or "") != session_id
            or str(record.get("session_key") or "") != session_key
        ):
            return
        key = str(record.get("key") or "")
        token_record = token_records.get(key) if isinstance(token_records, dict) else None
        if not key or not isinstance(token_record, dict) or token_record.get("token") != action_token:
            return
        if action_token not in actions:
            actions[action_token] = {
                "key": key,
                "disposition": "open",
                "created_at": datetime.now(REVIEW_TZ).isoformat(),
                "recovered": True,
            }
        outcomes.pop(key, None)
        pending.pop(action_token, None)
        accepted.append(action_token)

    _mutate_state(mutate)
    return {
        "ok": True,
        "accepted": accepted,
        "session_id": session_id,
        "session_key": session_key,
    }


def set_disposition(
    key: str,
    disposition: str,
    until: str | None,
    *,
    action_token: str | None = None,
) -> dict[str, Any]:
    current = snapshot(include_hidden=True)
    outcome = next((item for item in current["outcomes"] if item["key"] == key), None)
    if outcome is None:
        raise SystemExit(f"unknown outcome key: {key}")

    applied = True

    def mutate(data: dict[str, Any]) -> None:
        nonlocal applied
        if action_token:
            token_records = data.get("review_tokens", {})
            token_record = token_records.get(key) if isinstance(token_records, dict) else None
            if not isinstance(token_record, dict) or token_record.get("token") != action_token:
                applied = False
                return
            actions = data.setdefault("actions", {})
            existing = actions.get(action_token)
            if existing:
                applied = False
                return
            actions[action_token] = {
                "key": key,
                "disposition": disposition,
                "created_at": datetime.now(REVIEW_TZ).isoformat(),
            }
            pending = data.get("pending_actions", {})
            if isinstance(pending, dict):
                pending.pop(action_token, None)
        outcomes = data.setdefault("outcomes", {})
        if disposition == "open":
            outcomes.pop(key, None)
        elif disposition == "closed":
            outcomes[key] = {"disposition": "closed", "updated_at": datetime.now(REVIEW_TZ).isoformat()}
        else:
            if until:
                due = _parse_until(until)
            else:
                due = _next_daily_review()
            outcomes[key] = {
                "disposition": "deferred",
                "until": due.isoformat(),
                "updated_at": datetime.now(REVIEW_TZ).isoformat(),
            }

    data = _mutate_state(mutate)
    outcomes = data.get("outcomes", {})
    return {
        "ok": True,
        "applied": applied,
        "key": key,
        "disposition": disposition,
        "until": outcomes.get(key, {}).get("until"),
        "outcome": {
            "title": outcome.get("title"),
            "session_id": outcome.get("session_id"),
            "session_link": outcome.get("session_link"),
            "items": outcome.get("items", []),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    snap = sub.add_parser("snapshot")
    snap.add_argument("--include-hidden", action="store_true")
    action = sub.add_parser("action")
    action.add_argument("--key", required=True)
    action.add_argument("--disposition", choices=("open", "deferred", "closed"), required=True)
    action.add_argument("--until")
    action.add_argument("--action-token")
    reserve = sub.add_parser("reserve")
    reserve.add_argument("--key", required=True)
    reserve.add_argument("--action-token", required=True)
    reserve.add_argument("--session-key", required=True)
    sub.add_parser("pending")
    release = sub.add_parser("release")
    release.add_argument("--key", required=True)
    release.add_argument("--action-token", required=True)
    accept_pending = sub.add_parser("accept-pending")
    accept_pending.add_argument("--session-id", required=True)
    accept_pending.add_argument("--session-key", required=True)
    accept_pending.add_argument("--action-token", required=True)
    status = sub.add_parser("status")
    status.add_argument("--key", required=True)
    status.add_argument("--action-token", required=True)
    deliver = sub.add_parser("deliver")
    deliver.add_argument("--reason", choices=("daily", "restart", "manual"), required=True)
    deliver.add_argument("--force", action="store_true")
    deliver.add_argument("--print-receipt", action="store_true")
    args = parser.parse_args()

    if args.command in (None, "snapshot"):
        result = snapshot(include_hidden=bool(getattr(args, "include_hidden", False)))
    elif args.command == "reserve":
        result = reserve_action(args.key, args.action_token, session_key=args.session_key)
    elif args.command == "pending":
        result = list_pending_actions()
    elif args.command == "release":
        result = release_action(args.key, args.action_token)
    elif args.command == "accept-pending":
        result = accept_pending_for_session(
            args.session_id, args.session_key, args.action_token
        )
    elif args.command == "status":
        result = action_status(args.key, args.action_token)
    elif args.command == "action":
        result = set_disposition(
            args.key,
            args.disposition,
            args.until,
            action_token=args.action_token,
        )
    else:
        result = asyncio.run(deliver_task_review(args.reason, force=args.force))
        if not args.print_receipt:
            return
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
