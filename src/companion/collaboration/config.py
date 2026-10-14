from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml


class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_role: str = Field(min_length=1, max_length=64)
    token_env: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    prompt: str = Field(min_length=1, max_length=8000)


class CollaborationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    agents: dict[str, AgentSpec] = Field(min_length=1, max_length=32)
    coordinator: str = "coordinator"
    channel_env: str = Field(default="DISCORD_CHANNEL_ID", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    workflow: list[str] = Field(min_length=1, max_length=16)
    poll_interval: float = Field(default=2.0, ge=0.5, le=60, allow_inf_nan=False)
    task_timeout: float = Field(default=600.0, gt=0, le=3600, allow_inf_nan=False)
    context_limit: int = Field(default=64000, gt=0, le=64000)

    @model_validator(mode="after")
    def validate_roles(self):
        if any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", role) for role in self.agents):
            raise ValueError("Invalid agent role name")
        if self.coordinator not in self.agents:
            raise ValueError("Coordinator must be a registered agent")
        if any(role not in self.agents for role in self.workflow):
            raise ValueError("Workflow references an unregistered agent")
        if self.workflow[-1] != self.coordinator:
            raise ValueError("Workflow must end with the coordinator")
        if len({spec.token_env for spec in self.agents.values()}) != len(self.agents):
            raise ValueError("Each agent needs its own bot token binding")
        return self

    @classmethod
    def load(cls, path: Path):
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
