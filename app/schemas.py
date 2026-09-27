from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ItemIn(BaseModel):
    """Request body for POST and PUT (PUT replaces the whole item)."""

    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class ItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None
    created_at: datetime
