"""Small state-transition helpers."""

from __future__ import annotations

from typing import Any


def ticket_definition(definition: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    for ticket in definition["tickets"]:
        if ticket["id"] == ticket_id:
            return ticket
    raise KeyError(ticket_id)


def ticket_state(state: dict[str, Any], ticket_id: str) -> dict[str, Any]:
    for ticket in state["tickets"]:
        if ticket["id"] == ticket_id:
            return ticket
    raise KeyError(ticket_id)


def next_ready_ticket(definition: dict[str, Any], state: dict[str, Any]) -> str | None:
    completed = {
        ticket["id"] for ticket in state["tickets"] if ticket["state"] == "complete"
    }
    for ticket in definition["tickets"]:
        current = ticket_state(state, ticket["id"])
        if current["state"] == "pending" and set(ticket["depends_on"]) <= completed:
            return ticket["id"]
    return None
