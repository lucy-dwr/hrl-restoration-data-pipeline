from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class Finding:
    stage: str
    severity: str
    rule: str
    message: str
    record_id: str | None = None


@dataclass(frozen=True)
class Repair:
    record_id: str | None
    affected: str
    original: Any
    new: Any
    rule: str
    pipeline_version: str


@dataclass
class Report:
    submission_id: str | None
    registry: dict[str, str]
    schema: dict[str, str]
    pipeline_version: str
    validation_timestamp: str
    findings: list[Finding] = field(default_factory=list)
    repairs: list[Repair] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [x for x in self.findings if x.severity == "ERROR"]

    @property
    def warnings(self) -> list[Finding]:
        return [x for x in self.findings if x.severity == "WARNING"]

    @property
    def status(self) -> str:
        return "NEEDS_CORRECTION" if self.errors else "AWAITING_APPROVAL"

    def add(self, stage: str, severity: str, rule: str, message: str, record_id: str | None = None) -> None:
        self.findings.append(Finding(stage, severity, rule, message, record_id))

    def json(self) -> dict[str, Any]:
        return {"submission_id": self.submission_id, "status": self.status,
                "pipeline_version": self.pipeline_version, "schema": self.schema,
                "validation_timestamp": self.validation_timestamp,
                "registry": self.registry, "error_count": len(self.errors),
                "warning_count": len(self.warnings), "findings": [asdict(x) for x in self.findings],
                "repairs": [asdict(x) for x in self.repairs]}
