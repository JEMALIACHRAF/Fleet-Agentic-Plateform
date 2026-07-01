"""Tool registry. A skill declares the tools it needs by name; the skill loader
validates every declared tool exists here *before* building an agent. That turns
"agent calls a tool that doesn't exist" from a runtime crash into a load-time
error — a small thing that buys a lot of reliability."""
from __future__ import annotations

from collections.abc import Callable

from ..config import Settings, get_settings
from .bigquery_tool import VehicleDataTool
from .ticket_tool import TicketDataTool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Callable] = {} 

    def register(self, name: str, fn: Callable) -> None:
        if name in self._tools:
            raise ValueError(f"tool already registered: {name}")
        self._tools[name] = fn

    def get(self, name: str) -> Callable:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)


def default_registry(settings: Settings | None = None) -> ToolRegistry:
    s = settings or get_settings()
    data = VehicleDataTool(s)
    reg = ToolRegistry()
    reg.register("get_vehicle_readings", data.get_vehicle_readings)
    tickets = TicketDataTool(s)
    reg.register("get_customer_ticket", tickets.get_customer_ticket)
    return reg
