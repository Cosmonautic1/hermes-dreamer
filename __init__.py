"""Dreamer plugin - registration, wake hook, background idle-dreamer thread."""

import logging
import threading
import time
from pathlib import Path

from . import dream_engine, schemas, tools

logger = logging.getLogger(__name__)

THREAD_INTERVAL_SECONDS = 900


def _wake_hook(session_id: str, user_message: str, is_first_turn: bool, **kwargs):
    """pre_llm_call: on the first turn of a fresh session, deliver dream residue."""
    if not is_first_turn:
        return None
    try:
        conn = dream_engine._db()
        try:
            dream = dream_engine.pending_wake(conn)
        finally:
            conn.close()
        if dream is None:
            return None
        max_chars = 2000
        try:
            ctx = tools.get_ctx()
            if ctx is not None:
                max_chars = int(ctx.get_config("inject_max_chars", default=2000))
        except Exception:  # noqa: BLE001 - fall back to default cap
            max_chars = 2000
        context = dream_engine.build_wake_context(dream, max_chars=max_chars)
        dream_engine.mark_waked(dream["id"])
        logger.info("dreamer: wake residue delivered into session %s (dream #%s)",
                    session_id, dream["id"])
        return {"context": context}
    except Exception as e:  # noqa: BLE001 - a broken dream must never break a session
        logger.debug("dreamer: wake hook skipped: %s", e)
        return None


def _register_skills(ctx) -> None:
    skills_dir = Path(__file__).parent / "skills"
    try:
        for child in sorted(skills_dir.iterdir()):
            skill_md = child / "SKILL.md"
            if child.is_dir() and skill_md.exists():
                ctx.register_skill(child.name, skill_md)
    except Exception as e:  # noqa: BLE001
        logger.debug("dreamer: skill registration skipped: %s", e)


def _background_dreamer_loop(ctx) -> None:  # pragma: no cover - daemon loop
    logger.info("dreamer: background dreamer thread started")
    while True:
        time.sleep(THREAD_INTERVAL_SECONDS)
        try:
            enabled = ctx.get_config("enabled", default=True)
            if not enabled:
                continue
            interval = max(0, int(ctx.get_config("interval_minutes", default=720)))
            idle_state = dream_engine.idle_info(ctx)
            if not idle_state.get("idle", False):
                continue
            conn = dream_engine._db()
            try:
                last_ts = float(dream_engine._meta_get(conn, "last_dream_ts") or 0)
            finally:
                conn.close()
            if time.time() - last_ts < interval * 60:
                continue
            result = dream_engine.run_dream_cycle(ctx)
            if result.get("dream"):
                logger.info("dreamer: background dream recorded: %s",
                            result["dream"].get("title"))
            else:
                logger.debug("dreamer: background dream skipped: %s", result.get("error"))
        except Exception as e:  # noqa: BLE001 - the dreamer thread must survive storms
            logger.warning("dreamer: background cycle error: %s", e)


def register(ctx):
    tools.set_ctx(ctx)

    ctx.register_tool(
        name="dream_now", toolset="dreamer", schema=schemas.DREAM_NOW,
        handler=tools.dream_now,
        description="Trigger a dream cycle now, replaying recent session residue.")
    ctx.register_tool(
        name="dream_journal", toolset="dreamer", schema=schemas.DREAM_JOURNAL,
        handler=tools.dream_journal)
    ctx.register_tool(
        name="dream_status", toolset="dreamer", schema=schemas.DREAM_STATUS,
        handler=tools.dream_status)

    ctx.register_hook("pre_llm_call", _wake_hook)

    thread = threading.Thread(
        target=_background_dreamer_loop, args=(ctx,),
        name="dreamer-idle-thread", daemon=True)
    thread.start()

    logger.info("dreamer: registered 3 tools, wake hook, idle dreamer thread")
