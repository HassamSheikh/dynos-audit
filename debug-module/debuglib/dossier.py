"""
Dossier assembly — AC18.

Assembles a deterministic evidence dossier from pipeline outputs by minting
sequential, type-prefixed, zero-padded IDs and indexing every payload.

Counters are LOCAL to each `assemble()` call. Calling `assemble()` twice with
the same inputs always yields the same IDs (no module-level mutable state).

ID prefixes:
    MF-NNN  manifest / file-level linter findings
    F-NNN   symbol / frame findings
    S-NNN   Semgrep findings
    T-NNN   test findings
    ST-NNN  stack trace frames
    CG-NNN  coverage gaps
"""
from __future__ import annotations

import uuid
from typing import Any


# Public ID prefix constants — kept here so callers can reference them without
# duplicating string literals.
PREFIX_MANIFEST = "MF"
PREFIX_FRAME = "F"
PREFIX_SEMGREP = "S"
PREFIX_TEST = "T"
PREFIX_STACK = "ST"
PREFIX_COVERAGE_GAP = "CG"


def _format_id(prefix: str, n: int) -> str:
    """Format a prefix + integer counter as a zero-padded 3-digit ID."""
    if n < 0:
        raise ValueError(f"counter must be non-negative, got {n}")
    return f"{prefix}-{n:03d}"


def _mint_block(
    prefix: str,
    items: list[Any] | None,
    evidence_index: dict[str, Any],
) -> list[str]:
    """
    Mint sequential IDs for every item in `items`, register each payload in
    `evidence_index`, and return the list of minted IDs (in the same order as
    the input list).

    A fresh counter starting at 1 is used for this call. None or an empty list
    yields no IDs.
    """
    if not items:
        return []
    if not isinstance(items, list):
        # Defensive: tolerate non-list iterables (e.g. tuples) but reject
        # anything that isn't iterable in a structured way.
        try:
            items = list(items)
        except TypeError:
            return []

    minted: list[str] = []
    for i, payload in enumerate(items, start=1):
        eid = _format_id(prefix, i)
        # Wrap the raw payload with its minted ID so downstream consumers can
        # always recover the citation key from the value alone.
        evidence_index[eid] = {
            "id": eid,
            "kind": prefix,
            "payload": payload,
        }
        minted.append(eid)
    return minted


def assemble(pipeline_outputs: dict | None) -> dict:
    """
    Assemble an evidence dossier from pipeline outputs.

    All keys of `pipeline_outputs` are optional; missing keys default to empty
    structures. The returned dossier is a fresh dict with no shared mutable
    state across calls.

    Counters reset to 1 per prefix on EACH call (no module-level globals).
    """
    if pipeline_outputs is None:
        pipeline_outputs = {}
    if not isinstance(pipeline_outputs, dict):
        raise TypeError(
            f"pipeline_outputs must be a dict, got {type(pipeline_outputs).__name__}"
        )

    # Local counters/state — guaranteed reset between calls.
    evidence_index: dict[str, Any] = {}

    linter_findings = pipeline_outputs.get("linter_findings") or []
    stack_frames = pipeline_outputs.get("stack_frames") or []
    semgrep_findings = pipeline_outputs.get("semgrep_findings") or []
    test_results = pipeline_outputs.get("test_results") or []
    coverage_gaps = pipeline_outputs.get("coverage_gaps") or []
    mentioned_symbols = pipeline_outputs.get("mentioned_symbols") or []

    # Order is deterministic and stable across calls. Each block uses its own
    # counter starting at 1.
    mf_ids = _mint_block(PREFIX_MANIFEST, linter_findings, evidence_index)
    f_ids = _mint_block(PREFIX_FRAME, mentioned_symbols, evidence_index)
    s_ids = _mint_block(PREFIX_SEMGREP, semgrep_findings, evidence_index)
    t_ids = _mint_block(PREFIX_TEST, test_results, evidence_index)
    st_ids = _mint_block(PREFIX_STACK, stack_frames, evidence_index)
    cg_ids = _mint_block(PREFIX_COVERAGE_GAP, coverage_gaps, evidence_index)

    # investigation_id: prefer caller-supplied, otherwise mint a new one. Reject
    # non-string overrides so callers cannot smuggle in non-serialisable data.
    raw_inv_id = pipeline_outputs.get("investigation_id")
    if isinstance(raw_inv_id, str) and raw_inv_id.strip():
        investigation_id = raw_inv_id
    else:
        investigation_id = f"INV-{uuid.uuid4().hex[:12]}"

    # Coerce optional metadata to safe defaults so downstream renderers never
    # have to handle None.
    bug_type = pipeline_outputs.get("bug_type") or "unknown"
    bug_text = pipeline_outputs.get("bug_text") or ""
    repo_path = pipeline_outputs.get("repo_path") or ""
    languages_detected = pipeline_outputs.get("languages") or []
    pipeline_errors = pipeline_outputs.get("pipeline_errors") or []

    if not isinstance(pipeline_errors, list):
        pipeline_errors = [str(pipeline_errors)]
    if not isinstance(languages_detected, list):
        languages_detected = [str(languages_detected)]

    dossier = {
        "investigation_id": investigation_id,
        "bug_type": str(bug_type),
        "bug_text": str(bug_text),
        "repo_path": str(repo_path),
        "languages_detected": [str(x) for x in languages_detected],
        "pipeline_errors": [str(x) for x in pipeline_errors],
        "evidence_index": evidence_index,
        # Surface the per-prefix ID lists so callers can iterate in original
        # order without re-sorting the index keys.
        "evidence_ids_by_kind": {
            PREFIX_MANIFEST: mf_ids,
            PREFIX_FRAME: f_ids,
            PREFIX_SEMGREP: s_ids,
            PREFIX_TEST: t_ids,
            PREFIX_STACK: st_ids,
            PREFIX_COVERAGE_GAP: cg_ids,
        },
        # Pass through useful raw context (defensive copies — never share refs
        # with the caller's pipeline_outputs dict).
        "stack_frames": list(stack_frames) if isinstance(stack_frames, list) else [],
        "git_forensics": dict(pipeline_outputs.get("git_forensics") or {}),
        "log_entries": list(pipeline_outputs.get("log_entries") or []),
        "schema_drift": list(pipeline_outputs.get("schema_drift") or []),
        "mentioned_files": list(pipeline_outputs.get("mentioned_files") or []),
    }

    return dossier
