#!/usr/bin/env python3
"""Read-only retrieval helper for the precomputed EOS manual knowledge base.

This tool never imports, chunks, summarizes, opens PDFs, or writes corpus data
at query time. It reads the generated SQLite DB for primary retrieval or existing YAML fixture artifacts for regression scenarios.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment guard
    raise SystemExit("PyYAML is required: python3 -m pip install pyyaml") from exc

ROOT = Path(__file__).resolve().parents[1]
BODY_ALLOWED_PERMISSION_STATUSES = {"permitted", "user-provided", "synthetic"}
SQLITE_DEFAULT_LIMIT = 8
F_ONLY_MANUAL_CORPUS_NOTE = (
    "EOS User Manual corpus contains F-release manuals only. "
    "F releases are feature releases; M releases are maintenance releases and do not have separate manuals here."
)
EOS_VERSION_RE = re.compile(r"^\s*(?:EOS[-_ ]*)?(\d+)\.(\d+)(?:\.(\d+))?([A-Za-z]*)\s*$")



def to_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def load_yaml(relative_path: str) -> dict[str, Any]:
    path = ROOT / relative_path
    if not path.is_file():
        raise FileNotFoundError(f"missing file: {relative_path}")
    with path.open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def rel_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def get_in(obj: Any, *keys: str) -> Any:
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def find_index_entry(indexes_by_id: dict[str, dict[str, Any]], index_id: str, key: str) -> dict[str, Any] | None:
    index = indexes_by_id.get(index_id)
    for entry in to_list(index.get("entries") if index else None):
        if isinstance(entry, dict) and entry.get("key") == key:
            return entry
    return None


def chunk_id_from_ref(ref: Any) -> str | None:
    if isinstance(ref, dict):
        return ref.get("chunk_id")
    if isinstance(ref, str):
        return ref
    return None


def collect_index_lookup(result: dict[str, Any], indexes_by_id: dict[str, dict[str, Any]], index_id: str, key: str) -> None:
    entry = find_index_entry(indexes_by_id, index_id, key)
    lookup: dict[str, Any] = {
        "index_id": index_id,
        "key": key,
        "found": entry is not None,
        "chunk_ids": [],
        "metadata_only_sources": [],
    }

    if entry is None:
        result["missing_index_entries"].append({"index_id": index_id, "key": key})
        result["index_lookups"].append(lookup)
        return

    if entry.get("source_id"):
        lookup["source_id"] = entry["source_id"]
        result["selected_source_ids"].append(entry["source_id"])

    for chunk_ref in to_list(entry.get("chunks")):
        chunk_id = chunk_id_from_ref(chunk_ref)
        if chunk_id:
            lookup["chunk_ids"].append(chunk_id)
            result["selected_chunk_ids"].append(chunk_id)
        if isinstance(chunk_ref, dict) and chunk_ref.get("source_id"):
            result["selected_source_ids"].append(chunk_ref["source_id"])

    for source_ref in to_list(entry.get("metadata_only_sources")):
        if not isinstance(source_ref, dict):
            continue
        lookup["metadata_only_sources"].append(source_ref)
        if source_ref.get("source_id"):
            result["selected_source_ids"].append(source_ref["source_id"])

    result["index_lookups"].append(lookup)


def unique_in_place(values: list[Any]) -> None:
    seen: set[str] = set()
    unique: list[Any] = []
    for value in values:
        marker = json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
        if marker in seen:
            continue
        seen.add(marker)
        unique.append(value)
    values[:] = unique


def compact_record(record: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "stable_section_id",
        "diff_status",
        "from_chunk_id",
        "to_chunk_id",
        "chunks",
        "source_id",
        "permission_status",
        "from_permission_status",
        "to_permission_status",
        "reason",
    ]
    return {key: record.get(key) for key in keys if key in record}



def open_sqlite_readonly(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise FileNotFoundError(f"missing SQLite DB: {db_path}")
    uri = f"{db_path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn


def sqlite_rows(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def split_versions(values: list[str]) -> list[str]:
    versions: list[str] = []
    for value in values:
        for item in str(value).split(","):
            item = item.strip()
            if item:
                versions.append(item)
    seen: set[str] = set()
    unique: list[str] = []
    for version in versions:
        if version in seen:
            continue
        seen.add(version)
        unique.append(version)
    return unique


def parse_eos_version(version: str) -> dict[str, Any] | None:
    match = EOS_VERSION_RE.fullmatch(str(version).strip())
    if not match:
        return None
    major, minor, patch, suffix = match.groups()
    return {
        "major": int(major),
        "minor": int(minor),
        "patch": int(patch) if patch is not None else None,
        "suffix": suffix.upper() if suffix else "",
    }


def natural_version_key(version: str) -> tuple[int, int, int, str]:
    parsed = parse_eos_version(version)
    if not parsed:
        return (10**9, 10**9, 10**9, str(version))
    patch = parsed["patch"] if parsed["patch"] is not None else -1
    return (parsed["major"], parsed["minor"], patch, parsed["suffix"])


def release_train(parsed: dict[str, Any]) -> str:
    return f"{parsed['major']}.{parsed['minor']}"


def is_f_release(version: str) -> bool:
    parsed = parse_eos_version(version)
    return bool(parsed and parsed.get("suffix") == "F")


def sqlite_manual_versions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = sqlite_rows(
        conn,
        "SELECT eos_version, document_version, first_source_id FROM manual_versions",
    )
    rows.sort(key=lambda row: natural_version_key(row["eos_version"]))
    return rows


def latest_same_train_f_manual(conn: sqlite3.Connection, parsed: dict[str, Any]) -> dict[str, Any] | None:
    candidates = []
    for row in sqlite_manual_versions(conn):
        row_parsed = parse_eos_version(row["eos_version"])
        if not row_parsed:
            continue
        if row_parsed["major"] != parsed["major"] or row_parsed["minor"] != parsed["minor"]:
            continue
        if row_parsed["suffix"] != "F":
            continue
        candidates.append(row)
    if not candidates:
        return None
    return max(candidates, key=lambda row: natural_version_key(row["eos_version"]))


def version_resolution_base(version_or_alias: str) -> dict[str, Any]:
    return {
        "input": version_or_alias,
        "requested_eos_version": version_or_alias,
        "manual_corpus": F_ONLY_MANUAL_CORPUS_NOTE,
    }


def resolve_sqlite_version(conn: sqlite3.Connection, version_or_alias: str) -> dict[str, Any]:
    alias = conn.execute(
        "SELECT alias, eos_version, source_id, notes FROM version_aliases WHERE alias = ?",
        (version_or_alias,),
    ).fetchone()
    if alias:
        parsed = parse_eos_version(alias["eos_version"])
        scope = "exact F manual" if parsed and parsed.get("suffix") == "F" else "alias-resolved manual version"
        return {
            **version_resolution_base(version_or_alias),
            "resolved_eos_version": alias["eos_version"],
            "manual_evidence_version": alias["eos_version"],
            "alias": alias["alias"],
            "source_id": alias["source_id"],
            "notes": alias["notes"],
            "resolution_status": "alias",
            "resolution_reason": "version alias resolved to a manual version recorded in the SQLite DB",
            "evidence_scope": scope,
            **({"release_train": release_train(parsed)} if parsed else {}),
        }
    version = conn.execute(
        "SELECT eos_version, document_version, first_source_id FROM manual_versions WHERE eos_version = ?",
        (version_or_alias,),
    ).fetchone()
    if version:
        parsed = parse_eos_version(version["eos_version"])
        status = "exact_f_manual" if parsed and parsed.get("suffix") == "F" else "exact_manual"
        scope = "exact F manual" if status == "exact_f_manual" else "exact manual version"
        return {
            **version_resolution_base(version_or_alias),
            "resolved_eos_version": version["eos_version"],
            "manual_evidence_version": version["eos_version"],
            "document_version": version["document_version"],
            "source_id": version["first_source_id"],
            "resolution_status": status,
            "resolution_reason": "exact manual version exists in the SQLite DB",
            "evidence_scope": scope,
            **({"release_train": release_train(parsed), "requested_suffix": parsed.get("suffix")} if parsed else {}),
        }

    parsed = parse_eos_version(version_or_alias)
    if parsed:
        fallback = latest_same_train_f_manual(conn, parsed)
        if fallback:
            suffix = parsed.get("suffix") or ""
            if suffix == "M":
                status = "same_train_latest_f_proxy_for_m_release"
                reason = (
                    "requested version is an M maintenance release; using the latest available F manual "
                    "in the same major.minor train as the primary manual evidence"
                )
                scope = "same-train latest F manual proxy for requested M maintenance release"
            elif suffix == "F":
                status = "same_train_latest_f_fallback"
                reason = (
                    "requested F patch manual is not present; using the latest available F manual "
                    "in the same major.minor train"
                )
                scope = "same-train latest F manual fallback; exact requested F patch manual missing"
            elif parsed.get("patch") is None:
                status = "minor_train_latest_f_fallback"
                reason = "minor-train shorthand was requested; using the latest available F manual in that train"
                scope = "minor-train latest F manual fallback"
            else:
                status = "same_train_latest_f_fallback"
                reason = "requested version is not present; using the latest available F manual in the same major.minor train"
                scope = "same-train latest F manual fallback"
            return {
                **version_resolution_base(version_or_alias),
                "resolved_eos_version": fallback["eos_version"],
                "manual_evidence_version": fallback["eos_version"],
                "document_version": fallback.get("document_version"),
                "source_id": fallback.get("first_source_id"),
                "missing_in_db": True,
                "resolution_status": status,
                "resolution_reason": reason,
                "evidence_scope": scope,
                "release_train": release_train(parsed),
                "requested_suffix": suffix,
            }

    return {
        **version_resolution_base(version_or_alias),
        "resolved_eos_version": None,
        "manual_evidence_version": None,
        "missing_in_db": True,
        "resolution_status": "unresolved",
        "resolution_reason": "no exact manual version, alias, or same-train F manual exists in the SQLite DB",
        "evidence_scope": "unversioned corpus search only; no version-specific support claim",
    }


def fts5_query(value: str) -> str:
    # SQLite FTS5 unicode61 tokenizes punctuation such as hyphen, dot, colon,
    # and slash as separators by default. Match that behavior here instead of
    # sending punctuation-bearing quoted tokens like "send-label", which can
    # miss prose that indexes as "send" + "label". The EOS manuals are English;
    # ignoring non-ASCII natural-language glue lets mixed Korean/English user
    # requests still search on the command/feature terms they contain.
    tokens = re.findall(r"[A-Za-z0-9_]+", value)
    escaped = [token.replace('"', '""') for token in tokens if token.strip()]
    return " AND ".join(f'"{token}"' for token in escaped)


def command_query_tokens(value: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[\w.:-]+", value, flags=re.UNICODE) if token.strip()]


def sqlite_command_ids_for_query(conn: sqlite3.Connection, query: str | None, limit: int) -> list[str]:
    """Return command ids matching a query when no full-text chunks are available.

    This is intentionally a small SQLite-only fallback for slim/lite variants.
    It does not open PDFs or synthesize support; it only searches the already
    extracted command table and lets command_support constrain versions.
    """

    tokens = command_query_tokens(query or "")
    if not tokens:
        return []
    normalized = " ".join(tokens)
    all_token_clause = " AND ".join("normalized_command LIKE ?" for _ in tokens)
    params: list[Any] = [f"%{normalized}%", *(f"%{token}%" for token in tokens), normalized, f"{normalized}%"]
    return [
        row["id"]
        for row in conn.execute(
            f"""
            SELECT id
            FROM commands
            WHERE normalized_command LIKE ? OR ({all_token_clause})
            ORDER BY
              CASE
                WHEN normalized_command = ? THEN 0
                WHEN normalized_command LIKE ? THEN 1
                ELSE 2
              END,
              LENGTH(normalized_command),
              normalized_command
            LIMIT ?
            """,
            (*params, max(limit * 4, limit)),
        )
    ]


def fetch_chunk_tags(conn: sqlite3.Connection, chunk_ids: list[str]) -> dict[str, dict[str, list[str]]]:
    if not chunk_ids:
        return {}
    placeholders = ",".join("?" for _ in chunk_ids)
    tags = {chunk_id: {"commands": [], "features": [], "situations": []} for chunk_id in chunk_ids}
    for row in conn.execute(
        f"""
        SELECT cc.chunk_id, c.command_text
        FROM chunk_commands cc JOIN commands c ON c.id = cc.command_id
        WHERE cc.chunk_id IN ({placeholders})
        ORDER BY c.command_text
        """,
        chunk_ids,
    ):
        tags[row["chunk_id"]]["commands"].append(row["command_text"])
    for row in conn.execute(
        f"""
        SELECT cf.chunk_id, f.feature_name
        FROM chunk_features cf JOIN features f ON f.id = cf.feature_id
        WHERE cf.chunk_id IN ({placeholders})
        ORDER BY f.feature_name
        """,
        chunk_ids,
    ):
        tags[row["chunk_id"]]["features"].append(row["feature_name"])
    for row in conn.execute(
        f"""
        SELECT cs.chunk_id, s.situation_name
        FROM chunk_situations cs JOIN situations s ON s.id = cs.situation_id
        WHERE cs.chunk_id IN ({placeholders})
        ORDER BY s.situation_name
        """,
        chunk_ids,
    ):
        tags[row["chunk_id"]]["situations"].append(row["situation_name"])
    return tags


def sqlite_chunk_query(conn: sqlite3.Connection, args: argparse.Namespace, resolved_versions: list[str]) -> list[dict[str, Any]]:
    filters: list[str] = []
    params: list[Any] = []
    if resolved_versions:
        filters.append(f"c.eos_version IN ({','.join('?' for _ in resolved_versions)})")
        params.extend(resolved_versions)
    if args.source_id:
        filters.append("c.source_id = ?")
        params.append(args.source_id)
    where_suffix = f" AND {' AND '.join(filters)}" if filters else ""
    limit = args.limit or SQLITE_DEFAULT_LIMIT

    if args.query:
        expression = fts5_query(args.query)
        if not expression:
            return []
        sql = f"""
        SELECT c.id AS chunk_id, c.source_id, c.eos_version, c.section_id, c.page_start, c.page_end,
               c.title, c.stable_section_id, c.body, c.char_count, c.token_estimate,
               c.permission_status, c.full_text_allowed, s.document_title, s.document_version,
               c.source_sha256, s.source_path, sec.level AS section_level,
               bm25(chunks_fts) AS rank
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.chunk_id
        JOIN sources s ON s.id = c.source_id
        LEFT JOIN sections sec ON sec.id = c.section_id
        WHERE chunks_fts MATCH ?{where_suffix}
        ORDER BY rank, c.eos_version, c.page_start, c.chunk_index
        LIMIT ?
        """
        rows = sqlite_rows(conn, sql, (expression, *params, limit))
    else:
        sql = f"""
        SELECT c.id AS chunk_id, c.source_id, c.eos_version, c.section_id, c.page_start, c.page_end,
               c.title, c.stable_section_id, c.body, c.char_count, c.token_estimate,
               c.permission_status, c.full_text_allowed, s.document_title, s.document_version,
               c.source_sha256, s.source_path, sec.level AS section_level,
               NULL AS rank
        FROM chunks c
        JOIN sources s ON s.id = c.source_id
        LEFT JOIN sections sec ON sec.id = c.section_id
        WHERE 1=1{where_suffix}
        ORDER BY c.eos_version, c.page_start, c.chunk_index
        LIMIT ?
        """
        rows = sqlite_rows(conn, sql, (*params, limit))

    tags = fetch_chunk_tags(conn, [row["chunk_id"] for row in rows])
    rendered: list[dict[str, Any]] = []
    for row in rows:
        permission_status = row.get("permission_status")
        full_text_allowed = bool(row.get("full_text_allowed"))
        body_allowed = permission_status in BODY_ALLOWED_PERMISSION_STATUSES and full_text_allowed
        item = {
            "chunk_id": row["chunk_id"],
            "source_id": row["source_id"],
            "eos_version": row["eos_version"],
            "document_title": row["document_title"],
            "document_version": row["document_version"],
            "source_sha256": row["source_sha256"],
            "source_path": row["source_path"],
            "section_id": row["section_id"],
            "stable_section_id": row["stable_section_id"],
            "section_level": row["section_level"],
            "title": row["title"],
            "page_start": row["page_start"],
            "page_end": row["page_end"],
            "char_count": row["char_count"],
            "token_estimate": row["token_estimate"],
            "rank": row["rank"],
            "permission_status": permission_status,
            "full_text_allowed": full_text_allowed,
            "body_available": body_allowed and row.get("body") is not None,
            **tags.get(row["chunk_id"], {"commands": [], "features": [], "situations": []}),
        }
        if item["body_available"]:
            item["body"] = row["body"]
        else:
            item["body_omitted_reason"] = "metadata-only or not full_text_allowed"
        rendered.append(item)
    return rendered


def sqlite_compare(
    conn: sqlite3.Connection,
    version_resolution: dict[str, Any] | None,
    compare_version_resolution: dict[str, Any] | None,
    limit: int,
) -> dict[str, Any] | None:
    if not version_resolution or not compare_version_resolution:
        return None
    left = version_resolution.get("resolved_eos_version")
    right = compare_version_resolution.get("resolved_eos_version")
    if not left or not right:
        return {
            "from": left,
            "to": right,
            "from_resolution": version_resolution,
            "to_resolution": compare_version_resolution,
            "records": [],
            "manual_corpus": F_ONLY_MANUAL_CORPUS_NOTE,
            "evidence_scope": "comparison skipped because at least one requested version has no resolvable F manual evidence version",
            "notes": "Absence of comparison records is not support/removal evidence.",
        }
    rows = sqlite_rows(
        conn,
        """
        SELECT vd.id, vd.from_eos_version, vd.to_eos_version, vd.entity_type, vd.entity_id,
               cmd.command_text AS entity_name, vd.diff_status, vd.from_chunk_id, vd.to_chunk_id, vd.notes, vd.generated_at
        FROM version_diffs vd
        LEFT JOIN commands cmd ON cmd.id = vd.entity_id AND vd.entity_type = 'command'
        WHERE (vd.from_eos_version = ? AND vd.to_eos_version = ?)
           OR (vd.from_eos_version = ? AND vd.to_eos_version = ?)
        ORDER BY vd.diff_status, COALESCE(cmd.command_text, vd.entity_id)
        LIMIT ?
        """,
        (left, right, right, left, limit),
    )
    return {
        "from": left,
        "to": right,
        "from_resolution": version_resolution,
        "to_resolution": compare_version_resolution,
        "records": rows,
        "manual_corpus": F_ONLY_MANUAL_CORPUS_NOTE,
        "evidence_scope": "comparison uses resolved F manual evidence versions, including same-train F proxies when requested",
        "notes": "One-sided command-documentation records are diff_status=unknown; absence is not treated as unsupported or removed.",
    }


def sqlite_command_support(
    conn: sqlite3.Connection,
    chunk_ids: list[str],
    versions: list[str],
    limit: int,
    *,
    fallback_command_ids: list[str] | None = None,
    query_supplied: bool = False,
) -> list[dict[str, Any]]:
    command_ids: list[str] = []
    if chunk_ids:
        placeholders = ",".join("?" for _ in chunk_ids)
        command_ids = [
            row["command_id"]
            for row in conn.execute(f"SELECT DISTINCT command_id FROM chunk_commands WHERE chunk_id IN ({placeholders})", chunk_ids)
        ]
    elif fallback_command_ids:
        command_ids = fallback_command_ids
    if query_supplied and not command_ids:
        return []
    filters: list[str] = []
    params: list[Any] = []
    if command_ids:
        filters.append(f"cs.command_id IN ({','.join('?' for _ in command_ids)})")
        params.extend(command_ids)
    if versions:
        filters.append(f"cs.eos_version IN ({','.join('?' for _ in versions)})")
        params.extend(versions)
    where = f"WHERE {' AND '.join(filters)}" if filters else ""
    return sqlite_rows(
        conn,
        f"""
        SELECT cs.command_id, c.command_text, cs.eos_version, cs.source_id, cs.support_status,
               cs.evidence_chunk_id, cs.confidence, cs.notes
        FROM command_support cs JOIN commands c ON c.id = cs.command_id
        {where}
        ORDER BY c.command_text, cs.eos_version, cs.source_id
        LIMIT ?
        """,
        (*params, limit),
    )


def unique_values(values: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        unique.append(value)
    return unique


def sqlite_manual_presence_by_version(conn: sqlite3.Connection, query: str, limit: int) -> list[dict[str, Any]]:
    expression = fts5_query(query)
    if not expression:
        return []
    grouped = sqlite_rows(
        conn,
        """
        SELECT c.eos_version, COUNT(*) AS matching_chunks, MIN(c.page_start) AS first_page
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.chunk_id
        WHERE chunks_fts MATCH ?
        GROUP BY c.eos_version
        """,
        (expression,),
    )
    grouped = [row for row in grouped if is_f_release(row["eos_version"])]
    grouped.sort(key=lambda row: natural_version_key(row["eos_version"]))
    rendered: list[dict[str, Any]] = []
    for row in grouped[: max(limit, SQLITE_DEFAULT_LIMIT)]:
        samples = sqlite_rows(
            conn,
            """
            SELECT c.id AS chunk_id, c.source_id, c.eos_version, c.title, c.page_start, c.page_end,
                   c.stable_section_id, bm25(chunks_fts) AS rank
            FROM chunks_fts
            JOIN chunks c ON c.id = chunks_fts.chunk_id
            WHERE chunks_fts MATCH ? AND c.eos_version = ?
            ORDER BY rank, c.page_start, c.chunk_index
            LIMIT 3
            """,
            (expression, row["eos_version"]),
        )
        rendered.append(
            {
                "eos_version": row["eos_version"],
                "matching_chunks": row["matching_chunks"],
                "first_page": row["first_page"],
                "sample_chunks": samples,
            }
        )
    return rendered


def sqlite_body_coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    manual_versions = [row["eos_version"] for row in sqlite_manual_versions(conn)]
    body_versions = [
        row["eos_version"]
        for row in sqlite_rows(
            conn,
            "SELECT DISTINCT eos_version FROM chunks ORDER BY eos_version",
        )
    ]
    manual_f_versions = [version for version in manual_versions if is_f_release(version)]
    body_f_versions = [version for version in body_versions if is_f_release(version)]
    body_f_versions.sort(key=natural_version_key)
    manual_f_versions.sort(key=natural_version_key)
    return {
        "manual_f_version_count": len(manual_f_versions),
        "body_f_version_count": len(body_f_versions),
        "all_f_versions_have_body": len(body_f_versions) == len(manual_f_versions),
    }


def sqlite_earliest_support(conn: sqlite3.Connection, args: argparse.Namespace) -> dict[str, Any] | None:
    if not getattr(args, "earliest_support", False):
        return None
    limit = args.limit or SQLITE_DEFAULT_LIMIT
    result: dict[str, Any] = {
        "query": args.query,
        "manual_corpus": F_ONLY_MANUAL_CORPUS_NOTE,
        "scope": "scan all available F-release manual evidence in the selected DB; do not apply a single requested-version filter",
        "version_filter_applied": False,
        "body_coverage": sqlite_body_coverage(conn),
        "command_records": [],
        "manual_presence": [],
        "notes": [],
    }
    if not args.query:
        result["notes"].append("--earliest-support requires --query; no scan was performed.")
        return result

    manual_presence = sqlite_manual_presence_by_version(conn, args.query, limit)
    result["manual_presence"] = manual_presence
    if manual_presence:
        result["earliest_manual_presence_version"] = manual_presence[0]["eos_version"]

    command_ids = unique_values(sqlite_command_ids_for_query(conn, args.query, limit))
    if command_ids:
        placeholders = ",".join("?" for _ in command_ids)
        rows = sqlite_rows(
            conn,
            f"""
            SELECT cs.command_id, c.command_text, cs.eos_version, cs.source_id, cs.support_status,
                   cs.evidence_chunk_id, cs.confidence, cs.notes
            FROM command_support cs
            JOIN commands c ON c.id = cs.command_id
            WHERE cs.command_id IN ({placeholders}) AND cs.support_status = 'supported'
            ORDER BY c.command_text, cs.eos_version, cs.source_id
            """,
            tuple(command_ids),
        )
        by_command: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            if not is_f_release(row["eos_version"]):
                continue
            by_command.setdefault(row["command_id"], []).append(row)
        for command_id, command_rows in by_command.items():
            command_rows.sort(key=lambda row: natural_version_key(row["eos_version"]))
            versions = unique_values([row["eos_version"] for row in command_rows])
            versions_sample = versions if len(versions) <= 10 else versions[:5] + ["..."] + versions[-5:]
            earliest = command_rows[0]
            latest = max(command_rows, key=lambda row: natural_version_key(row["eos_version"]))
            result["command_records"].append(
                {
                    "command_id": command_id,
                    "command_text": earliest["command_text"],
                    "earliest_eos_version": earliest["eos_version"],
                    "latest_eos_version": latest["eos_version"],
                    "support_status": earliest["support_status"],
                    "earliest_source_id": earliest["source_id"],
                    "earliest_evidence_chunk_id": earliest["evidence_chunk_id"],
                    "confidence": earliest["confidence"],
                    "supported_f_version_count": len(versions),
                    "supported_f_versions_sample": versions_sample,
                    "evidence_scope": "earliest command_support row across F-release manuals in this DB",
                }
            )
        result["command_records"].sort(key=lambda row: (natural_version_key(row["earliest_eos_version"]), row["command_text"]))
    else:
        result["notes"].append(
            "No direct precomputed command table match for the query; use manual_presence as weaker text-occurrence evidence only."
        )

    if result["body_coverage"]["manual_f_version_count"] and not result["body_coverage"]["all_f_versions_have_body"]:
        result["notes"].append(
            "Selected DB does not retain prose chunks for every F manual version; use the full DB for prose-level earliest-introduction analysis."
        )
    if not result["command_records"] and not result["manual_presence"]:
        result["notes"].append("No F-manual evidence matched. Do not infer that the feature is unsupported.")
    return result


def sqlite_sources_for_context(
    conn: sqlite3.Connection,
    *,
    explicit_source_id: str | None,
    chunk_source_ids: list[str],
    version_context: list[str],
) -> list[dict[str, Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if explicit_source_id:
        clauses.append("id = ?")
        params.append(explicit_source_id)
    else:
        unique_chunk_source_ids = sorted(set(chunk_source_ids))
        unique_versions = sorted(set(version_context))
        if unique_chunk_source_ids:
            clauses.append(f"id IN ({','.join('?' for _ in unique_chunk_source_ids)})")
            params.extend(unique_chunk_source_ids)
        if unique_versions:
            clauses.append(f"eos_version IN ({','.join('?' for _ in unique_versions)})")
            params.extend(unique_versions)
    where = f"WHERE {' OR '.join(f'({clause})' for clause in clauses)}" if clauses else ""
    return sqlite_rows(
        conn,
        f"""
        SELECT id, vendor, document_title, eos_version, document_version, source_path,
               source_sha256, source_size_bytes, imported_at, permission_status,
               permission_basis, full_text_allowed
        FROM sources {where}
        ORDER BY eos_version, id
        """,
        tuple(params),
    )


def sqlite_evidence_scope(requested_versions: list[str], resolved: list[dict[str, Any]]) -> dict[str, Any]:
    manual_evidence_versions = [
        item["resolved_eos_version"]
        for item in resolved
        if item.get("resolved_eos_version")
    ]
    scope: dict[str, Any] = {
        "manual_corpus": F_ONLY_MANUAL_CORPUS_NOTE,
        "requested_versions": requested_versions,
        "manual_evidence_versions": manual_evidence_versions,
        "default_version_assumed": False,
    }
    if not requested_versions:
        scope.update(
            {
                "version_filter": None,
                "version_filter_reason": "no EOS version supplied; retrieval intentionally does not force a default/latest version",
            }
        )
        return scope
    if manual_evidence_versions:
        scope.update(
            {
                "version_filter": manual_evidence_versions,
                "version_filter_reason": "using resolved manual evidence versions from inputs.resolved_versions",
                "resolution_statuses": [item.get("resolution_status") for item in resolved],
            }
        )
        return scope
    scope.update(
        {
            "version_filter": None,
            "version_filter_reason": "requested version(s) could not be resolved to exact or same-train F manual evidence; retrieval is unversioned and cannot prove version support",
            "resolution_statuses": [item.get("resolution_status") for item in resolved],
        }
    )
    return scope


def build_sqlite_result(args: argparse.Namespace) -> dict[str, Any]:
    db_path = (ROOT / args.db if not args.db.is_absolute() else args.db).resolve()
    conn = open_sqlite_readonly(db_path)
    try:
        requested_versions = split_versions(args.versions + args.versions_csv)
        resolved = [resolve_sqlite_version(conn, version) for version in requested_versions]
        resolved_versions = [item["resolved_eos_version"] for item in resolved if item.get("resolved_eos_version")]
        chunks = sqlite_chunk_query(conn, args, resolved_versions)
        fallback_command_ids = sqlite_command_ids_for_query(conn, args.query, args.limit or SQLITE_DEFAULT_LIMIT) if args.query and not chunks else []
        compare_version_resolution = resolve_sqlite_version(conn, args.compare_version) if args.compare_version else None
        compare = sqlite_compare(
            conn,
            resolved[0] if resolved else None,
            compare_version_resolution,
            args.limit or SQLITE_DEFAULT_LIMIT,
        )
        command_support = sqlite_command_support(
            conn,
            [chunk["chunk_id"] for chunk in chunks],
            resolved_versions,
            args.limit or SQLITE_DEFAULT_LIMIT,
            fallback_command_ids=fallback_command_ids,
            query_supplied=bool(args.query),
        )
        version_context = list(resolved_versions)
        if compare:
            version_context.extend(version for version in [compare.get("from"), compare.get("to")] if version)
        earliest_support = sqlite_earliest_support(conn, args)
        if earliest_support:
            for record in earliest_support.get("command_records") or []:
                if record.get("earliest_eos_version"):
                    version_context.append(record["earliest_eos_version"])
            if earliest_support.get("earliest_manual_presence_version"):
                version_context.append(earliest_support["earliest_manual_presence_version"])
        sources = sqlite_sources_for_context(
            conn,
            explicit_source_id=args.source_id,
            chunk_source_ids=[chunk["source_id"] for chunk in chunks],
            version_context=version_context,
        )
        result: dict[str, Any] = {
            "retrieval_contract": {
                "corpus_mode": "precomputed-sqlite-fts5",
                "query_time_behavior": "read-only-sqlite-lookup",
                "query_time_chunking": "forbidden",
                "opens_pdf": False,
                "writes_files": False,
                "device_actions": False,
            },
            "inputs": {
                "db": str(db_path),
                "query": args.query,
                "versions": requested_versions,
                "resolved_versions": resolved,
                "resolved_compare_version": compare_version_resolution,
                "source_id": args.source_id,
                "compare_version": args.compare_version,
                "limit": args.limit or SQLITE_DEFAULT_LIMIT,
            },
            "evidence_scope": sqlite_evidence_scope(requested_versions, resolved),
            "sources": sources,
            "chunks": chunks,
            "command_support": command_support,
            "version_comparison": compare,
            "earliest_support": earliest_support,
            "answering_notes": [],
        }
        if not requested_versions:
            result["answering_notes"].append(
                "No EOS version was provided; retrieval ran without a version filter. Do not invent or force a default EOS version."
            )
        for item in resolved:
            if item.get("resolution_status") in {
                "same_train_latest_f_proxy_for_m_release",
                "same_train_latest_f_fallback",
                "minor_train_latest_f_fallback",
                "unresolved",
            }:
                result["answering_notes"].append(
                    f"Version input {item['input']!r}: {item.get('resolution_reason')} "
                    f"(evidence_scope={item.get('evidence_scope')})."
                )
        if compare_version_resolution and compare_version_resolution.get("resolution_status") != "exact_f_manual":
            result["answering_notes"].append(
                f"Compare-version input {compare_version_resolution['input']!r}: "
                f"{compare_version_resolution.get('resolution_reason')} "
                f"(evidence_scope={compare_version_resolution.get('evidence_scope')})."
            )
        if earliest_support:
            result["answering_notes"].append(
                "Earliest-support scan ignores single-version filtering and scans F-release manual evidence in the selected DB."
            )
        if any(not chunk.get("body_available") for chunk in chunks):
            result["answering_notes"].append("Some chunks are metadata-only or body-gated; do not infer missing prose.")
        if fallback_command_ids and command_support:
            result["answering_notes"].append(
                "No full-text chunk matched; returned command_support records from the precomputed command table. "
                "Use the canonical full DB for source prose if detailed manual context is required."
            )
        if not chunks and not compare and not command_support:
            result["answering_notes"].append("No precomputed SQLite chunks matched. Do not open or chunk the PDF at query time; run offline ingest first.")
        return result
    finally:
        conn.close()


def build_result(args: argparse.Namespace) -> dict[str, Any]:
    sources_doc = load_yaml("manuals/sources.yml")
    sources_by_id = {source.get("id"): source for source in to_list(sources_doc.get("sources")) if isinstance(source, dict) and source.get("id")}

    indexes_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "knowledge/indexes").glob("*.yml")):
        index = load_yaml(rel_path(path))
        if index.get("index_id"):
            index["__path"] = rel_path(path)
            indexes_by_id[index["index_id"]] = index

    chunks_by_id: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "knowledge/chunks").glob("**/*.yml")):
        chunk = load_yaml(rel_path(path))
        if chunk.get("id"):
            chunk["__path"] = rel_path(path)
            chunks_by_id[chunk["id"]] = chunk

    result: dict[str, Any] = {
        "retrieval_contract": {
            "corpus_mode": "precomputed-offline-chunks-and-indexes",
            "query_time_behavior": "read-only-lookup",
            "query_time_chunking": "forbidden",
            "opens_pdf": False,
            "writes_files": False,
            "device_actions": False,
        },
        "inputs": {
            "features": args.features,
            "commands": args.commands,
            "situations": args.situations,
            "versions": args.versions,
            "diffs": args.diffs,
        },
        "index_lookups": [],
        "missing_index_entries": [],
        "selected_chunk_ids": [],
        "selected_source_ids": [],
        "chunks": [],
        "metadata_only_sources": [],
        "related_diffs": [],
        "answering_notes": [],
    }

    for key in args.features:
        collect_index_lookup(result, indexes_by_id, "by-feature", key)
    for key in args.commands:
        collect_index_lookup(result, indexes_by_id, "by-command", key)
    for key in args.situations:
        collect_index_lookup(result, indexes_by_id, "by-situation", key)
    for key in args.versions:
        collect_index_lookup(result, indexes_by_id, "by-version", key)

    unique_in_place(result["selected_chunk_ids"])
    unique_in_place(result["selected_source_ids"])

    for chunk_id in result["selected_chunk_ids"]:
        chunk = chunks_by_id.get(chunk_id)
        if not chunk:
            result["answering_notes"].append(f"Missing chunk referenced by index: {chunk_id}")
            continue

        source = chunk.get("source") or {}
        permission_status = source.get("permission_status")
        full_text_allowed = source.get("full_text_allowed") is True
        body_allowed = permission_status in BODY_ALLOWED_PERMISSION_STATUSES and full_text_allowed
        source_id = source.get("source_id")
        if source_id:
            result["selected_source_ids"].append(source_id)

        rendered = {
            "chunk_id": chunk_id,
            "path": chunk.get("__path"),
            "stable_section_id": chunk.get("stable_section_id"),
            "content_type": chunk.get("content_type"),
            "source_id": source_id,
            "permission_status": permission_status,
            "full_text_allowed": full_text_allowed,
            "actual_eos_version": get_in(chunk, "version_scope", "actual_eos_version"),
            "applies_to": get_in(chunk, "version_scope", "applies_to"),
            "diff_status": chunk.get("diff_status"),
            "feature": chunk.get("feature"),
            "commands": chunk.get("commands"),
            "platforms": chunk.get("platforms"),
            "body_available": body_allowed and "body" in chunk,
        }

        if rendered["body_available"]:
            rendered["body"] = chunk.get("body")
        else:
            rendered["body_omitted_reason"] = "metadata-only or not full_text_allowed"

        result["chunks"].append(rendered)

    unique_in_place(result["selected_source_ids"])

    for source_id in result["selected_source_ids"]:
        source = sources_by_id.get(source_id)
        if not source or source.get("permission_status") not in {"unknown", "metadata-only"}:
            continue
        result["metadata_only_sources"].append(
            {
                "source_id": source_id,
                "vendor": source.get("vendor"),
                "document_title": source.get("document_title"),
                "actual_eos_version": source.get("actual_eos_version"),
                "actual_document_version": source.get("actual_document_version"),
                "source_url": source.get("source_url"),
                "source_pdf_url": source.get("source_pdf_url"),
                "retrieved_at": source.get("retrieved_at"),
                "permission_status": source.get("permission_status"),
                "full_text_allowed": source.get("full_text_allowed"),
            }
        )

    requested_diff_paths_or_ids = set(args.diffs)
    chunk_id_set = set(result["selected_chunk_ids"])
    source_id_set = set(result["selected_source_ids"])
    for path in sorted((ROOT / "knowledge/diffs").glob("*.yml")):
        rel = rel_path(path)
        diff = load_yaml(rel)
        records = to_list(diff.get("records")) + to_list(diff.get("common_records")) + to_list(diff.get("unknown_records"))
        explicit = rel in requested_diff_paths_or_ids or diff.get("diff_id") in requested_diff_paths_or_ids
        related = False
        for record in records:
            if not isinstance(record, dict):
                continue
            record_chunk_ids = [record.get("from_chunk_id"), record.get("to_chunk_id"), *to_list(record.get("chunks"))]
            if any(chunk_id in chunk_id_set for chunk_id in record_chunk_ids if chunk_id):
                related = True
            if record.get("source_id") in source_id_set:
                related = True
        if not (explicit or related):
            continue

        result["related_diffs"].append(
            {
                "diff_id": diff.get("diff_id"),
                "path": rel,
                "comparison_scope": diff.get("comparison_scope"),
                "permission_status": diff.get("permission_status"),
                "records": [compact_record(record) for record in records if isinstance(record, dict)],
            }
        )

    if result["metadata_only_sources"]:
        result["answering_notes"].append(
            "Some selected official sources are metadata-only; answer only with source/version metadata and explicit unknowns, not manual prose."
        )
    if not result["chunks"] and not result["metadata_only_sources"]:
        result["answering_notes"].append(
            "No precomputed chunks or metadata-only sources matched. Do not chunk at query time; add offline corpus/index artifacts first."
        )

    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only retrieval helper for the precomputed EOS manual knowledge base.",
        epilog=(
            "Examples:\n"
            "  python3 tools/query_knowledge_base.py --db knowledge/eos_manual.sqlite --query 'show mlag' --version 4.36.0F --json\n"
            "  python3 tools/query_knowledge_base.py --db knowledge/eos_manual.sqlite --query 'bgp labeled-unicast' --limit 5\n"
            "  python3 tools/query_knowledge_base.py --situation validate-mlag-4.34-synthetic  # YAML fixture mode"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--feature", dest="features", action="append", default=[], help="Lookup YAML fixture knowledge/indexes/by-feature.yml key")
    parser.add_argument("--command", dest="commands", action="append", default=[], help="Lookup YAML fixture knowledge/indexes/by-command.yml key")
    parser.add_argument("--situation", dest="situations", action="append", default=[], help="Lookup YAML fixture knowledge/indexes/by-situation.yml key")
    parser.add_argument("--version", dest="versions", action="append", default=[], help="Filter SQLite by EOS version/alias, or lookup a YAML fixture by-version key without --db")
    parser.add_argument("--versions", dest="versions_csv", action="append", default=[], help="Comma-separated SQLite EOS versions/aliases")
    parser.add_argument("--diff", dest="diffs", action="append", default=[], help="Include a precomputed YAML fixture diff by path or diff_id")
    parser.add_argument("--db", type=Path, help="Read the primary precomputed SQLite knowledge base instead of YAML fixture artifacts")
    parser.add_argument("--query", help="SQLite FTS query string; never opens the PDF")
    parser.add_argument("--source-id", help="SQLite source_id filter")
    parser.add_argument("--compare-version", help="SQLite version/alias to compare with the first --version/--versions value")
    parser.add_argument(
        "--earliest-support",
        "--when-added",
        dest="earliest_support",
        action="store_true",
        help="Scan all F-release manual evidence for earliest matching command/text evidence; do not apply a single version filter to this scan.",
    )
    parser.add_argument("--limit", type=int, default=SQLITE_DEFAULT_LIMIT, help="SQLite result limit")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of YAML")
    args = parser.parse_args(argv)
    if args.db:
        if args.limit <= 0:
            parser.error("--limit must be positive")
        if args.earliest_support and not args.query:
            parser.error("--earliest-support/--when-added require --query")
        if not any([args.query, args.versions, args.versions_csv, args.source_id, args.compare_version]):
            parser.print_help(sys.stderr)
            raise SystemExit(2)
        return args
    if any([args.query, args.source_id, args.compare_version, args.versions_csv, args.earliest_support]):
        parser.error("--query/--source-id/--compare-version/--versions/--earliest-support require --db")
    if not any([args.features, args.commands, args.situations, args.versions, args.diffs]):
        parser.print_help(sys.stderr)
        raise SystemExit(2)
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    result = build_sqlite_result(args) if args.db else build_result(args)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(yaml.safe_dump(result, allow_unicode=True, sort_keys=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
