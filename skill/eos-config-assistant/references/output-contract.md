# Output contract

Use this structure for EOS config guidance unless the user asks for a different format.

## 1. Scope and assumptions

- State the target lab path or files read.
- State EOS version requested by the user/lab, manual evidence version used for retrieval, DB variant, and evidence scope.
- If no version was supplied, say no version filter was applied; do not name a default/latest EOS version.
- If an M release was requested, state that EOS User Manuals are F-release-only in this KB and that the latest same-train F manual was used as a proxy.
- List assumptions and items needing confirmation in the user's language.

## 2. Topology normalization

- Show device names exactly as the runnable topology uses them.
- Show interface mapping exactly as the runnable topology uses it.
- If image/reference ports differ, keep them in a separate reference column only.

## 3. Manual retrieval evidence

- List the manual queries performed.
- For each query, include version scope from the retrieval result: exact F manual, same-train F proxy, unversioned, metadata-only, or unknown.
- Summarize relevant feature/command evidence.
- Mark unsupported evidence states as `unknown` unless explicit DB evidence says otherwise.
- For "when added/introduced" answers, use manual `earliest_support` evidence and state corpus gaps explicitly.

## 4. Candidate configuration

Group by device:

```text
<device-name>
  purpose: <role>
  config:
    <candidate EOS config>
```

Keep config blocks focused and avoid unrelated defaults.

## 5. Validation plan

Group by validation layer:

- Physical/link state.
- Underlay routing.
- Overlay EVPN/VXLAN.
- MPLS/BGP-LU or VPNv4, if applicable.
- VRF/service routing.
- Endpoint traffic.

For each command, include expected observations rather than exact brittle output unless exact output is known.

## 6. Risks and next steps

- State commands/features with weak manual evidence.
- State device-platform or version risks.
- State safe next local action.
- Do not propose live execution as the default next step.
