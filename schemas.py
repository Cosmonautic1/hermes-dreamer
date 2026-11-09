"""Tool schemas for the dreamer plugin - what the LLM sees."""

DREAM_NOW = {
    "name": "dream_now",
    "description": (
        "Trigger a dream cycle right now: replay the recent session activity (the day's "
        "residue), generate a narrative dream with 1-3 actionable insights using the "
        "active model, and store it in the dream journal. Use when the user asks Hermes "
        "to dream, or after learning something worth consolidating."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topics_hint": {
                "type": "string",
                "description": "Optional themes to steer the dream toward (e.g. 'the failed backup job')",
            },
        },
        "required": [],
    },
}

DREAM_JOURNAL = {
    "name": "dream_journal",
    "description": (
        "Read recent dreams from the dream journal: title, narrative and the insights "
        "each dream produced. Use when the user asks about dreams or past self-reflection."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "How many dreams to return (default 5, max 20)",
            },
        },
        "required": [],
    },
}

DREAM_STATUS = {
    "name": "dream_status",
    "description": (
        "Show the dreamer plugin status: whether dreaming is enabled, when the last dream "
        "happened, how idle Hermes currently is, and whether a dream residue is waiting "
        "to be injected into the next fresh session."
    ),
    "parameters": {"type": "object", "properties": {}, "required": []},
}
