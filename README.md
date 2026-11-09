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
