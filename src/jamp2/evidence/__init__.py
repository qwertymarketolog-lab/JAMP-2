"""Canonical evidence records and integrity primitives."""

from jamp2.evidence.digest import compute_record_hash, compute_sha256
from jamp2.evidence.model import EvidenceRecord

__all__ = ["EvidenceRecord", "compute_record_hash", "compute_sha256"]
