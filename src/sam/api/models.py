"""Public API response models."""

from pydantic import BaseModel, Field


class ErrorResponse(BaseModel):
    """Stable error shape exposed by the API."""

    class ErrorDetail(BaseModel):
        code: str = Field(min_length=1)
        message: str = Field(min_length=1)

    error: ErrorDetail