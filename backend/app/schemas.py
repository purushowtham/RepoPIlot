from typing import Literal
from pydantic import BaseModel, Field, ConfigDict, SecretStr


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Credentials(Strict):
    email: str = Field(min_length=5, max_length=254, pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
    password: str = Field(min_length=10, max_length=128)


class RepoInput(Strict):
    full_name: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=200)


class RunInput(Strict):
    repository_id: str
    issue_text: str = Field(min_length=15, max_length=8000)
    base_ref: str = Field(default="main", min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_./-]+$")
    issue_number: int | None = Field(default=None, ge=1)


class Decision(Strict):
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=2000)


class Plan(Strict):
    summary: str = Field(max_length=2000)
    acceptance_criteria: list[str] = Field(min_length=1, max_length=10)
    steps: list[str] = Field(min_length=1, max_length=10)
    risks: list[str] = Field(max_length=10)


class Patch(Strict):
    diff: str = Field(min_length=1, max_length=100000)
    explanation: str = Field(max_length=4000)


class Finding(Strict):
    severity: Literal["info", "warning", "critical"]
    file_path: str | None = None
    line: int | None = Field(default=None, ge=1)
    description: str = Field(max_length=2000)
    blocking: bool


class Review(Strict):
    summary: str = Field(max_length=2000)
    findings: list[Finding] = Field(max_length=20)


class ConnectionInput(Strict):
    model: str | None = Field(default=None, min_length=1, max_length=150, pattern=r"^[A-Za-z0-9_.:/-]+$")
    llm_api_key: SecretStr | None = Field(default=None, max_length=1000)
    github_token: SecretStr | None = Field(default=None, max_length=1000)
