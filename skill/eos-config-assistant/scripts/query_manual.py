#!/usr/bin/env python3
"""Wrapper for read-only EOS manual SQLite retrieval.

This script lets the skill query the project knowledge base even when the lab
folder is outside the assistant repository.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VARIANT_DB_NAMES = {
    "full": "eos_manual.sqlite",
    "slim": "eos_manual.slim.sqlite",
    "lite": "eos_manual.lite.sqlite",
}
VARIANT_ORDERS = {
    "auto": ["slim", "full", "lite"],
    "fast": ["lite", "slim", "full"],
    "full": ["full"],
    "slim": ["slim"],
    "lite": ["lite"],
}
VALID_VARIANTS = set(VARIANT_ORDERS)


@dataclass(frozen=True)
class DbCandidate:
    variant: str
    path: Path


@dataclass(frozen=True)
class DbSelection:
    requested_variant: str
    candidates: list[DbCandidate]
    used_override: bool = False


def candidate_roots(script_path: Path) -> list[Path]:
    roots: list[Path] = []
    env_root = os.environ.get("EOS_CONFIG_ASSISTANT_HOME")
    if env_root:
        roots.append(Path(env_root).expanduser())
    roots.extend([Path.cwd(), *Path.cwd().parents])
    resolved = script_path.resolve()
    roots.extend([resolved.parent, *resolved.parents])
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            marker = str(root.resolve())
        except OSError:
            marker = str(root)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(root)
    return unique


def find_project_root(script_path: Path) -> Path:
    for root in candidate_roots(script_path):
        if (root / "tools" / "query_knowledge_base.py").is_file() and (root / "knowledge").is_dir():
            return root
    raise SystemExit(
        "Could not locate eos-config-assistant project root. "
        "Set EOS_CONFIG_ASSISTANT_HOME to the repository path."
    )


def resolve_db_selection(root: Path, *, db_override: Path | None, variant: str) -> DbSelection:
    """Return existing DB candidates in request order.

    --db/EOS_MANUAL_DB are explicit overrides and disable variant ordering.
    """

    if db_override:
        return DbSelection("custom", [DbCandidate("custom", db_override.expanduser().resolve())], used_override=True)
    env_db = os.environ.get("EOS_MANUAL_DB")
    if env_db:
        return DbSelection("custom", [DbCandidate("custom", Path(env_db).expanduser().resolve())], used_override=True)

    requested = os.environ.get("EOS_MANUAL_DB_VARIANT", variant).strip().lower() or "auto"
    if requested not in VALID_VARIANTS:
        expected = ", ".join(["auto", "fast", "full", "slim", "lite"])
        raise SystemExit(f"invalid EOS manual DB variant: {requested!r}; expected {expected}")

    knowledge = root / "knowledge"
    order = VARIANT_ORDERS[requested]
    candidates = [DbCandidate(name, knowledge / VARIANT_DB_NAMES[name]) for name in order]
    return DbSelection(requested, candidates)


def existing_candidates(selection: DbSelection) -> list[DbCandidate]:
    return [candidate for candidate in selection.candidates if candidate.path.is_file()]


def passthrough_requests_json(passthrough: list[str]) -> bool:
    return "--json" in passthrough


def passthrough_with_json(passthrough: list[str]) -> list[str]:
    return passthrough if passthrough_requests_json(passthrough) else [*passthrough, "--json"]


def run_helper_json(helper: Path, root: Path, db: Path, passthrough: list[str]) -> tuple[int, dict[str, Any] | None, str, str]:
    cmd = [sys.executable, str(helper), "--db", str(db), *passthrough_with_json(passthrough)]
    completed = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True)
    result: dict[str, Any] | None = None
    if completed.returncode == 0:
        try:
            parsed = json.loads(completed.stdout)
            if isinstance(parsed, dict):
                result = parsed
        except json.JSONDecodeError:
            result = None
    return completed.returncode, result, completed.stdout, completed.stderr


def emit_result(result: dict[str, Any], *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    try:
        import yaml  # type: ignore
    except ImportError:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0


def annotate_selection(
    result: dict[str, Any],
    *,
    selection: DbSelection,
    selected: DbCandidate,
    attempted: list[DbCandidate],
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    result = dict(result)
    result["db_variant_selection"] = {
        "requested_variant": selection.requested_variant,
        "selected_variant": selected.variant,
        "selected_db": str(selected.path),
        "attempted_variants": [candidate.variant for candidate in attempted],
        "fallback_reason": fallback_reason,
    }
    return result


def has_body_chunk(result: dict[str, Any]) -> bool:
    for chunk in result.get("chunks") or []:
        if isinstance(chunk, dict) and chunk.get("body_available") and chunk.get("body"):
            return True
    return False


def auto_full_escalation_reason(result: dict[str, Any]) -> str | None:
    """Return the reason to rerun full, or None when the slim result is enough.

    In auto mode, slim is the practical first choice. If it cannot provide any
    retained body chunk, use full when available so guidance can still be
    grounded in source prose for pruned patch versions or sparse slim results.
    """

    compare = result.get("version_comparison")
    if isinstance(compare, dict) and not compare.get("records"):
        return "slim returned empty reduced version-diff result"
    if has_body_chunk(result):
        return None
    if result.get("chunks"):
        return "slim returned chunks without retained body text"
    if result.get("command_support"):
        return "slim returned command_support without retained body chunks"
    notes = "\n".join(str(note) for note in result.get("answering_notes") or [])
    if "full DB" in notes or "No precomputed SQLite chunks matched" in notes:
        return "slim returned no retained body chunks"
    return None


def should_escalate_auto_to_full(result: dict[str, Any]) -> bool:
    return auto_full_escalation_reason(result) is not None


def forward_raw(returncode: int, stdout: str, stderr: str) -> int:
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return returncode


def missing_db_error(selection: DbSelection) -> str:
    tried = "\n".join(f"  - {candidate.variant}: {candidate.path}" for candidate in selection.candidates)
    return (
        "missing SQLite DB for requested EOS manual variant.\n"
        f"requested_variant: {selection.requested_variant}\n"
        f"tried:\n{tried}\n"
        "Build it first with tools/ingest_pdf.py/tools/build_kb_variants.py, "
        "choose --variant auto|fast|full|slim|lite, or pass --db explicitly."
    )


def run_selected(helper: Path, root: Path, candidate: DbCandidate, passthrough: list[str]) -> int:
    cmd = [sys.executable, str(helper), "--db", str(candidate.path), *passthrough]
    return subprocess.call(cmd, cwd=str(root))


def run_auto_or_fast(
    helper: Path,
    root: Path,
    selection: DbSelection,
    candidates: list[DbCandidate],
    passthrough: list[str],
) -> int:
    as_json = passthrough_requests_json(passthrough)
    selected = candidates[0]

    if selection.requested_variant == "fast":
        # Fast means fastest available artifact by existence order. Do not rerun
        # full just because lite/slim returned metadata-only evidence.
        return run_selected(helper, root, selected, passthrough)

    if selected.variant != "slim":
        return run_selected(helper, root, selected, passthrough)

    full = next((candidate for candidate in candidates if candidate.variant == "full"), None)
    if full is None:
        return run_selected(helper, root, selected, passthrough)

    returncode, slim_result, stdout, stderr = run_helper_json(helper, root, selected.path, passthrough)
    if returncode != 0 or slim_result is None:
        return forward_raw(returncode, stdout, stderr)

    fallback_reason = auto_full_escalation_reason(slim_result)
    if not fallback_reason:
        annotated = annotate_selection(slim_result, selection=selection, selected=selected, attempted=[selected])
        return emit_result(annotated, as_json=as_json)

    full_returncode, full_result, full_stdout, full_stderr = run_helper_json(helper, root, full.path, passthrough)
    if full_returncode != 0 or full_result is None:
        # Keep the successful slim result if full rerun cannot be parsed/run.
        annotated = annotate_selection(
            slim_result,
            selection=selection,
            selected=selected,
            attempted=[selected, full],
            fallback_reason="full rerun failed; retained slim result",
        )
        if full_stderr:
            sys.stderr.write(full_stderr)
        return emit_result(annotated, as_json=as_json)

    annotated = annotate_selection(
        full_result,
        selection=selection,
        selected=full,
        attempted=[selected, full],
        fallback_reason=fallback_reason,
    )
    if stderr:
        sys.stderr.write(stderr)
    if full_stderr:
        sys.stderr.write(full_stderr)
    return emit_result(annotated, as_json=as_json)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Query the eos-config-assistant EOS manual knowledge base without mutating corpus data."
    )
    parser.add_argument("--root", type=Path, help="Override eos-config-assistant repository root")
    parser.add_argument("--db", type=Path, help="Override SQLite DB path")
    parser.add_argument(
        "--variant",
        choices=["auto", "fast", "full", "slim", "lite"],
        default="auto",
        help=(
            "Choose bundled DB variant when --db/EOS_MANUAL_DB is not set. "
            "auto prefers slim, falls back to full for missing source prose, then lite; "
            "fast prefers lite, then slim, then full."
        ),
    )
    args, passthrough = parser.parse_known_args(argv)

    root = args.root.expanduser().resolve() if args.root else find_project_root(Path(__file__))
    helper = root / "tools" / "query_knowledge_base.py"
    if not helper.is_file():
        raise SystemExit(f"missing query helper: {helper}")

    selection = resolve_db_selection(root, db_override=args.db, variant=args.variant)
    candidates = existing_candidates(selection)
    if not candidates:
        raise SystemExit(missing_db_error(selection))

    if selection.used_override or selection.requested_variant in {"full", "slim", "lite"}:
        return run_selected(helper, root, candidates[0], passthrough)
    return run_auto_or_fast(helper, root, selection, candidates, passthrough)


if __name__ == "__main__":
    raise SystemExit(main())
