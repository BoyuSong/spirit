from pathlib import Path
import yaml
from pydantic import BaseModel, Field


class ModelConfig(BaseModel):
    provider: str
    model: str
    host: str = "http://127.0.0.1:11434"


class RegistryConfig(BaseModel):
    models: dict[str, ModelConfig] = Field(default_factory=dict)


class ModelRegistry:
    def __init__(self, config_path: Path):
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        self.config = RegistryConfig.model_validate(raw) #returns RegistryConfig

    def get(self, role: str) -> ModelConfig:
        try:
            return self.config.models[role]
        except KeyError as exc:
            raise ValueError(f"Unknown model role: {role}") from exc
