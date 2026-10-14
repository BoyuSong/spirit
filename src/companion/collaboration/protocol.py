from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


PREFIX = "ai-companion:v1\n"
MAX_BYTES = 512 * 1024


class Envelope(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    kind: Literal["task", "result", "error"]
    job_id: UUID
    task_id: UUID
    sender: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    recipient: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    step: int = Field(ge=0, lt=16)
    expires_at: float = Field(gt=0, allow_inf_nan=False)
    content: str = Field(min_length=1, max_length=32000)
    context: dict[str, str] = Field(default_factory=dict, max_length=16)

    def encode(self) -> bytes:
        data = self.model_dump_json().encode("utf-8")
        if len(data) > MAX_BYTES:
            raise ValueError("Discord task payload exceeds 512 KiB")
        return data

    @classmethod
    def decode(cls, data: bytes):
        if len(data) > MAX_BYTES:
            raise ValueError("Discord task payload exceeds 512 KiB")
        return cls.model_validate_json(data)
