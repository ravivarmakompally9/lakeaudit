"""Every output row must be regenerable six months later. This module produces the
identifiers that make that possible: a run id, the code revision, and a hash of
the exact configuration used."""
from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from . import __version__


def _git_commit(repo_root: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short=10", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            dirty = subprocess.run(
                ["git", "-C", str(repo_root), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return out.stdout.strip() + ("-dirty" if dirty else "")
    except Exception:
        pass
    return "nogit"


def _hash_files(paths: list[Path]) -> str:
    h = hashlib.sha256()
    for p in paths:
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Provenance:
    run_id: str
    started_utc: str
    code_version: str
    code_commit: str
    params_hash: str
    config_files: list[str]

    def as_dict(self) -> dict:
        return asdict(self)

    def stamp(self, row: dict) -> dict:
        """Attach provenance columns to any output record."""
        row = dict(row)
        row["run_id"] = self.run_id
        row["code_commit"] = self.code_commit
        row["params_hash"] = self.params_hash
        return row


def new_run(repo_root: Path, config_paths: list[Path]) -> Provenance:
    ts = datetime.now(timezone.utc)
    return Provenance(
        run_id=f"run_{ts.strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}",
        started_utc=ts.isoformat(timespec="seconds"),
        code_version=__version__,
        code_commit=_git_commit(repo_root),
        params_hash=_hash_files(config_paths),
        config_files=[str(p.relative_to(repo_root)) for p in config_paths],
    )


def write_run_manifest(prov: Provenance, out_dir: Path, extra: dict | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = prov.as_dict()
    if extra:
        manifest.update(extra)
    p = out_dir / "run.json"
    p.write_text(json.dumps(manifest, indent=2))
    return p
