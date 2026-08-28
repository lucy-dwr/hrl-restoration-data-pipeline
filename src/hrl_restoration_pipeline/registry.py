from __future__ import annotations

import csv
import hashlib
from abc import ABC, abstractmethod
from pathlib import Path


class ProjectIdRegistry(ABC):
    @abstractmethod
    def eligible(self, project_id: str) -> bool: ...
    @abstractmethod
    def provenance(self) -> dict[str, str]: ...


class CsvRegistry(ProjectIdRegistry):
    """Read-only view of ``project-id-registry.csv`` from ``hrl-project-registry``.

    The file is expected to be checked out at a known commit. It is never
    allocated, altered, or followed as a moving pointer. ``ref`` records which
    commit or tag the caller pinned, for the validation report.
    """

    def __init__(self, path: Path, ref: str | None = None):
        self.path = Path(path)
        if self.path.suffix != ".csv":
            raise ValueError("project ID registry must be a .csv file")
        with self.path.open(newline="", encoding="utf-8-sig") as file:
            rows = list(csv.DictReader(file))
        if not rows or "project_id" not in rows[0] or "status" not in rows[0]:
            raise ValueError("registry CSV must have at least project_id and status columns")
        self.rows = {str(row["project_id"]).strip(): row for row in rows}
        self.checksum = hashlib.sha256(self.path.read_bytes()).hexdigest()
        self.ref = ref or "unpinned"

    def eligible(self, project_id: str) -> bool:
        row = self.rows.get(project_id)
        return row is not None and (row.get("status") or "").strip() == "eligible"

    def provenance(self) -> dict[str, str]:
        return {
            "source": "hrl-project-registry/project-id-registry.csv",
            "version": self.ref,
            "checksum": self.checksum,
        }
