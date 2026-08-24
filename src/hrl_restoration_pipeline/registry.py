from __future__ import annotations

import csv, hashlib, json
from abc import ABC, abstractmethod
from pathlib import Path


class ProjectIdRegistry(ABC):
    @abstractmethod
    def eligible(self, project_id: str) -> bool: ...
    @abstractmethod
    def provenance(self) -> dict[str, str]: ...


class SnapshotRegistry(ProjectIdRegistry):
    """Read-only CSV/JSON export adapter; never allocates or mutates IDs."""
    def __init__(self, export: Path, manifest: Path):
        self.export, self.manifest = export, manifest
        data = json.loads(manifest.read_text())
        for key in ("export_version", "source_registry", "checksums"):
            if key not in data:
                raise ValueError(f"registry manifest missing {key}")
        digest = hashlib.sha256(export.read_bytes()).hexdigest()
        expected = data["checksums"].get(export.name)
        if expected != digest:
            raise ValueError("registry export checksum does not match manifest")
        self.data, self.checksum = data, digest
        if export.suffix == ".json":
            rows = json.loads(export.read_text())
        elif export.suffix == ".csv":
            rows = list(csv.DictReader(export.open(newline="")))
        else:
            raise ValueError("registry export must be CSV or JSON")
        self.rows = {str(r["project_id"]): r for r in rows}

    def eligible(self, project_id: str) -> bool:
        return project_id in self.rows and self.rows[project_id].get("status") == "eligible"

    def provenance(self) -> dict[str, str]:
        return {"source": self.data["source_registry"], "version": self.data["export_version"], "checksum": self.checksum}
