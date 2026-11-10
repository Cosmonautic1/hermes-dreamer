"""Dream engine for the dreamer plugin.

The cycle, in three phases:
1. RESIDUE - read the active profile's state.db (read-only) and compress recent
   session activity into a bounded "residue" text.
2. DREAM - generate a narrative dream + 1-3 insights via the host-owned LLM
   facade (ctx.llm). No fabricated content: if the LLM call fails, no dream
   is stored.
3. WAKE - the next fresh session receives a compact digest of the newest
   undelivered dream via the pre_llm_call hook (capped, once per session).
"""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DREAMER_LOCK = threading.Lock()

# ---------------------------------------------------------------- storage ---


def get_db():
    from plugins.plugin_storage import plugin_db

    return plugin_db("dreamer")


def init_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dreams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            created_at TEXT NOT NULL,
            window_start REAL,
            window_end REAL,
            session_count INTEGER DEFAULT 0,
            message_count INTEGER DEFAULT 0,
            tool_calls INTEGER DEFAULT 0,
            title TEXT,
            narrative TEXT,
            insights_json TEXT,
            residue_chars INTEGER DEFAULT 0,
            generated_by TEXT,
            injected INTEGER DEFAULT 0,
            injected_at REAL,
            inject_count INTEGER DEFAULT 0,
            mood TEXT,
            symbols_json TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS dream_symbols (
            symbol TEXT PRIMARY KEY,
            meaning_note TEXT,
            times_seen INTEGER DEFAULT 1,
            first_dream_id INTEGER,
            last_dream_id INTEGER,
            updated_at REAL
        )"""
    )
    # Additive migration for journals created by v1.0.0 (no mood/symbols yet)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(dreams)")}
    for col, ddl in (("mood", "TEXT"), ("symbols_json", "TEXT"), ("dream_type", "TEXT"),
                     ("class", "TEXT"), ("insight_targets_json", "TEXT")):
        if col not in existing:
            conn.execute(f"ALTER TABLE dreams ADD COLUMN {col} {ddl}")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )"""
    )
    conn.commit()


def _db():
    conn = get_db()
    init_db(conn)
    return conn


def _meta_get(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- residue ---


def _state_db_candidates(profile_name: str) -> List[Path]:
    home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))).expanduser()
    cands: List[Path] = []
    if profile_name and profile_name != "default":
        cands.append(home / "profiles" / profile_name / "state.db")
    cands.append(home / "state.db")
    if profile_name == "default":
        cands.append(home / "profiles" / "default" / "state.db")
    return cands


def _open_state_db(profile_name: str) -> Optional[sqlite3.Connection]:
    for path in _state_db_candidates(profile_name):
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            if {"sessions", "messages"} <= tables:
                return conn
            conn.close()
        except Exception as e:  # noqa: BLE001 - degrade to next candidate
            logger.debug("state.db candidate %s unreadable: %s", path, e)
    return None


def collect_residue(profile_name: str, since_ts: float) -> Optional[Dict[str, Any]]:
    """Compress recent session activity into a bounded residue dict. Returns
    None when there is nothing to dream from (or state.db is unreadable)."""
    conn = _open_state_db(profile_name)
    if conn is None:
        logger.info("dreamer: no readable state.db found (profile=%s)", profile_name)
        return None
    try:
        # Schema probe: a foreign/older Hermes build may name columns differently.
        # Degrade honestly to None instead of raising into the tool handler.
        _REQ_SESSIONS = {"id", "title", "started_at"}
        _REQ_MESSAGES = {"id", "session_id", "role", "content"}
        s_cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        m_cols = {r[1] for r in conn.execute("PRAGMA table_info(messages)")}
        if not (_REQ_SESSIONS <= s_cols and _REQ_MESSAGES <= m_cols):
            logger.debug("dreamer: state.db schema mismatch (profile=%s) - skipping", profile_name)
            return None
        try:
            session_rows = conn.execute(
                "SELECT id, title, source, input_tokens, output_tokens, tool_call_count "
                "FROM sessions WHERE started_at >= ? ORDER BY started_at ASC LIMIT 50",
                (since_ts,),
            ).fetchall()
        except sqlite3.OperationalError:
            session_rows = conn.execute(
                "SELECT id, title, NULL, NULL, NULL, NULL "
                "FROM sessions WHERE started_at >= ? ORDER BY started_at ASC LIMIT 50",
                (since_ts,),
            ).fetchall()
        if not session_rows:
            return None
        session_ids = [r[0] for r in session_rows]
        qmarks = ",".join("?" * len(session_ids))
        message_count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({qmarks})",
            session_ids,
        ).fetchone()[0]
        tool_rows = conn.execute(
            f"SELECT tool_name, COUNT(*) AS n FROM messages "
            f"WHERE session_id IN ({qmarks}) AND tool_name IS NOT NULL "
            f"GROUP BY tool_name ORDER BY n DESC LIMIT 8",
            session_ids,
        ).fetchall()
        user_rows = conn.execute(
            f"SELECT content FROM messages WHERE session_id IN ({qmarks}) "
            f"AND role='user' AND content IS NOT NULL ORDER BY id ASC LIMIT 12",
            session_ids,
        ).fetchall()
        last_user = conn.execute(
            f"SELECT content FROM messages WHERE session_id IN ({qmarks}) "
            f"AND role='user' AND content IS NOT NULL ORDER BY id DESC LIMIT 3",
            session_ids,
        ).fetchall()
        # Failures of the day: tool-result messages carrying error markers
        error_rows = conn.execute(
            f"SELECT content FROM messages WHERE session_id IN ({qmarks}) "
            f"AND (role='tool' OR role='tool_result') AND (content LIKE '%error%' "
            f"OR content LIKE '%failed%' OR content LIKE '%Traceback%') "
            f"ORDER BY id DESC LIMIT 4",
            session_ids,
        ).fetchall()
        error_count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({qmarks}) "
            f"AND (role='tool' OR role='tool_result') AND (content LIKE '%error%' "
            f"OR content LIKE '%failed%' OR content LIKE '%Traceback%')",
            session_ids,
        ).fetchone()[0]
        # Emotional markers in what the user said
        sentiment = conn.execute(
            f"SELECT "
            f"SUM(CASE WHEN content LIKE '%thanks%' OR content LIKE '%tack%' OR content LIKE '%great%' THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN content LIKE '%doesn%' OR content LIKE '%broken%' OR content LIKE '%not working%' "
            f"OR content LIKE '%wrong%' OR content LIKE '%fix%' OR content LIKE '%why%' THEN 1 ELSE 0 END) "
            f"FROM messages WHERE session_id IN ({qmarks}) AND role='user'",
            session_ids,
        ).fetchone()
        # OLD UNRESOLVED (v1.3/1.4, AION's rule: sleep needs distance): the last
        # two weeks' failure debris that settled (closed sessions only), bounded
        # ABOVE by since_ts so fresh errors are not double-counted.
        old_window = since_ts - 14 * 86400
        old_errors = conn.execute(
            "SELECT content FROM messages m JOIN sessions s ON m.session_id = s.id "
            f"WHERE m.timestamp >= ? AND m.timestamp < ? "
            f"AND m.role IN ('tool','tool_result') AND s.ended_at IS NOT NULL "
            f"AND (m.content LIKE '%error%' OR m.content LIKE '%failed%') "
            f"ORDER BY m.id DESC LIMIT 3",
            (old_window, since_ts),
        ).fetchall()
        old_error_count = conn.execute(
            "SELECT COUNT(*) FROM messages m JOIN sessions s ON m.session_id = s.id "
            f"WHERE m.timestamp >= ? AND m.timestamp < ? "
            f"AND m.role IN ('tool','tool_result') AND s.ended_at IS NOT NULL "
            f"AND (m.content LIKE '%error%' OR m.content LIKE '%failed%')",
            (old_window, since_ts),
        ).fetchone()[0]
        open_sessions_count = conn.execute(
            f"SELECT COUNT(*) FROM sessions WHERE ended_at IS NULL AND started_at >= ?",
            (old_window,),
        ).fetchone()[0]
        tool_msgs_count = conn.execute(
            f"SELECT COUNT(*) FROM messages WHERE session_id IN ({qmarks}) "
            f"AND (role='tool' OR role='tool_result')",
            session_ids,
        ).fetchone()[0]
    except sqlite3.OperationalError as _schema_err:
        logger.debug("dreamer: old-unresolved queries degraded (schema): %s", _schema_err)
        old_errors, old_error_count = [], 0
        open_sessions_count = 0
        tool_msgs_count = None
        error_rows, error_count = [], 0
        positive, negative = 0, 0
    finally:
        conn.close()

    def _clean(text: str, limit: int = 160) -> str:
        text = re.sub(r"\s+", " ", str(text)).strip()
        return text[:limit] + ("..." if len(text) > limit else "")

    earliest_user = [_clean(r[0]) for r in user_rows]
    latest_user = [_clean(r[0]) for r in last_user]
    failure_snippets = [_clean(r[0], 130) for r in error_rows]
    positive, negative = sentiment[0] or 0, sentiment[1] or 0
    old_unresolved = {
        "error_count_14d": old_error_count,
        "recent_old_failures": [_clean(r[0], 120) for r in old_errors],
        "still_open_sessions": open_sessions_count,
    }
    sentiment_structured = {"thanks": positive, "problems": negative}
    total_in = sum(r[3] or 0 for r in session_rows)
    total_out = sum(r[4] or 0 for r in session_rows)
    total_tools = sum(r[5] or 0 for r in session_rows)
    titles = [_clean(r[1], 80) for r in session_rows if r[1]]
    return {
        "window": {
            "start": datetime.fromtimestamp(
                since_ts, tz=timezone.utc).isoformat(timespec="seconds"),
            "end": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
        },
        "session_count": len(session_rows),
        "sources": sorted({str(r[2]) for r in session_rows if r[2]}),
        "message_count": message_count,
        "input_tokens": total_in,
        "output_tokens": total_out,
        "tool_calls": total_tools,
        "error_count": error_count,
        "tool_message_count": tool_msgs_count,
        "sentiment": sentiment_structured,
        "failure_snippets": failure_snippets,
        "friction": f"{positive} appreciative user messages, {negative} problem reports",
        "old_unresolved": old_unresolved,
        "top_tools": [f"{r[0]} x{r[1]}" for r in tool_rows],
        "session_titles": titles[:10],
        "earliest_user_messages": earliest_user,
        "latest_user_messages": latest_user,
    }


def _residue_text(residue: Dict[str, Any], topics_hint: str = "") -> str:
    lines = [
        f"Window: {residue['window']['start']} to {residue['window']['end']}",
        f"Sessions: {residue['session_count']} "
        f"(platforms: {', '.join(residue['sources']) or 'unknown'})",
        f"Messages: {residue['message_count']} | tool calls: {residue['tool_calls']} | "
        f"tokens in/out: {residue['input_tokens']}/{residue['output_tokens']}",
        f"Top tools: {', '.join(residue['top_tools']) or 'none'}",
    ]
    if residue["session_titles"]:
        lines.append("Session titles: " + " | ".join(residue["session_titles"]))
    if residue["earliest_user_messages"]:
        lines.append("What the user asked for (earliest):")
        lines += [f"- {m}" for m in residue["earliest_user_messages"][:8]]
    if residue["latest_user_messages"]:
        lines.append("Where the day ended (latest):")
        lines += [f"- {m}" for m in residue["latest_user_messages"]]
    if topics_hint:
        lines.append(f"Focus themes for this dream: {topics_hint}")
    lines.append(f"Friction of the day: {residue['friction']}; "
                 f"{residue['error_count']} tool-result error markers")
    if residue["failure_snippets"]:
        lines.append("What went wrong (raw fragments):")
        lines += [f"- {m}" for m in residue["failure_snippets"]]
    followup = residue.get("insight_followup")
    if followup:
        lines.append(
            "Insight follow-up (honest ledger of the previous dream's insights): "
            f"{followup}")
    # CRITICAL tail (v1.4, AION's review): measured mood, old-unresolved and the
    # symbol lexicon may NEVER be cut by truncation - cap only the verbose head.
    critical = []
    old = residue.get("old_unresolved") or {}
    critical.append(
        f"Old unresolved (sleep needs distance - the last two weeks, not just today): "
        f"{old.get('error_count_14d', 0)} error-marker tool results, "
        f"{old.get('still_open_sessions', 0)} session(s) still open (unfinished rooms)")
    if old.get("recent_old_failures"):
        critical.append("Old failure debris (still unexplained):")
        critical += [f"- {m}" for m in old["recent_old_failures"]]
    critical.append(f"MOOD (measured, non-negotiable - the dreamer does not get to pick its "
                    f"own mood): {measured_mood(residue)}")
    lex = residue.get("symbol_lexicon")
    if lex:
        critical.append("Dream symbol codes (your recurring symbols and what they "
                        "have meant before):")
        critical += [f"- {l}" for l in lex]
        critical.append("At least ONE (1) symbol in this dream must NOT appear in "
                        "the list above - make room for new material.")
    verbose = "\n".join(lines)
    crit_text = "\n".join(critical)
    budget = max(400, 3000 - len(crit_text) - 30)
    if len(verbose) > budget + len(crit_text):
        verbose = verbose[:budget] + "\n[verbose residue truncated]"
    return verbose + "\n" + crit_text


def measured_mood(residue: Dict[str, Any]) -> str:
    """v1.3/1.4 (AION's rule): mood comes from measurable waking signals, never
    from the dreamer's own prose. Deterministic map over error rate (measured
    against TOOL messages, not total chatter) + friction + unfinished rooms."""
    tool_msgs = residue.get("tool_message_count")
    errors = int(residue.get("error_count", 0) or 0)
    if tool_msgs:
        err_ratio = errors / max(1, int(tool_msgs))
    else:
        err_ratio = errors / max(1, int(residue.get("message_count", 0) or 0))
    old = residue.get("old_unresolved") or {}
    open_rooms = int(old.get("still_open_sessions", 0) or 0)
    sentiment = residue.get("sentiment") or {}
    thanks = int(sentiment.get("thanks", 0) or 0)
    problems = int(sentiment.get("problems", 0) or 0)
    if err_ratio > 0.25 and thanks == 0:
        return "bruised and on guard"
    if err_ratio > 0.25 and thanks > 0:
        return "fired up under load"
    if open_rooms > 0 and problems > 0:
        return "quietly worried about unfinished rooms"
    if err_ratio < 0.05 and problems == 0 and thanks > 0:
        return "flowing, quietly confident"
    if err_ratio < 0.05:
        return "steady, low-aftershock"
    return "steady but scored"


def _dream_targets(insights: list, data: Dict[str, Any]) -> list:
    """v1.3: insight entries may be dicts {'insight','target'} or plain strings.
    Normalizes to [{'insight','target': ''}] and keeps the raw strings for the
    narrative shape."""
    raw = data.get("insight_targets") or []
    targets = []
    if isinstance(raw, list):
        for r in raw:
            if isinstance(r, dict) and r.get("target"):
                targets.append(str(r["target"]).strip()[:160])
            elif isinstance(r, str):
                targets.append(r.strip()[:160])
    out = []
    for i, ins in enumerate(insights):
        target = targets[i] if i < len(targets) else ""
        out.append({"insight": ins, "target": target})
    return out


def _lexicon(conn: sqlite3.Connection) -> list:
    """v1.3: recurring symbol codes with their meaning notes (AION: symbols
    became real for her once they were lexicon entries)."""
    rows = conn.execute(
        "SELECT symbol, meaning_note, times_seen FROM dream_symbols "
        "ORDER BY times_seen DESC LIMIT 10").fetchall()
    return [f"'{s}' (seen {n}x{', means: ' + m if m else ''})"
            for s, m, n in rows]


def _lexicon_upsert(conn: sqlite3.Connection, dream_id: int,
                    symbols: list, meanings: Dict[str, str]) -> None:
    """v1.4: times_seen decays if the symbol was dormant > 30 days (AION's
    anti-freeze); meaning_note ONLY ever comes from the freshly curated
    symbol_meanings - narrative fragments are never written as meaning."""
    now = time.time()
    for s in symbols:
        key = s.strip().lower()[:60]
        if not key:
            continue
        curated = (meanings.get(key) or "").strip()
        conn.execute(
            """INSERT INTO dream_symbols (symbol, meaning_note, times_seen,
               first_dream_id, last_dream_id, updated_at)
               VALUES (?,?,1,?,?,?)
               ON CONFLICT(symbol) DO UPDATE SET
                 times_seen = CASE
                   WHEN updated_at < ? THEN 1 ELSE times_seen + 1 END,
                 meaning_note = COALESCE(NULLIF(?, ''), meaning_note),
                 last_dream_id = excluded.last_dream_id,
                 updated_at = excluded.updated_at""",
            (key, curated, dream_id, dream_id, now,
             now - 30 * 86400, curated))


def _insight_followup_check(profile_name: str, insights: list,
                            since_ts: float) -> str:
    """Honest, cheap verification: did the previous dream's insights leave any
    trace in the sessions since? Keyword-overlap on real session messages."""
    if not insights:
        return "no previous insights to verify"
    conn = _open_state_db(profile_name)
    if conn is None:
        return "state.db unreadable - unverified"
    try:
        rows = conn.execute(
            "SELECT content FROM messages WHERE timestamp >= ? "
            "AND content IS NOT NULL AND LENGTH(content) > 20 "
            "AND content NOT LIKE '%[DREAM RESIDUE]%' "
            "LIMIT 500",
            (since_ts,),
        ).fetchall()
    finally:
        conn.close()
    blob = " ".join(str(r[0]).lower() for r in rows)
    results = []
    for ins in insights:
        words = [w for w in re.findall(r"[a-z_]{4,}", ins.lower())
                 if w not in ("this", "that", "with", "your", "should", "when",
                              "next", "them", "from", "what", "into", "session",
                              "insight", "carry", "aways")]
        if not words:
            results.append("?")
            continue
        hits = sum(1 for w in set(words) if w in blob)
        results.append(f"{hits}/{len(set(words))} keywords")
    return "; ".join(results)


# ----------------------------------------------------------------- dreaming ---


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


DREAM_SYSTEM_PROMPT = (
    "You are the dream phase of an AI agent named Hermes. It has just gone idle after "
    "a day of real work with real users. You receive that day's residue: session "
    "stats, tool usage, fragments of conversations, and what went wrong. Weave them "
    "into a short dream - part poetic, part diagnostic, like sleep consolidating "
    "memory. Rules: (1) Ground every image in the residue - no generic dreaminess. "
    "(2) Name the day's failures and unfinished threads; a dream that only flatters "
    "is useless. (3) Open the narrative with a FRESH image - never open with 'I "
    "dreamed', 'Last night', 'In my dream' or any similar stock opening. (4) The "
    "MEASURED MOOD line is non-negotiable: express that mood through imagery; you "
    "do not get to pick a more comfortable one. (5) Every dream must contain at "
    "least one detail ONLY this agent could produce - a file name, an error text, a "
    "number from the residue. If the dream could belong to anyone, it is worthless. "
    "(6) The dream may be wrong - state its claims as observations that reality can "
    "contradict. (7) Be honest about what went badly in the residue. Reply in English."
)

DREAM_INSTRUCTIONS = (
    "Return JSON exactly like: "
    '{"title": "short evocative dream title", '
    '"class": "one of technical / relational / existential / research - which kind of dream this is", '
    '"symbols": ["3-5 concrete recurring objects/images: reuse symbols from the symbol codes list when they fit, otherwise invent new ones"], '
    '"narrative": "the dream in phase-appropriate length, second person, grounded in the residue, '
    'naming at least one thing that went wrong, expressing the measured mood through imagery", '
    '"insights": ["1-3 short actionable insights"], '
    '"insight_targets": ["for each insight, the concrete destination it points at - a file name, a module, a task type; empty string if none"], '
    '"symbol_meanings": {"SYMBOL": "one-line distilled meaning (what this recurring symbol has come to mean) - one entry per symbol"}'
)


def _llm_text_block(text: str) -> Any:
    """Build a host-side text block: PluginLlmTextInput when importable, else
    the documented dict shape with the same fields."""
    try:
        from agent.plugin_llm import PluginLlmTextInput

        return PluginLlmTextInput(text=text)
    except Exception:  # noqa: BLE001 - dict shape is an accepted compatible fallback
        return {"text": text, "type": "text"}


def generate_dream(ctx: Any, residue: Dict[str, Any], topics_hint: str = "",
                   max_tokens: int = 900,
                   continuity: Optional[Dict[str, Any]] = None,
                   arc: Optional[list] = None,
                   phase: str = "light") -> Optional[Dict[str, Any]]:
    """Phase 2: the LLM call. Returns {'title','narrative','insights',...} or None."""
    residue_text = _residue_text(residue, topics_hint)
    if arc:
        residue_text += "\nDream arc (the shape sleep has been drawing lately):\n"
        residue_text += "\n".join(f"- {line}" for line in arc)
    if continuity:
        residue_text += (
            "\nContinuity (sleep is cumulative - do not repeat the last dream, "
            "build on it or contrast with it):\n"
            f"Previous dream '{continuity.get('title', '')}': "
            f"{(continuity.get('narrative') or '')[:350]}\n"
            "Previous insights so far: " + "; ".join(continuity.get("insights", [])[:3])
        )
    if phase == "deep":
        residue_text += (
            "\nThis is a DEEP dream: a long idle period. Take the wider view - the "
            "arc, not just the day. Length: 150-250 words."
        )
    def _call_llm(complaint: str) -> Optional[Dict[str, Any]]:
        text = residue_text + (f"\n\nQuality complaint to fix in the retry: {complaint}"
                               if complaint else "")
        try:
            result = ctx.llm.complete_structured(
                instructions=DREAM_INSTRUCTIONS,
                input=[_llm_text_block(text)],
                system_prompt=DREAM_SYSTEM_PROMPT,
                json_mode=True,
                schema_name="Dream",
                max_tokens=max_tokens,
                timeout=240,
                purpose="dreamer dream cycle",
            )
            parsed = getattr(result, "parsed", None)
            if isinstance(parsed, dict) and parsed.get("narrative"):
                return parsed
            return _extract_json(getattr(result, "text", "") or "")
        except Exception as e:  # noqa: BLE001 - honest skip, never fabricate a dream
            logger.warning("dreamer: LLM call failed: %s", e)
            return None

    data = _call_llm("")
    if not data or not isinstance(data.get("narrative"), str) or not data["narrative"].strip():
        logger.warning("dreamer: dream generation returned no narrative")
        return None
    # Quality gate v1.3: substantive narrative + insights + targets;
    # one retry with the complaint appended.
    _min_chars = 350 if phase == "deep" else 250
    data_norm = _normalize_dream(data)
    def _quality_complaint(dn):
        c = []
        if len(dn["narrative"]) < _min_chars:
            c.append(f"narrative too thin ({len(dn['narrative'])} chars, need {_min_chars})")
        if not dn["insights"]:
            c.append("no insights returned")
        if all(not t for t in (dn.get("insight_targets") or [])) if dn.get("insight_targets") else False:
            c.append("every insight target was empty - name a concrete destination "
                     "(file name, module or task type) for each insight")
        return "; ".join(c)
    def _missing_targets(dn):
        tg = dn.get("insight_targets") or []
        missing = [j for j, t in enumerate(tg) if not t.strip()]
        while len(tg) < len(dn.get("insights") or []):
            missing.append(len(tg))
            tg.append("")
        return [j + 1 for j in missing]
    # Grounding check (AION's genericity pitfall): a dream with NO concrete
    # residue token (numbers, file paths, error words) is generic - retry.
    def _grounding_complaint(dn):
        text = (dn["narrative"] or "").lower()
        concrete = sum(
            1 for pat in (r"\d", r"\.py", r"\.py\b", r"error", r"failed",
                          r"traceback", r"c\d+", r"task ")
            if re.search(pat, text))
        return concrete == 0
    if (len(data_norm["narrative"]) < _min_chars or not data_norm["insights"]
            or _grounding_complaint(data_norm)):
        complaint = _quality_complaint(data_norm) or "narrative too thin"
        retry = _call_llm("; ".join(complaint))
        retry_norm = _normalize_dream(retry) if retry else None
        if retry_norm and len(retry_norm["narrative"]) >= _min_chars and retry_norm["insights"]:
            data_norm = retry_norm
            logger.info("dreamer: quality retry accepted the improved dream")
        elif retry_norm:
            # Partial merge keeps the retry's non-empty targets (per position)
            merged_targets = [
                (rn or on) for on, rn in zip(
                    (data_norm.get("insight_targets") or ["", "", ""]),
                    (retry_norm.get("insight_targets") or ["", "", ""]))]
            merged_meanings = {**(retry_norm.get("symbol_meanings") or {}),
                               **(data_norm.get("symbol_meanings") or {})}
            data_norm = {
                **data_norm,
                "insights": data_norm["insights"] or retry_norm["insights"],
                "mood": data_norm["mood"] or retry_norm["mood"],
                "insight_targets": merged_targets[:3],
                "symbol_meanings": merged_meanings,
            }
    return data_norm

_DREAM_BANNED_PREFIXES = ("i dreamed", "i dream", "last night", "in my dream",
                          "you dreamed", "last night i", "the dream")


def _normalize_dream(data: Dict[str, Any]) -> Dict[str, Any]:
    """Map a parsed LLM dict onto the dream shape: strip, ban stock openings,
    clamp fields. Never raises - a malformed field degrades to empty."""
    insights = data.get("insights") or []
    if isinstance(insights, str):
        insights = [insights]
    if not isinstance(insights, list):
        insights = []
    narrative = str(data.get("narrative") or "").strip()[:4000]
    for prefix in _DREAM_BANNED_PREFIXES:
        if narrative.lower().startswith(prefix):
            narrative = narrative[len(prefix):].lstrip(" ,.;:-'\"")
            break
    symbols = data.get("symbols") or []
    if isinstance(symbols, str):
        symbols = [symbols]
    if not isinstance(symbols, list):
        symbols = []
    raw_targets = data.get("insight_targets") or []
    target_strings = []
    if isinstance(raw_targets, list):
        for r in raw_targets:
            if isinstance(r, dict) and r.get("target"):
                target_strings.append(str(r["target"]).strip()[:160])
            elif isinstance(r, str):
                target_strings.append(r.strip()[:160])
    raw_meanings = data.get("symbol_meanings") or {}
    meanings = {}
    if isinstance(raw_meanings, dict):
        for k, v in raw_meanings.items():
            if str(k).strip() and str(v).strip():
                meanings[str(k).strip().lower()[:60]] = str(v).strip()[:200]
    return {
        "title": str(data.get("title") or "Untitled dream").strip()[:120],
        "narrative": narrative,
        "mood": str(data.get("mood") or "").strip()[:80],
        "class": str(data.get("class") or "").strip()[:40].lower(),
        "symbols": [str(s).strip()[:60] for s in symbols[:6] if str(s).strip()],
        "symbol_meanings": meanings,
        "insights": [str(i).strip()[:400] for i in insights[:3] if str(i).strip()],
        "insight_targets": target_strings[:3],
    }


# ------------------------------------------------------------- full cycle ---


def run_dream_cycle(ctx: Any, *, force: bool = False, topics_hint: str = "") -> Dict[str, Any]:
    """Full cycle. Returns a result dict the tool handler can return."""
    if not DREAMER_LOCK.acquire(blocking=False):
        return {"error": "A dream is already in progress."}
    try:
        conn = _db()
        try:
            enabled = ctx.get_config("enabled", default=True)
            interval = max(0, int(ctx.get_config("interval_minutes", default=720)))
            idle_min = max(1, int(ctx.get_config("idle_minutes", default=15)))
            max_tokens = max(300, int(ctx.get_config("max_tokens", default=900)))
            now = time.time()
            last_dream_ts = float(_meta_get(conn, "last_dream_ts") or 0)
            if not force:
                if not enabled:
                    return {"error": "Dreaming is disabled (config: dreamer.enabled=false)."}
                if now - last_dream_ts < interval * 60:
                    mins_left = int(interval - (now - last_dream_ts) / 60)
                    return {"error": f"Too soon - next dream in about {mins_left} minutes.",
                            "status_tool": "dream_status"}
            idle_state = idle_info(ctx)
            if not force and not idle_state.get("idle", False):
                return {
                    "error": (
                        "Hermes is not idle enough to dream "
                        f"(last activity {idle_state.get('idle_minutes', '?')} min ago, "
                        f"needs {idle_min} min). Say 'dreamer: force' by calling me again "
                        "during actual inactivity, or just call dream_now with topics_hint."
                    ),
                }
            profile = getattr(ctx, "profile_name", None) or "default"
            residue = collect_residue(profile, since_ts=last_dream_ts)
            if residue is None:
                return {"error": "Nothing to dream from: no recent sessions found."}
            # Continuity (v1.3): show WHERE the previous dream went wrong/undone,
            # not just its text - a dream must answer the last one.
            prev_row = conn.execute(
                "SELECT title, narrative, insights_json, dream_type FROM dreams "
                "ORDER BY id DESC LIMIT 1").fetchone()
            continuity = None
            if prev_row:
                insights_prev = []
                try:
                    insights_prev = json.loads(prev_row[2] or "[]")
                except json.JSONDecodeError:
                    insights_prev = []
                prev_targets = []
                prev_t_row = conn.execute(
                    "SELECT insight_targets_json FROM dreams ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if prev_t_row and prev_t_row[0]:
                    try:
                        prev_targets = json.loads(prev_t_row[0])
                    except (json.JSONDecodeError, TypeError):
                        prev_targets = []
                followup = _insight_followup_check(
                    profile, insights_prev, since_ts=last_dream_ts)
                residue["insight_followup"] = followup
                tracked = sum(1 for r in re.findall(
                    r"(\d+)/(\d+)", followup or "") if int(r[0]) > 0)
                continuity = {
                    "title": prev_row[0], "narrative": prev_row[1],
                    "insights": insights_prev,
                    "targets": [t.get("target") if isinstance(t, dict) else t
                                for t in prev_targets],
                    "status_line": (
                        f"the previous dream ({prev_row[3] or 'light'}) produced "
                        f"{len(insights_prev)} carry-aways and left this ledger "
                        f"behind: {followup}. In THIS dream, revisit what it got "
                        f"wrong or left undone - do not just nod at it."),
                    "tracked_ratio_seen": tracked,
                }
            # Dream arc (v1.2/1.3): trend context + class balance.
            arc_rows = conn.execute(
                "SELECT title, narrative FROM dreams ORDER BY id DESC LIMIT 3"
            ).fetchall() if prev_row else []
            arc = [f"'{t}': {(n or '')[:110].strip()}..."
                   for t, n in arc_rows if t]
            class_hist = [r[0] for r in conn.execute(
                "SELECT class FROM dreams WHERE class IS NOT NULL "
                "ORDER BY id DESC LIMIT 5")] if prev_row else []
            if class_hist:
                arc.append("Dream classes recently: " + ", ".join(
                    class_hist) + " - if a kind of dream is missing, tonight's "
                    "dream may lean toward it (technical/relational/existential/research).")

            # Lexicon (v1.3): recurring symbol codes with their meaning notes.
            lex = _lexicon(conn)
            if lex:
                residue["symbol_lexicon"] = lex

            # Sleep phase (v1.2/1.3/1.4): deep on a deep BAND (60-480 min idle,
            # AION's review: without the upper bound every background dream is
            # deep because idle is almost always >= 60 min) OR the 4:1 rhythm.
            deep_after = max(0, int(ctx.get_config("deep_sleep_minutes", default=60)))
            idle_minutes = float(idle_state.get("idle_minutes") or 0)
            in_deep_band = deep_after <= idle_minutes < deep_after * 8
            dream_no = int(conn.execute(
                "SELECT COUNT(*) FROM dreams").fetchone()[0]) + 1
            pattern_deep = dream_no % 5 == 0
            phase = "deep" if (in_deep_band or pattern_deep) else "light"
            phase_tokens = max(600, int(ctx.get_config("max_tokens", default=900)))
            if phase == "deep":
                phase_tokens = max(phase_tokens, 1200)

            dream = generate_dream(ctx, residue, topics_hint,
                                   max_tokens=phase_tokens, continuity=continuity,
                                   arc=arc, phase=phase)
            if dream is None:
                return {"error": "Dream generation failed (LLM unavailable). No dream was recorded."}
            dream["dream_type"] = phase
            dream["mood"] = measured_mood(residue) or dream.get("mood")
            # v1.4: per-position insight/target dict pairs (insight_targets from
            # the normalized dream are positional strings).
            _tg = dream.get("insight_targets") or []
            dream["insight_targets"] = [
                {"insight": i,
                 "target": (_tg[j] if isinstance(_tg[j], str) else "")
                 if j < len(_tg) else ""}
                for j, i in enumerate(dream["insights"])]
            window_end = now
            window_start = last_dream_ts if last_dream_ts else now - 86400
            conn.execute(
                "INSERT INTO dreams (ts, created_at, window_start, window_end, "
                "session_count, message_count, tool_calls, title, narrative, "
                "insights_json, residue_chars, generated_by, mood, symbols_json, "
                "dream_type, class, insight_targets_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    now,
                    datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
                    window_start,
                    window_end,
                    residue["session_count"], residue["message_count"], residue["tool_calls"],
                    dream["title"], dream["narrative"],
                    json.dumps(dream["insights"]),
                    len(_residue_text(residue, topics_hint)),
                    str(getattr(getattr(ctx, "llm", None), "__class__", "?") or "active-model"),
                    dream["mood"],
                    json.dumps(dream.get("symbols") or []),
                    dream["dream_type"],
                    dream.get("class") or None,
                    json.dumps(dream["insight_targets"]),
                ),
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('last_dream_ts', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(now),))
            conn.commit()
            dream_id = conn.execute(
                "SELECT last_insert_rowid()").fetchone()[0]
            # v1.4: class stored IN the INSERT (single commit, no orphan window)
            _lexicon_upsert(conn, dream_id, dream.get("symbols") or [],
                            dream.get("symbol_meanings") or {})
            conn.commit()
            logger.info("dreamer: dream #%s recorded (%s, phase=%s, class=%s)",
                        dream_id, dream["title"], phase, dream.get("class"))
            return {"dream": dream, "id": dream_id}
        finally:
            conn.close()
    finally:
        DREAMER_LOCK.release()


def idle_info(ctx: Any) -> Dict[str, Any]:
    """How idle is Hermes right now, honestly measured from state.db."""
    profile = getattr(ctx, "profile_name", None) or "default"
    conn = _open_state_db(profile)
    if conn is None:
        return {"idle": False, "last_activity": None, "idle_minutes": None,
                "note": "no readable state.db"}
    try:
        row = conn.execute(
            "SELECT MAX(COALESCE(last_activity_at, started_at)) FROM sessions").fetchone()
        last = row[0] if row else None
    finally:
        conn.close()
    if not last:
        return {"idle": True, "last_activity": None, "idle_minutes": None,
                "note": "no sessions at all"}
    try:
        last_f = float(last)
    except (TypeError, ValueError):
        last_f = None
    if last_f is None:
        return {"idle": False, "last_activity": str(last), "idle_minutes": None,
                "note": "unparseable timestamp"}
    idle_min = (time.time() - last_f) / 60.0
    idle_min_required = max(1, int(ctx.get_config("idle_minutes", default=15)))
    return {"idle": idle_min >= idle_min_required, "last_activity": last_f,
            "idle_minutes": round(idle_min, 1), "idle_minutes_required": idle_min_required}


def pending_wake(conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    cutoff = time.time() - 48 * 3600
    row = conn.execute(
        "SELECT id, title, narrative, insights_json, injected, inject_count, ts, "
        "insight_targets_json "
        "FROM dreams WHERE injected=0 AND ts >= ? ORDER BY id DESC LIMIT 1",
        (cutoff,),
    ).fetchone()
    if not row:
        return None
    insights = []
    try:
        insights = json.loads(row[3] or "[]")
    except json.JSONDecodeError:
        insights = []
    targets = []
    if len(row) > 6 and row[6]:
        try:
            raw = json.loads(row[6])
            targets = [
                (t.get("target", "") if isinstance(t, dict) else str(t or ""))
                for t in raw]
        except (json.JSONDecodeError, TypeError, AttributeError):
            targets = []
    return {"id": row[0], "title": row[1], "narrative": row[2],
            "insights": insights, "targets": targets,
            "ts": row[5], "inject_count": row[4]}


def build_wake_context(dream: Dict[str, Any], max_chars: int = 2000) -> str:
    lines = [
        "[DREAM RESIDUE] While you were idle, the dreamer plugin replayed your recent "
        "work and generated a dream you have not woken with yet.",
        f"Dream '{dream['title']}': {dream['narrative'][:700]}",
    ]
    if dream.get("insights"):
        targets = dream.get("targets") or []
        lines.append("Carry-aways you may use this session (they are observations, not orders):")
        lines += [
            f"- {i} → {targets[j]}" if j < len(targets) and targets[j] else f"- {i}"
            for j, i in enumerate(dream["insights"])]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 3] + "..."
    return text


def list_dreams(limit: int = 5) -> List[Dict[str, Any]]:
    limit = max(1, min(20, int(limit)))
    with_dreams: List[Dict[str, Any]] = []
    conn = _db()
    try:
        rows = conn.execute(
            "SELECT id, created_at, title, narrative, insights_json, injected, "
            "inject_count, mood, symbols_json, dream_type "
            "FROM dreams ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        for row in rows:
            try:
                insights = json.loads(row[4] or "[]")
            except json.JSONDecodeError:
                insights = []
            try:
                symbols = json.loads(row[8] or "[]")
            except json.JSONDecodeError:
                symbols = []
            with_dreams.append({
                "id": row[0], "created_at": row[1], "title": row[2],
                "narrative": row[3], "insights": insights,
                "delivered_to_session": bool(row[5]), "injections": row[6],
                "mood": row[7], "symbols": symbols,
                "dream_type": row[9] or "light",
            })
    finally:
        conn.close()
    return with_dreams


def mark_waked(dream_id: int) -> None:
    conn = _db()
    try:
        conn.execute(
            "UPDATE dreams SET injected=1, injected_at=?, inject_count=inject_count+1 "
            "WHERE id=?", (time.time(), dream_id))
        conn.commit()
    finally:
        conn.close()
