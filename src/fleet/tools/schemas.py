"""Validated tool I/O schemas. Every tool returns a Pydantic model so an agent
never has to defend against a malformed dict — the shape is guaranteed at the
boundary, and a schema mismatch surfaces as a CONTEXT_FAILURE span, not a
mysterious KeyError three nodes downstream."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class VehicleReading(BaseModel):
    vehicle_id: str
    ts: datetime
    speed_kmh: float = Field(ge=0)
    engine_temp_c: float
    fuel_level_pct: float = Field(ge=0, le=100)
    odometer_km: float = Field(ge=0)
    lat: float
    lon: float


class VehicleReadings(BaseModel):
    """Guaranteed-shaped response from the data tool."""
    vehicle_id: str
    count: int
    source: str  # "bigquery" | "csv_cache"
    readings: list[VehicleReading]

    @property
    def is_empty(self) -> bool:
        return self.count == 0


class FleetSummary(BaseModel):
    vehicles: int
    total_readings: int
    avg_speed_kmh: float
    max_engine_temp_c: float
    source: str


class CustomerTicket(BaseModel):
    """Guaranteed-shaped customer support ticket (the ticket use case)."""
    ticket_id: str
    customer_tier: str          # "standard" | "enterprise"
    subject: str
    body: str
    created_at: str
    source: str = "csv_cache"   # "ticketing_api" | "csv_cache"
