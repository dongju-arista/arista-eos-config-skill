# EOS Config Assistant Skill Runtime

Runtime distribution for the `eos-config-assistant` skill, usable from Codex and Claude Code.

This repository is intended to be cloned or installed as a clean skill runtime. `knowledge/*.sqlite` files and DB symlinks are local artifacts and must stay untracked.

## Contents

```text
skill/eos-config-assistant/   # skill directory to install/symlink into Codex or Claude Code
tools/query_knowledge_base.py # read-only SQLite/FTS retrieval engine
tools/fetch_kb.py             # downloads packaged slim/lite KB archives
knowledge/                    # local KB artifact directory; DB files are gitignored
requirements.txt              # runtime Python dependencies
```

`skill/eos-config-assistant/SKILL.md` is the shared skill entrypoint for both Codex and Claude Code. `skill/eos-config-assistant/agents/openai.yaml` is Codex-specific UI metadata; Claude Code can ignore it.

## Prerequisites

- Python 3.10+.
- `zstd` CLI for decompressing packaged KB archives.
- Access to clone this repository.

On macOS with Homebrew:

```bash
brew install zstd
```

If you do not use Homebrew, install `zstd` from your OS package manager.

## Shared setup

```bash
git clone https://github.com/dongju-arista/arista-eos-config-skill.git
cd arista-eos-config-skill
python3 -m pip install -r requirements.txt
```

## Install for Codex

Install the skill as a personal Codex skill:

```bash
mkdir -p ~/.codex/skills
ln -s "$PWD/skill/eos-config-assistant" ~/.codex/skills/eos-config-assistant
```

## Install for Claude Code

Install the skill as a personal Claude Code skill:

```bash
mkdir -p ~/.claude/skills
ln -s "$PWD/skill/eos-config-assistant" ~/.claude/skills/eos-config-assistant
```

Or install it for only one project:

```bash
mkdir -p /path/to/project/.claude/skills
ln -s "$PWD/skill/eos-config-assistant" /path/to/project/.claude/skills/eos-config-assistant
```

If the skill is copied instead of symlinked in either client, set the runtime root explicitly so the skill can find `knowledge/` and `tools/`:

```bash
export EOS_CONFIG_ASSISTANT_HOME="$PWD"
```

## EOS manual DB setup

The EOS manuals are preprocessed into SQLite databases. Uncompressed DB files are not committed. Put one or more of these files under `knowledge/`:

```text
knowledge/eos_manual.sqlite       # optional full DB
knowledge/eos_manual.slim.sqlite  # recommended default guidance DB
knowledge/eos_manual.lite.sqlite  # fast command/version support DB
```

Download the packaged `slim` and `lite` manual DB archives from GitHub Releases and decompress them into `knowledge/`:

```bash
python3 tools/fetch_kb.py
```

The helper downloads from the GitHub release `v0.2.0` by default. No authentication is required for public repositories. Run `python3 tools/fetch_kb.py --help` for override options.

The helper requires the `zstd` CLI. On macOS Python installs with an incomplete CA store, it automatically falls back to `curl` for the download.

### Manual DB archive download fallback

If `tools/fetch_kb.py` still cannot download the package, download the DB archives from the GitHub release page instead:

1. Open the release page: `https://github.com/dongju-arista/arista-eos-config-skill/releases/tag/v0.2.0`.
2. Download one or both archive files:
   - `eos_manual.slim.sqlite.zst` — recommended default guidance DB
   - `eos_manual.lite.sqlite.zst` — fast command/version support DB

Keep the downloaded filenames unchanged, move them under `knowledge/`, then decompress them:

```bash
mkdir -p knowledge
mv ~/Downloads/eos_manual.*.sqlite.zst knowledge/

for archive in knowledge/eos_manual.*.sqlite.zst; do
  [ -e "$archive" ] || continue
  zstd -d -k -f "$archive"
done
```

Confirm that at least one SQLite DB now exists:

```bash
ls -lh knowledge/eos_manual.*.sqlite
python3 skill/eos-config-assistant/scripts/query_manual.py --variant lite --query "egress permit acl logging" --limit 1 --json
```

You may also symlink existing DB files into `knowledge/`, but do not commit those symlinks or DB files.

Example:

```bash
ln -s /path/to/eos_manual.slim.sqlite knowledge/eos_manual.slim.sqlite
ln -s /path/to/eos_manual.lite.sqlite knowledge/eos_manual.lite.sqlite
```

## EOS manual version behavior

- This KB contains EOS User Manuals for F releases only.
- F releases are treated as feature releases; M releases are treated as maintenance releases.
- For an M-release question such as `4.34.5M`, the skill uses the latest available same-train F manual (`4.34.xF`) as the primary manual evidence and labels it as a proxy, not exact M-manual evidence.
- If no EOS version is provided, the skill queries without a version filter instead of forcing a default/latest version.
- For "when was this added?" questions, the skill scans all F-manual evidence with `--earliest-support`/`--when-added` instead of filtering to one version.

## Use from Codex

After installing the skill and KB, start a new Codex session or reload skills if your client supports it. Reference the skill by name when asking for EOS guidance:

```text
Use $eos-config-assistant with /path/to/lab/topology.yml.
Build manual-grounded EVPN/VXLAN candidate configs for EOS 4.36.0F and include validation commands.
```

Other useful prompts:

```text
$eos-config-assistant Check whether neighbor send-label is documented for EOS 4.36.0F and show the validation commands.

$eos-config-assistant Read /path/to/lab and produce device-by-device candidate config plus risks and assumptions.
```

The skill expects either lab files (`topology.yml`, scenario docs, configs, validation files) or a clear text requirement. It produces manual-grounded guidance, candidate configs, version-support notes, and validation plans. It does not connect to devices or push config unless you explicitly authorize that later.

## Use from Claude Code

After installing the skill and KB, start or restart Claude Code if this is your first `~/.claude/skills` directory. Invoke the skill by slash command:

```text
/eos-config-assistant Read /path/to/lab/topology.yml.
Build manual-grounded EVPN/VXLAN candidate configs for EOS 4.36.0F and include validation commands.
```

Other useful prompts:

```text
/eos-config-assistant Check whether neighbor send-label is documented for EOS 4.36.0F and show the validation commands.

/eos-config-assistant Read /path/to/lab and produce device-by-device candidate config plus risks and assumptions.
```

## Smoke test

```bash
python3 skill/eos-config-assistant/scripts/query_manual.py --variant slim --query "evpn vxlan" --version 4.36.0F --limit 1 --json
```

Variant policy is implemented by `scripts/query_manual.py`:

```text
auto = slim -> full -> lite, with full retry when slim lacks source prose
fast = lite -> slim -> full
```

Use `tools/fetch_kb.py --force` to refresh the local `slim` and `lite` manual DB files from GitHub release `v0.2.0`.
