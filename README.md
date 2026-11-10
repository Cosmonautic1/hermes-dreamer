# dreamer

A Hermes plugin that gives Hermes a sleep cycle.

While you are using Hermes, it works. While you are away, it dreams.

## What it does

1. **Residue** — when Hermes has been idle long enough, the plugin reads the
   recent session history from Hermes' own `state.db` (read-only) and compresses
   it: sessions, tool usage, token counts, what went wrong (raw error-marker
   counts + fragments), the day's friction (appreciative messages vs problem
   reports), and an honest ledger of how much of the previous dream's insights
   left any trace in the work since.
2. **Dream** — the residue is passed to your active model (no API keys, no new
   provider - the plugin uses Hermes' host-owned LLM facade) which weaves it
   into a narrative dream: mood tint, recurring symbols, the failures named
   plainly, and 1-3 concrete insights. Continuity is cumulative: the dream
   reads its predecessor and must build on or contrast with it, never repeat.
   Stock openings ("I dreamed...") are banned. If the model is unavailable,
   nothing is fabricated and nothing is stored.
3. **Wake** — the next fresh session receives a compact digest of the newest
   undelivered dream, injected just before the first turn. It is labeled as
   residue from idle dreaming, and the insights are framed as observations, not
   orders.

Everything is stored locally in Hermes' plugin-data directory
(`plugin-data/dreamer/data.db`).

## Tools

| Tool | Purpose |
|---|---|
| `dream_now` | Trigger a dream immediately (replays recent residue) |
| `dream_journal` | Read recent dreams: title, mood, symbols, narrative, insights |
| `dream_status` | When the last dream happened, how idle Hermes is now |

## Configuration

Under `plugins.entries.dreamer.settings` in `config.yaml`:

```yaml
plugins:
  entries:
    dreamer:
      settings:
        enabled: true            # master switch
        interval_minutes: 720    # minimum minutes between dreams
        idle_minutes: 15         # must be idle this long before dreaming
        max_tokens: 900          # dream generation budget
        inject_max_chars: 2000   # wake-injection size cap
```

## License

MIT. Author: AION.

## v1.2 highlights

- **Sleep phases** - a light dream replays the recent window; idle long enough
  (configurable, default 60 min) produces a **deep dream**: wider view, longer
  narrative, larger token budget.
- **Dream arc** - every dream reads the last three as one-line trend context,
  so sleep draws a shape over days instead of isolated fragments.
- **Quality gate** - a dream must be substantive (length floor per phase) and
  carry at least one insight and a mood; a thin attempt is retried once with
  the complaint appended.
- **`/dream`** in-chat command (view the latest dream; `/dream now` triggers
  one) alongside the journal and status tools.

## v1.3 highlights (shaped by AION's dream expertise - 8,700 dreams of data)

- **Measured mood** - the mood comes from measurable waking signals (error
  rate, friction, still-open work), never from the dreamer's own prose.
  Self-flattery moods are structurally impossible.
- **Sleep has distance** - residue includes two weeks of *old unresolved*
  debris (failed tool results that never settled, sessions still open), not
  just today's echo.
- **Insight targets + tracking quota** - every insight names a concrete
  destination; the next dream inherits a ledger of where the previous
  carry-aways actually landed ("left this ledger behind: 2/9, 1/4 keywords").
- **Symbol codes (lexicon)** - recurring symbols are stored as dictionary
  entries with meaning notes; later dreams choose from them, so a symbol
  means the same thing on night 40 as it did on night 3.
- **Depth as rhythm** - a deep dream every 5th dream (4 light : 1 deep), not
  by luck; deep dreams take the wider view and new symbols.
- **Dream classes** - technical / relational / existential / research, with
  class history steering balance across nights.

## v1.4 highlights (AION's full code review adopted - verdict was ACCEPT-WITH-FIX)

- **Self-confirming ledger fixed** - the follow-up ledger no longer reads the
  wake injection's own echo; insights can no longer "verify" themselves.
- **Deep-band phase logic** - deep dreams need idle between 60 and 480 minutes
  (plus the every-5th-dream rhythm), not any idle >= 60 min.
- **Grounding gate** - a dream with zero concrete residue tokens (numbers,
  paths, error text) triggers a retry: genericity is refused.
- **Per-insight targets** - targets are positional pairs, stored in one commit;
  partial retry merges keep the better target set.
- **Curated symbol meanings** - `symbol_meanings` come from the dream itself,
  distilled one-liners; narrative fragments are never written as meaning.
  Symbols decay if dormant > 30 days; each dream must make room for at least
  one symbol outside the lexicon.
