from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator


class ExperimentSettings(BaseModel):
    name: str = Field(min_length=1)
    participants: Literal[3]
    rounds: Literal[2]
    round_duration_seconds: int = Field(ge=5, le=3600)


class ParticipantSettings(BaseModel):
    id: str = Field(pattern=r"^[A-C]$")
    type: Literal["human", "ai"]


class AISettings(BaseModel):
    enabled: bool
    strategy: Literal["simple", "groq"]
    response_delay_seconds: int = Field(default=2, ge=0, le=10)


class AppSettings(BaseModel):
    experiment: ExperimentSettings
    participants: list[ParticipantSettings]
    ai_agent: AISettings

    @model_validator(mode="after")
    def validate_roster(self):
        if len(self.participants) != 3:
            raise ValueError("exactly three participant entries are required")
        ids = [participant.id for participant in self.participants]
        if len(set(ids)) != 3:
            raise ValueError("participant identifiers must be unique")
        if set(ids) != {"A", "B", "C"}:
            raise ValueError("participant identifiers must be A, B, and C")
        if not any(p.type == "human" for p in self.participants):
            raise ValueError("at least one human participant must be configured")
        if any(p.type == "ai" for p in self.participants) and not self.ai_agent.enabled:
            raise ValueError("ai_agent.enabled must be true when an AI participant is configured")
        return self


def load_config(path: str | Path) -> AppSettings:
    path = Path(path)
    try:
        with path.open(encoding="utf-8") as config_file:
            raw = yaml.safe_load(config_file)
        return AppSettings.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError, TypeError) as exc:
        raise ValueError(f"Invalid experiment configuration at {path}: {exc}") from exc
