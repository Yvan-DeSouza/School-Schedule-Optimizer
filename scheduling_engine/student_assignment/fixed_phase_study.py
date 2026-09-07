"""Crash-safe artifact primitives for fixed-family research branches.

This module is research infrastructure only. It does not load Django state,
build scheduling models, run CP-SAT, validate candidates, or choose an
operator. The branch worker supplies those facts through the existing
``FixedFamilyPhaseController`` and the canonical checkpoint writer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import gzip
import json
import os
from pathlib import Path
from time import sleep, time


STUDY_MANIFEST_SCHEMA = "v2_r16_r4_hybrid_study_manifest_v1"
BRANCH_STATE_SCHEMA = "fixed_family_branch_state_v1"
ATTEMPT_EVENT_SCHEMA = "fixed_family_attempt_v1"
SEALED_SCHEMA = "fixed_family_study_seal_v1"


@dataclass(frozen=True)
class FixedPhaseStudyContract:
    """The immutable experiment contract written before branch execution."""

    lineage_id: str
    source_path: str
    source_file_sha256: str
    source_fingerprint: str
    materialized_source_fingerprint: str
    input_fingerprint: str
    model_fingerprint: str
    objective_semantics_version: str = "v2"
    branch_seconds: float = 10_800.0
    phase_switch_seconds: float = 3_600.0
    search_seconds: float = 300.0
    validation_seconds: float = 180.0
    worker_count: int = 8
    validation_worker_count: int = 1
    cp_sat_seed: int = 101
    branch_order: tuple[str, ...] = ("r16_only", "hybrid")

    def to_dict(self):
        return asdict(self)


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_bytes(payload):
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=repr,
    ).encode("utf-8")


def atomic_bytes(path, payload):
    """Atomically publish bytes and never overwrite a different directory."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("wb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    replace_error = None
    for _ in range(20):
        try:
            os.replace(temporary, path)
            replace_error = None
            break
        except PermissionError as error:
            replace_error = error
            sleep(0.05)
    if replace_error is not None:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise replace_error
    try:
        directory_fd = os.open(path.parent, os.O_RDONLY)
    except (OSError, AttributeError):
        directory_fd = None
    if directory_fd is not None:
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def atomic_json(path, payload):
    atomic_bytes(path, _json_bytes(payload))


def gzip_json_bytes(payload):
    return gzip.compress(_json_bytes(payload), mtime=0)


def create_lineage(
    root,
    *,
    lineage_id,
    confirm_lineage_id=None,
    historical_roots=(),
    forbidden_roots=(),
):
    """Create an exclusive, empty study root with path safety checks."""

    if confirm_lineage_id is not None and str(confirm_lineage_id) != str(lineage_id):
        raise ValueError("lineage confirmation does not match lineage_id")
    root = Path(root).resolve()
    if root.exists():
        raise FileExistsError(f"research lineage already exists: {root}")
    for forbidden in tuple(historical_roots) + tuple(forbidden_roots):
        forbidden = Path(forbidden).resolve()
        if (
            root == forbidden
            or forbidden in root.parents
            or root in forbidden.parents
        ):
            raise ValueError("lineage path overlaps a forbidden research root")
    root.mkdir(parents=True, exist_ok=False)
    lock = root / "lineage.lock"
    with lock.open("x", encoding="utf-8") as stream:
        stream.write(str(lineage_id))
        stream.flush()
        os.fsync(stream.fileno())
    for directory in (
        "code",
        "source",
        "branches/r16_only/attempts",
        "branches/r16_only/checkpoints",
        "branches/r16_only/selectors",
        "branches/r16_only/targeting",
        "branches/hybrid/attempts",
        "branches/hybrid/checkpoints",
        "branches/hybrid/selectors",
        "branches/hybrid/targeting",
        "analysis",
        "report",
    ):
        (root / directory).mkdir(parents=True, exist_ok=False)
    return root


class FixedPhaseArtifactWriter:
    """Persist branch events and checkpoints with bounded, atomic writes."""

    def __init__(self, root, *, contract: FixedPhaseStudyContract, branch_id):
        self.root = Path(root).resolve()
        self.contract = contract
        self.branch_id = str(branch_id)
        self.branch_root = self.root / "branches" / self.branch_id
        self.attempt_number = 0
        self._event_path = self.branch_root / "phase_events.jsonl"
        self._resource_path = self.branch_root / "resource_samples.jsonl"

    def write_manifest(self, *, status="planned", extra=None):
        payload = {
            "schema": STUDY_MANIFEST_SCHEMA,
            "status": str(status),
            "created_at_epoch_seconds": time(),
            "contract": self.contract.to_dict(),
            "branch_id": self.branch_id,
            **dict(extra or {}),
        }
        atomic_json(self.root / "study_manifest.json", payload)
        atomic_json(self.branch_root / "branch_manifest.json", payload)
        return payload

    def append_jsonl(self, path, payload):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(_json_bytes(payload).decode("utf-8"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def record_event(self, event):
        payload = event.to_dict() if hasattr(event, "to_dict") else dict(event)
        self.append_jsonl(self._event_path, payload)
        attempt = payload.get("attempt") or {}
        if attempt:
            self.attempt_number += 1
            attempt_payload = {
                "schema": ATTEMPT_EVENT_SCHEMA,
                "lineage_id": self.contract.lineage_id,
                "branch_id": self.branch_id,
                "attempt_index": self.attempt_number,
                **payload,
            }
            atomic_json(
                self.branch_root
                / "attempts"
                / f"attempt_{self.attempt_number:04d}.json",
                attempt_payload,
            )
        return payload

    def record_resource_sample(self, sample):
        self.append_jsonl(self._resource_path, dict(sample))

    def write_state(self, controller_snapshot, *, status="running"):
        payload = {
            "schema": BRANCH_STATE_SCHEMA,
            "status": str(status),
            "lineage_id": self.contract.lineage_id,
            "branch_id": self.branch_id,
            "contract_fingerprint": hashlib.sha256(
                _json_bytes(self.contract.to_dict())
            ).hexdigest(),
            "controller": dict(controller_snapshot),
        }
        atomic_json(self.branch_root / "branch_state.json", payload)
        return payload

    def write_checkpoint(self, checkpoint_name, payload):
        """Publish one incumbent checkpoint and its SHA-256 sidecar."""

        checkpoint = (self.branch_root / "checkpoints" / str(checkpoint_name)).resolve()
        if self.branch_root.resolve() not in checkpoint.parents:
            raise ValueError("checkpoint must remain inside the branch directory")
        encoded = payload if isinstance(payload, bytes) else _json_bytes(payload)
        atomic_bytes(checkpoint, encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        sidecar = checkpoint.with_name(f"{checkpoint.name}.sha256")
        atomic_bytes(sidecar, f"{digest}  {checkpoint.name}\n".encode("ascii"))
        return {
            "path": checkpoint.relative_to(self.root).as_posix(),
            "sha256": digest,
            "sidecar": sidecar.relative_to(self.root).as_posix(),
        }

    def register_checkpoint(self, checkpoint_name):
        """Hash a checkpoint already written by the canonical checkpoint writer."""

        checkpoint = (self.branch_root / "checkpoints" / str(checkpoint_name)).resolve()
        if self.branch_root.resolve() not in checkpoint.parents or not checkpoint.is_file():
            raise ValueError("checkpoint must be an existing file inside the branch")
        digest = sha256_file(checkpoint)
        sidecar = checkpoint.with_name(f"{checkpoint.name}.sha256")
        atomic_bytes(sidecar, f"{digest}  {checkpoint.name}\n".encode("ascii"))
        return {
            "path": checkpoint.relative_to(self.root).as_posix(),
            "sha256": digest,
            "sidecar": sidecar.relative_to(self.root).as_posix(),
        }

    def write_targeting_snapshot(self, attempt_number, payload):
        """Write one compressed immutable targeting snapshot."""

        raw = gzip_json_bytes(payload)
        name = f"attempt_{int(attempt_number):04d}_targeting.json.gz"
        path = (self.branch_root / "targeting" / name).resolve()
        if self.branch_root.resolve() not in path.parents:
            raise ValueError("targeting snapshot must remain inside the branch")
        atomic_bytes(path, raw)
        return {
            "path": path.relative_to(self.root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "compressed_bytes": len(raw),
        }

    def write_hash_manifest(self):
        """Hash every published artifact before the final seal."""

        manifest = hash_tree(self.root)
        atomic_bytes(self.root / "artifact_hashes.sha256", manifest.encode("utf-8"))
        return manifest

    def write_seal(self, *, status="complete"):
        manifest_path = self.root / "artifact_hashes.sha256"
        if not manifest_path.exists():
            raise FileNotFoundError(
                "write_hash_manifest must run before the final seal"
            )
        manifest_sha256 = sha256_file(manifest_path)
        payload = {
            "schema": SEALED_SCHEMA,
            "status": str(status),
            "lineage_id": self.contract.lineage_id,
            "artifact_hashes_sha256": manifest_sha256,
            "sealed_at_epoch_seconds": time(),
        }
        atomic_json(self.root / "SEALED", payload)
        return payload


def hash_tree(root, *, exclude=("artifact_hashes.sha256", "SEALED")):
    """Return stable relative-path hashes before the final seal."""

    root = Path(root).resolve()
    excluded = set(exclude)
    entries = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        if relative in excluded:
            continue
        entries.append(f"{sha256_file(path)}  {relative}")
    return "\n".join(entries) + ("\n" if entries else "")


__all__ = [
    "ATTEMPT_EVENT_SCHEMA",
    "BRANCH_STATE_SCHEMA",
    "FixedPhaseArtifactWriter",
    "FixedPhaseStudyContract",
    "SEALED_SCHEMA",
    "STUDY_MANIFEST_SCHEMA",
    "atomic_bytes",
    "atomic_json",
    "create_lineage",
    "hash_tree",
    "sha256_file",
]
