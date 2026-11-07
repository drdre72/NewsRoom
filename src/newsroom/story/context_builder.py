"""Context builder — assembles the FACTS block the LLM is constrained by (§12).

The LLM sees ONLY this. If a number or claim isn't represented here, the
model must not say it, and the claim validator enforces that.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from ..intelligence.fact_ledger import FactLedger
from ..models.fact import Fact
from ..storage import repositories as repo


@dataclass
class FactContext:
    event_id: str
    revision: int
    current: list[Fact] = field(default_factory=list)
    all_facts: list[Fact] = field(default_factory=list)   # current + superseded
    revisions: list[dict] = field(default_factory=list)   # field: initial->current
    prompt_block: str = ""

    def fact_by_id(self) -> dict[str, Fact]:
        # Superseded facts are legitimate citation targets: a sentence about
        # a revised value cites the exact fact it describes.
        return {f.fact_id: f for f in self.all_facts}


def _friendly(field: str, value: str) -> str:
    return f"{field.replace('_', ' ')}: {value}"


def build_fact_context(conn: sqlite3.Connection, event_id: str) -> FactContext:
    event = repo.get_event(conn, event_id)
    ledger = FactLedger(conn)
    current = ledger.current(event_id)
    all_facts = repo.all_facts(conn, event_id)

    # Revision pairs for "was X, now Y" reporting (§10, §17).
    rev_summaries = []
    for f in current:
        summary = ledger.revision_summary(event_id, f.field)
        if summary:
            rev_summaries.append(summary)

    lines = [
        f"{f.fact_id} {_friendly(f.field, f.value_display)} "
        f"(source: {f.source}, confidence {f.confidence:.2f})"
        for f in current
    ]
    if rev_summaries:
        lines.append("")
        lines.append("REVISIONS (earlier values, now superseded):")
        for r in rev_summaries:
            lines.append(
                f"{r['initial_fact_id']}+{r['current_fact_id']} "
                f"{r['field'].replace('_', ' ')} was initially {r['initial']}; "
                f"now {r['current']}"
            )
    return FactContext(
        event_id=event_id,
        revision=event.current_revision if event else 0,
        current=current,
        all_facts=all_facts,
        revisions=rev_summaries,
        prompt_block="\n".join(lines),
    )
