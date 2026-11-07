"""Structured output schemas (§12)."""

SCRIPT_SCHEMA = {
    "type": "object",
    "required": ["headline", "segments"],
    "properties": {
        "headline": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["text", "fact_ids"],
                "properties": {
                    "text": {"type": "string"},
                    "fact_ids": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}
