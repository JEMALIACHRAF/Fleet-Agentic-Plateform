"""Customer-support ticket data tool.

Reads a local CSV as a stand-in for a real ticketing API (Zendesk / internal DB).
Returns a Pydantic-validated CustomerTicket so the shape is guaranteed at the boundary.

OBSERVABILITY (interview point)
-------------------------------
A ticket id that doesn't exist is a DATA failure (context_failure): the agent will
otherwise invent a classification on empty data and reply as if everything is fine.
So when the ticket is not found we RECORD a context_failure in Prometheus, but still
return a well-shaped "not_found" ticket so the workflow degrades gracefully instead
of crashing. Observe the failure without breaking the service — same pattern as the
BigQuery fallback in the vehicle tool.
"""
from __future__ import annotations

import csv
from pathlib import Path

from ..config import Settings, get_settings
from ..observability.failures import FailureType, traced_node
from .schemas import CustomerTicket


class TicketDataTool:
    def __init__(self, settings: Settings | None = None):
        self.s = settings or get_settings()
        self.path = Path("data/customer_tickets_sample.csv")

    def get_customer_ticket(self, ticket_id: str) -> CustomerTicket:
        """Return one ticket by id. Records a context_failure (but does not crash)
        when the id is not found, so a missing ticket is observable in the dashboard."""
        with traced_node("tool:get_customer_ticket", state_in={"ticket_id": ticket_id}):
            if self.path.exists():
                with self.path.open(newline="") as f:
                    for row in csv.DictReader(f):
                        if row["ticket_id"] == ticket_id:
                            return CustomerTicket(source="csv_cache", **row)
            # NOT FOUND -> record the data failure, then degrade gracefully.
            self._record_not_found(ticket_id)
            return CustomerTicket(
                ticket_id=ticket_id, customer_tier="unknown",
                subject="", body="", created_at="", source="not_found",
            )

    @staticmethod
    def _record_not_found(ticket_id: str) -> None:
        """Increment the typed failure metric for a missing ticket (context_failure).

        Typed as CONTEXT because the failure is 'could not retrieve the requested data'.
        Fires even though we still return gracefully — so Grafana shows missing-ticket
        lookups even when the agent replies normally."""
        try:
            from ..observability import metrics as M
            M.NODE_FAILURES.labels(
                node="tool:get_customer_ticket",
                graph="adk",
                type=FailureType.CONTEXT,
            ).inc()
        except Exception:
            pass  # observability must never break the tool
