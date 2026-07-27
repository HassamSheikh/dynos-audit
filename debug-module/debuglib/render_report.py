"""
Markdown report renderer — AC19, AC20.

`render(bug_report, dossier)` produces a Markdown string that includes a
WARNING line for every cited evidence ID that is missing from
`dossier["evidence_index"]`. The function never raises on missing citations
— it warns inline and renders the rest of the report.

CLI form:
    python3 debug-module/debuglib/render_report.py \
        --report  <path-to-bug-report.json> \
        --dossier <path-to-dossier.json>

Writes Markdown to stdout, exits 0 on success.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable


def _collect_cited_ids(bug_report: dict) -> list[str]:
    """
    Walk a bug_report dict and return the ordered list of every evidence ID
    cited in causal_chain[*].evidence_ids, root_cause.evidence_ids, and
    recommended_fix.evidence_ids. Duplicates are preserved so warnings track
    the order they appear in the rendered output.
    """
    cited: list[str] = []

    causal_chain = bug_report.get("causal_chain") or []
    if isinstance(causal_chain, list):
        for step in causal_chain:
            if not isinstance(step, dict):
                continue
            ids = step.get("evidence_ids") or []
            if isinstance(ids, list):
                cited.extend(str(x) for x in ids)

    root_cause = bug_report.get("root_cause") or {}
    if isinstance(root_cause, dict):
        ids = root_cause.get("evidence_ids") or []
        if isinstance(ids, list):
            cited.extend(str(x) for x in ids)

    fix = bug_report.get("recommended_fix") or {}
    if isinstance(fix, dict):
        ids = fix.get("evidence_ids") or []
        if isinstance(ids, list):
            cited.extend(str(x) for x in ids)

    return cited


def _format_evidence_ids(ids: Iterable[Any], known_ids: set[str]) -> str:
    """
    Format a list of evidence IDs for inline display. Missing IDs are wrapped
    with `<ID>` (markdown still renders) and tracked separately by the caller
    via the WARNING block.
    """
    parts: list[str] = []
    for raw in ids or []:
        eid = str(raw)
        if eid in known_ids:
            parts.append(f"`{eid}`")
        else:
            parts.append(f"`{eid}` (missing)")
    return ", ".join(parts) if parts else "_none_"


def _render_warnings(cited_ids: Iterable[str], known_ids: set[str]) -> list[str]:
    """
    Build the WARNING lines for missing citations. Each missing ID gets exactly
    one warning, preserving first-appearance order.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for eid in cited_ids:
        if eid in known_ids:
            continue
        if eid in seen:
            continue
        seen.add(eid)
        lines.append(f"> WARNING: citation {eid} not found in dossier")
    return lines


def render(bug_report_json: dict, dossier: dict) -> str:
    """
    Render a Markdown bug report. Validates citations against
    `dossier["evidence_index"]` and emits a WARNING line for every missing ID.
    Never raises on invalid citations.
    """
    if not isinstance(bug_report_json, dict):
        raise TypeError(
            f"bug_report_json must be a dict, got {type(bug_report_json).__name__}"
        )
    if not isinstance(dossier, dict):
        raise TypeError(f"dossier must be a dict, got {type(dossier).__name__}")

    evidence_index = dossier.get("evidence_index") or {}
    if not isinstance(evidence_index, dict):
        evidence_index = {}
    known_ids: set[str] = {str(k) for k in evidence_index.keys()}

    cited_ids = _collect_cited_ids(bug_report_json)
    warning_lines = _render_warnings(cited_ids, known_ids)

    investigation_id = (
        bug_report_json.get("investigation_id")
        or dossier.get("investigation_id")
        or "INV-unknown"
    )
    bug_text = dossier.get("bug_text") or ""
    # bug_type lives canonically in the dossier (triage classification); the
    # bug report may refine it. Prefer the report's value when present so the
    # rendered header reflects the investigator's conclusion.
    bug_type = (
        bug_report_json.get("bug_type")
        or dossier.get("bug_type")
        or "unknown"
    )
    repo_path = dossier.get("repo_path") or ""
    languages = dossier.get("languages_detected") or []
    pipeline_errors = dossier.get("pipeline_errors") or []

    out: list[str] = []
    out.append(f"# Bug Report — {investigation_id}")
    out.append("")
    out.append(f"- **Bug type:** {bug_type}")
    out.append(f"- **Repo:** `{repo_path}`")
    if languages:
        out.append(f"- **Languages detected:** {', '.join(str(x) for x in languages)}")
    if bug_text:
        out.append("")
        out.append("## Bug description")
        out.append("")
        out.append(str(bug_text))

    # WARNING block — always rendered before other sections so reviewers see
    # citation issues first. Tests look for exact substring; we render one line
    # per missing ID inside a blockquote.
    if warning_lines:
        out.append("")
        out.append("## Citation warnings")
        out.append("")
        out.extend(warning_lines)

    # Causal chain
    out.append("")
    out.append("## Causal chain")
    out.append("")
    causal_chain = bug_report_json.get("causal_chain") or []
    if isinstance(causal_chain, list) and causal_chain:
        for step in causal_chain:
            if not isinstance(step, dict):
                continue
            n = step.get("step", "?")
            desc = step.get("description", "")
            ids = step.get("evidence_ids") or []
            out.append(f"{n}. {desc}")
            out.append(f"   - Evidence: {_format_evidence_ids(ids, known_ids)}")
    else:
        out.append("_No causal chain provided._")

    # Root cause
    out.append("")
    out.append("## Root cause")
    out.append("")
    root_cause = bug_report_json.get("root_cause") or {}
    if isinstance(root_cause, dict):
        out.append(str(root_cause.get("description", "")))
        out.append("")
        out.append(
            f"**Evidence:** {_format_evidence_ids(root_cause.get('evidence_ids') or [], known_ids)}"
        )
    else:
        out.append("_No root cause provided._")

    # Recommended fix
    out.append("")
    out.append("## Recommended fix")
    out.append("")
    fix = bug_report_json.get("recommended_fix") or {}
    if isinstance(fix, dict):
        out.append(str(fix.get("description", "")))
        locations = fix.get("locations") or []
        if isinstance(locations, list) and locations:
            out.append("")
            out.append("**Locations:**")
            for loc in locations:
                if not isinstance(loc, dict):
                    continue
                f_path = loc.get("file", "")
                line = loc.get("line", "")
                line_str = f":{line}" if line not in (None, "") else ""
                out.append(f"- `{f_path}{line_str}`")
        out.append("")
        out.append(
            f"**Evidence:** {_format_evidence_ids(fix.get('evidence_ids') or [], known_ids)}"
        )
    else:
        out.append("_No recommended fix provided._")

    # Pipeline errors (informational; not a citation)
    if pipeline_errors:
        out.append("")
        out.append("## Pipeline errors")
        out.append("")
        for err in pipeline_errors:
            out.append(f"- {err}")

    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> dict:
    """Load a JSON file or fail with a clear stderr message and exit code 2."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        print(f"error: file not found: {path}", file=sys.stderr)
        sys.exit(2)
    except json.JSONDecodeError as exc:
        print(f"error: invalid JSON in {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    except OSError as exc:
        print(f"error: could not read {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    if not isinstance(data, dict):
        print(
            f"error: {path} must contain a JSON object at the top level, "
            f"got {type(data).__name__}",
            file=sys.stderr,
        )
        sys.exit(2)
    return data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_report",
        description="Render a Markdown bug report from a bug-report JSON and dossier JSON.",
    )
    parser.add_argument(
        "--report",
        required=True,
        help="Path to the bug_report JSON file.",
    )
    parser.add_argument(
        "--dossier",
        required=True,
        help="Path to the evidence dossier JSON file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    dossier_path = Path(args.dossier)

    bug_report = _load_json(report_path)
    dossier = _load_json(dossier_path)

    try:
        markdown = render(bug_report, dossier)
    except Exception as exc:  # noqa: BLE001 — last-resort guard for CLI
        print(f"error: render failed: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
