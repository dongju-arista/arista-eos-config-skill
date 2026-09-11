# EOS Config Assistant workflow

## 1. Inputs to inspect

Prefer user-provided paths. Labs may be outside this repository.

Look for:

- `topology.yml`, `topology.yaml`, ACT YAML, or containerlab-style topology.
- `ref/topology.md`, `scenario.md`, `README.md`, or test-plan markdown.
- Existing `configs/` and `validation/` folders.
- Explicit user messages describing image-derived topology, traffic goals, or platform constraints.

Extract:

- Device names and roles.
- EOS version and platform/model.
- Interface map exactly as the runnable lab uses it.
- BGP ASNs, loopbacks, VTEP loopbacks, VRFs, VLANs, VNIs, route-targets, and service IPs.
- Test endpoints and traffic direction.
- Any mismatch between image/reference ports and current lab YAML ports.

## 2. Retrieval policy

Use precomputed knowledge only:

- SQLite DB variants:
  - `knowledge/eos_manual.sqlite` — canonical full DB; use for precise source prose and old patch-version detail.
  - `knowledge/eos_manual.slim.sqlite` — distribution DB; use as the normal guidance first choice.
  - `knowledge/eos_manual.lite.sqlite` — command/version support metadata only; use for automation pre-checks and CI-style guardrails.
- Query helpers:
  - Manual: `tools/query_knowledge_base.py` or `scripts/query_manual.py`.
- Existing docs under `docs/` for schema, ingestion, and retrieval contracts.

Do not do question-time PDF/HTML ingestion, scraping, re-chunking, or corpus mutation.

DB selection:

1. Use `scripts/query_manual.py --variant auto` by default; it tries slim first, reruns full when slim has no retained body chunks or an empty reduced version-diff result and full exists, then falls back by availability.
2. Use `--variant fast` for fast support checks before generated config is executed or linted; it tries lite, then slim, then full and does not rerun for prose.
3. Use `--variant lite` only when metadata-only command/version support is desired.
4. Use `--variant slim` or `--variant full` for candidate config explanations, feature caveats, examples, and validation guidance.
5. If a lite/slim lookup returns command support without chunks/body, treat it as version-support evidence only and escalate before writing prose-heavy guidance.

Feature-introduction lookup:

1. For "when was this added/introduced?" questions, use manual `--earliest-support`/`--when-added` and scan all F-release manual evidence.
2. Do not constrain that scan to a single requested version unless the user specifically asks for support in that version after the introduction check.
3. If manual evidence is missing, answer `unknown` rather than guessing.

Recommended query pattern:

1. Query broad feature terms first: `evpn vxlan`, `bgp labeled-unicast`, `vpnv4`, `ipv6 link-local bgp`.
2. Query command families second: `router bgp`, `address-family evpn`, `mpls ip`, `neighbor send-label`, `vlan vni`, `vxlan vlan`.
3. Query validation commands last: `show bgp evpn`, `show bgp labeled-unicast`, `show mpls`, `show vxlan`, `show ip route vrf`.
4. Use `--version` when the lab version is known.
5. Use `--compare-version` only when the user asks about version differences.

Version/manual rules:

- The SQLite manual corpus contains EOS User Manuals for F releases only.
- Treat F releases as feature releases and M releases as maintenance releases.
- If a requested M release is not in the DB, resolve it to the latest available same major.minor F manual. Example: `4.34.5M` should use the highest `4.34.xF` manual present in the DB as the primary manual source.
- Do not present same-train F fallback as exact M-release evidence. Say "manual evidence: 4.34.xF proxy for requested 4.34.5M" or equivalent.
- If no user/lab version is supplied, omit `--version`; do not silently use the newest DB version.
- If an exact F patch has no retained prose in `slim`, let `--variant auto` rerun `full` when available before writing prose-heavy guidance.
- For "when was this added/introduced?" questions, use manual `--earliest-support`/`--when-added` and scan all F-release evidence. Do not constrain that scan to a single requested version.
- If command_support or full-text lookup has no match, answer `unknown` rather than `unsupported` unless explicit removed/deprecated/changed evidence exists.

## 3. Synthesis rules

- Tie each proposed config block to the lab device and interface names, not image-only names.
- Preserve original image labels only as comments/reference mapping.
- State source coverage: which features were found in the manual DB, which DB variant was used, and whether the evidence was exact-version, same-train F proxy, unversioned, metadata-only, or unknown.
- Keep candidate configs reversible and local-file oriented unless the user explicitly requests device automation.
- Prefer minimal config that satisfies the stated test objective.

## 4. Automation phases

Phase 1 — guidance only:

- Produce explanation, candidate config, validation commands.
- No device connections.

Phase 2 — local generation:

- Write config files under a lab folder when requested.
- Validate syntax/structure as far as local tooling allows.

Phase 3 — approval-gated execution:

- Connect to devices/controllers only after explicit user authorization.
- Show target inventory, exact command set, rollback plan, and blast radius first.
