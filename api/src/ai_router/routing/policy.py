"""Routing policy loaded from YAML: departments, their descriptions and disambiguation rules.

The policy is data, not code: the default file ships with the package (`policy.yaml`) and can be
replaced at runtime with `ROUTING_POLICY_FILE`. It is validated at startup (fail fast), and its
content hash is part of the prompt version recorded with every routing decision.
"""

import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Self

import yaml
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

_EMAIL_IN_TEXT = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


class DepartmentSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    address: EmailStr
    name: str = Field(min_length=1)
    name_pl: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    not_here: str = Field(min_length=1)
    keywords_pl: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    fallback: bool = False

    @field_validator("address")
    @classmethod
    def _lowercase(cls, value: str) -> str:
        return value.lower()

    @property
    def local_part(self) -> str:
        return self.address.split("@", 1)[0]


class PolicySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    departments: tuple[DepartmentSpec, ...] = Field(min_length=2)
    decision_rules: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _consistency(self) -> Self:
        keys = [d.key for d in self.departments]
        addresses = [d.address for d in self.departments]
        if len(set(keys)) != len(keys):
            raise ValueError("department keys must be unique")
        if len(set(addresses)) != len(addresses):
            raise ValueError("department addresses must be unique")
        if sum(d.fallback for d in self.departments) != 1:
            raise ValueError("exactly one department must have `fallback: true`")
        known = set(addresses)
        for rule in self.decision_rules:
            unknown = {a.lower() for a in _EMAIL_IN_TEXT.findall(rule)} - known
            if unknown:
                raise ValueError(f"decision rule mentions unknown address(es): {sorted(unknown)}")
        return self


@dataclass(frozen=True)
class RoutingPolicy:
    """Validated, immutable routing policy used by the agent, prompts, API docs and evaluation."""

    spec: PolicySpec
    source: str

    @property
    def version(self) -> str:
        return self.spec.version

    @property
    def departments(self) -> tuple[DepartmentSpec, ...]:
        return self.spec.departments

    @property
    def decision_rules(self) -> tuple[str, ...]:
        return self.spec.decision_rules

    @property
    def addresses(self) -> tuple[str, ...]:
        return tuple(d.address for d in self.spec.departments)

    @property
    def fallback(self) -> DepartmentSpec:
        return next(d for d in self.spec.departments if d.fallback)

    @property
    def digest(self) -> str:
        """Hash of the policy content (part of the prompt version)."""
        canonical = json.dumps(
            self.spec.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:12]

    def get(self, address: str) -> DepartmentSpec | None:
        address = address.lower()
        return next((d for d in self.spec.departments if d.address == address), None)

    def require(self, address: str) -> DepartmentSpec:
        department = self.get(address)
        if department is None:
            raise KeyError(address)
        return department

    def by_key(self, key: str) -> DepartmentSpec:
        return next(d for d in self.spec.departments if d.key == key)

    def json_schema(self) -> dict[str, Any]:
        """Flat enum schema - Ollama's tool-schema parser drops `$ref` and `const`."""
        return {"type": "string", "enum": list(self.addresses)}

    def normalize(self, value: object) -> object:
        """Lenient pre-validation of a department chosen by the model.

        Accepts `" HELP-DESK@EXAMPLE.COM "`, `"help-desk"`, `"HELP_DESK"` or the department key.
        Values that still do not match are returned unchanged and fail validation, so Pydantic AI
        asks the model to correct itself (the error lists the allowed addresses).
        """
        if not isinstance(value, str):
            return value
        candidate = value.strip().strip("\"'<>").lower()
        if "@" in candidate:
            return candidate
        dashed = candidate.replace("_", "-")
        for department in self.spec.departments:
            if dashed in (department.local_part, department.key.replace("_", "-")):
                return department.address
        return candidate

    def validate_address(self, value: str) -> str:
        if self.get(value) is None:
            allowed = ", ".join(self.addresses)
            raise ValueError(f"department must be one of: {allowed}")
        return value.lower()

    @classmethod
    def from_mapping(cls, data: Any, source: str = "<memory>") -> "RoutingPolicy":
        return cls(spec=PolicySpec.model_validate(data), source=source)


def load_policy(path: str | None = None) -> RoutingPolicy:
    """Load and validate the policy (default: the file shipped with the package)."""
    if path:
        text = Path(path).read_text(encoding="utf-8")
        source = str(path)
    else:
        text = resources.files("ai_router.routing").joinpath("policy.yaml").read_text("utf-8")
        source = "ai_router/routing/policy.yaml"
    return RoutingPolicy.from_mapping(yaml.safe_load(text), source=source)
