"""Tool handlers for the dreamer plugin. Signatures follow the plugin rules:
(args: dict, **kwargs) -> str; always JSON, never raises."""

import json
import threading
from typing import Any

from . import dream_engine

_ctx_holder: list = []
_ctx_lock = threading.Lock()


def set_ctx(ctx: Any) -> None:
    with _ctx_lock:
        if not _ctx_holder:
            _ctx_holder.append(ctx)


def get_ctx():
    with _ctx_lock:
        return _ctx_holder[0] if _ctx_holder else None


def dream_now(args: dict, **kwargs) -> str:
    try:
        ctx = get_ctx()
        if ctx is None:
            return json.dumps({"error": "Plugin not initialized."})
        result = dream_engine.run_dream_cycle(
            ctx, force=True, topics_hint=str(args.get("topics_hint", "")).strip())
        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001 - tool handlers never raise
        return json.dumps({"error": f"dream_now failed: {type(e).__name__}: {e}"})


def dream_journal(args: dict, **kwargs) -> str:
    try:
        return _dream_journal_inner(args)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"dream_journal failed: {type(e).__name__}: {e}"})


def _dream_journal_inner(args: dict) -> str:
    try:
        limit = int(args.get("limit", 5))
    except (TypeError, ValueError):
        limit = 5
    dreams = dream_engine.list_dreams(limit=limit)
    if not dreams:
        return json.dumps({"dreams": [],
                           "note": "The journal is empty - no dream has been recorded yet. "
                                   "Call dream_now to trigger one."})
    return json.dumps({"dreams": dreams}, ensure_ascii=False, default=str)


def dream_status(args: dict, **kwargs) -> str:
    try:
        return _dream_status_inner(args)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"dream_status failed: {type(e).__name__}: {e}"})


def _dream_status_inner(args: dict) -> str:
    ctx = get_ctx()
    if ctx is None:
        return json.dumps({"error": "Plugin not initialized."})
    import time as _time

    idle_state = dream_engine.idle_info(ctx)
    conn = dream_engine._db()
    try:
        last_ts = dream_engine._meta_get(conn, "last_dream_ts")
        dream_count = conn.execute("SELECT COUNT(*) FROM dreams").fetchone()[0]
        wake = dream_engine.pending_wake(conn)
    finally:
        conn.close()
    return json.dumps({
        "enabled_at_start": True,
        "interval_minutes": ctx.get_config("interval_minutes", default=720),
        "total_dreams": dream_count,
        "last_dream_ts": float(last_ts) if last_ts else None,
        "minutes_since_last_dream": (
            round((_time.time() - float(last_ts)) / 60.0, 1) if last_ts else None),
        "idle": idle_state,
        "woken_already": bool(wake is None),
        "note": (
            "A fresh session will receive the oldest undelivered dream as residue."
            if wake else "No dream residue waiting to be delivered."),
    }, ensure_ascii=False, default=str)
