# Safety policy

## Default safe actions

Allowed without extra approval:

- Read local lab files.
- Read this repository's manual DB and docs.
- Generate local candidate configs, plans, and validation command lists.
- Run local validators and read-only query tools.

## Explicit approval required

Require explicit user approval before:

- Opening SSH/eAPI/CloudVision sessions to devices or controllers.
- Applying, replacing, or deleting configuration.
- Running commands that change device state.
- Reloading, shutting interfaces, clearing sessions/counters in a disruptive way, or changing production/lab traffic.

## Pre-execution checklist for future automation

Before any approval-gated device action, present:

1. Target devices and management addresses.
2. Exact commands/configs to run.
3. Whether the environment is lab or production.
4. Expected impact and blast radius.
5. Rollback plan.
6. Verification commands and stop condition.

If any item is unknown, do not execute; ask for the missing information or generate local-only artifacts instead.
