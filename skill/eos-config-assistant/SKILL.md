---
name: eos-config-assistant
description: Manual-grounded Arista EOS configuration assistance. Use when Codex needs to turn an EOS lab topology, test scenario, feature request, or external lab folder into source-backed EOS configuration guidance, candidate configs, validation commands, version-support checks, or later approval-gated automation plans using this repository's EOS manual SQLite knowledge base variants and tools.
---

# EOS Config Assistant

## Purpose

Use this skill to produce EOS configuration guidance from lab artifacts and the local EOS manual knowledge base. Keep the skill thin: read lab-specific files from the user's current workspace or provided paths, and read reusable EOS manual knowledge from this project.

## Root and lab discovery

1. Locate the assistant project root in this order:
   - `EOS_CONFIG_ASSISTANT_HOME` environment variable.
   - The parent runtime repo that contains `knowledge/` and `tools/query_knowledge_base.py`.
   - The resolved path of this skill when installed as a symlink.
2. Locate the lab root from the user's path, current working directory, or explicit files such as `topology.yml`, `*.yml`, `ref/topology.md`, `configs/`, or `validation/`.
3. Do not assume lab files live in this repository. Labs may be in any external repo/folder.

## Standard workflow

1. Read the lab topology and scenario files first.
2. Extract device names, platforms, EOS version if present, ASNs, loopbacks, interface mapping, service VLANs/VRFs/VNIs, and required test traffic.
3. Query the manual DB for every nontrivial feature or command family before proposing config. Prefer full/slim for explanatory guidance; use lite only for fast command/version support checks.
4. Separate facts from assumptions. If a topology field is missing, make a minimal explicit assumption or mark it as `Needs confirmation`, or use an equivalent label in the user's language.
5. Produce output using `references/output-contract.md`.
6. Include verification commands and expected observations before any automation plan.
7. For any action that would touch live devices, apply `references/safety-policy.md`.

Read `references/workflow.md` for the detailed workflow and retrieval policy. Read `references/output-contract.md` before producing config guidance or candidate configs. Read `references/safety-policy.md` before proposing or executing automation beyond local file generation.

## Manual retrieval helper

Prefer the bundled wrapper when searching the EOS manual DB:

```bash
python3 <skill>/scripts/query_manual.py --variant auto --query "bgp labeled-unicast" --version 4.36.0F --limit 5 --json
```

The wrapper delegates to this repository's read-only `tools/query_knowledge_base.py`. It must not ingest PDFs, chunk text, or mutate corpus records during a user question.

DB variant policy:

- `full`: `knowledge/eos_manual.sqlite`; canonical full DB with all retained manual text.
- `slim`: `knowledge/eos_manual.slim.sqlite`; latest patch per minor train has full text, all versions keep command support metadata.
- `lite`: `knowledge/eos_manual.lite.sqlite`; all-version command support metadata only, no manual prose.
- `auto`: prefer slim for normal guidance; if slim returns no retained body chunks or an empty reduced version-diff result and full exists, rerun full; otherwise fall back by availability. Override with `--variant`, `--db`, `EOS_MANUAL_DB`, or `EOS_MANUAL_DB_VARIANT`.

Use `--variant fast` for automation guardrail/pre-checks; it prefers lite, then slim, then full. Use `lite` as the explicit metadata-only mode ("is this command documented for this EOS version?"). Escalate to `slim` or `full` when the answer needs setup procedure, caveats, examples, or source prose.

If direct repo access is easier, use:

```bash
python3 tools/query_knowledge_base.py --db knowledge/eos_manual.sqlite --query "show bgp evpn" --version 4.36.0F --limit 5 --json
```

## Output boundaries

Default allowed outputs:

- Manual-grounded design notes.
- Device-by-device candidate configuration snippets.
- Lab-local config files, if requested.
- Validation command lists and expected results.
- Automation implementation plans that are not executed against devices.

Require explicit user authorization before:

- Connecting to devices or controllers.
- Pushing config.
- Running disruptive operational commands.
- Reloading, disabling interfaces, changing production state, or deleting data.

## Version handling

Use the lab's EOS version when provided. If not provided, use the user's stated version. If neither exists, query without a version filter and report the version gap. Never treat missing command evidence as unsupported; report it as `unknown` unless the knowledge base has explicit removed/deprecated/changed evidence.
