"""
Pydantic request/response schemas for IonShield API.
"""
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator


class Waypoint(BaseModel):
    lat: float = Field(..., ge=-90, le=90, description="Latitude in decimal degrees")
    lon: float = Field(..., ge=-180, le=180, description="Longitude in decimal degrees")
    name: Optional[str] = Field(None, max_length=128)

    @field_validator("name", mode="before")
    @classmethod
    def sanitize_name(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        # Strip control characters and limit length
        return "".join(c for c in str(v) if c.isprintable())[:128]


class RouteRequest(BaseModel):
    waypoints: List[Waypoint] = Field(..., min_length=1)
    asset_type: str = Field(
        default="GPS_L1",
        description=(
            "GPS receiver capability. Options: GPS_L1 (L1 C/A only), "
            "GPS_L1L2 (dual-freq, iono-corrected), GPS_L1L5 (modernized dual-freq), "
            "GPS_INS (GPS-aided inertial), SBAS (WAAS/EGNOS corrected)"
        ),
    )

    @field_validator("asset_type", mode="before")
    @classmethod
    def validate_asset_type(cls, v: str) -> str:
        allowed = {"GPS_L1", "GPS_L1L2", "GPS_L1L5", "GPS_INS", "SBAS"}
        if str(v).upper() not in allowed:
            return "GPS_L1"
        return str(v).upper()
