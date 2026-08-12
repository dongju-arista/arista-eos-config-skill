#!/usr/bin/env python3
"""Fetch packaged EOS manual KB archives into the skill runtime.

The eos-config-assistant skill reads uncompressed SQLite databases from the
runtime repository's ``knowledge/`` directory.  This helper downloads the
compressed ``.zst`` bootstrap archives from GitLab Generic Package Registry,
verifies them, decompresses them, and places the resulting ``.sqlite`` files
where ``skill/eos-config-assistant/scripts/query_manual.py`` can find them.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

DEFAULT_GITLAB_HOST = "https://gitlab.aristanetworks.com"
DEFAULT_PROJECT = "dongju/eos-config-assistant-skill"
DEFAULT_PACKAGE = "eos-manual-kb"
DEFAULT_PACKAGE_VERSION = "2026.8.12"

DEFAULT_GITHUB_REPO = "dongju-arista/arista-eos-config-skill"
DEFAULT_GITHUB_TAG = "v0.2.0"
SQLITE_HEADER = b"SQLite format 3\x00"
PROGRESS_STEP_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class ArchiveSpec:
    variant: str
    archive_name: str
    sqlite_name: str
    sha256: str
    size_bytes: int


ARCHIVES: dict[str, ArchiveSpec] = {
    "lite": ArchiveSpec(
        variant="lite",
        archive_name="eos_manual.lite.sqlite.zst",
        sqlite_name="eos_manual.lite.sqlite",
        sha256="b4bd331d7f478cb090829140d0e23d55e971e0b63b55d7bfea5be4f68ff6fd9e",
        size_bytes=10_709_871,
    ),
    "slim": ArchiveSpec(
        variant="slim",
        archive_name="eos_manual.slim.sqlite.zst",
        sqlite_name="eos_manual.slim.sqlite",
        sha256="d6de93b0b2dffb4414a48e46ee151bac03eb2efcf3919fc56b16c25a8c000e68",
        size_bytes=231_520_663,
    ),
}
ALL_VARIANTS = ["lite", "slim"]


@dataclass(frozen=True)
class AuthCandidate:
    label: str
    header_name: str | None
    header_value: str | None

    def headers(self) -> dict[str, str]:
        headers = {"User-Agent": "eos-config-assistant-fetch-kb/1.0"}
        if self.header_name and self.header_value:
            headers[self.header_name] = self.header_value
        return headers


class DownloadFailure(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class TokenSafeRedirectHandler(HTTPRedirectHandler):
    """Follow GitLab package redirects without forwarding tokens cross-host."""

    SENSITIVE_HEADERS = {"authorization", "deploy-token", "job-token", "private-token"}

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = urlparse(req.full_url).netloc
        new_host = urlparse(newurl).netloc
        if old_host != new_host:
            for header_map in (redirected.headers, redirected.unredirected_hdrs):
                for key in list(header_map):
                    if key.lower() in self.SENSITIVE_HEADERS:
                        del header_map[key]
        return redirected


def format_bytes(value: int | None) -> str:
    if value is None:
        return "unknown size"
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}" if unit != "B" else f"{int(amount)} B"
        amount /= 1024
    return f"{value} B"


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


def find_runtime_root(script_path: Path) -> Path:
    for root in candidate_roots(script_path):
        if (root / "knowledge").is_dir() and (root / "tools" / "query_knowledge_base.py").is_file():
            return root
    raise SystemExit(
        "Could not locate eos-config-assistant runtime root. "
        "Run this from the package repository or pass --root /path/to/eos-config-assistant-skill."
    )


def encode_project(value: str) -> str:
    stripped = value.strip()
    if stripped.isdecimal() or "%2f" in stripped.lower():
        return stripped
    return quote(stripped, safe="")


def build_package_url(
    *,
    host: str,
    project: str,
    package: str,
    package_version: str,
    archive_name: str,
) -> str:
    host = host.rstrip("/")
    return (
        f"{host}/api/v4/projects/{encode_project(project)}"
        f"/packages/generic/{quote(package, safe='')}"
        f"/{quote(package_version, safe='')}"
        f"/{quote(archive_name, safe='')}"
    )


def build_github_release_url(
    *,
    repo: str,
    tag: str,
    archive_name: str,
) -> str:
    return f"https://github.com/{repo}/releases/download/{quote(tag, safe='')}/{quote(archive_name, safe='')}"


def gitlab_hostname(host: str) -> str:
    parsed = urlparse(host if "://" in host else f"https://{host}")
    return parsed.netloc or parsed.path.split("/", 1)[0]


def auth_candidate_for(token_type: str, token: str, *, label_prefix: str | None = None) -> AuthCandidate:
    def label(value: str) -> str:
        return f"{label_prefix} as {value}" if label_prefix else value

    if token_type == "private":
        return AuthCandidate(label("PRIVATE-TOKEN"), "PRIVATE-TOKEN", token)
    if token_type == "deploy":
        return AuthCandidate(label("DEPLOY-TOKEN"), "DEPLOY-TOKEN", token)
    if token_type == "job":
        return AuthCandidate(label("JOB-TOKEN"), "JOB-TOKEN", token)
    if token_type == "bearer":
        return AuthCandidate(label("Authorization: Bearer"), "Authorization", f"Bearer {token}")
    raise ValueError(f"unsupported token type: {token_type}")


def token_from_glab(host: str) -> str | None:
    """Read a token from an existing `glab auth login` session, if available."""

    glab = shutil.which("glab")
    if not glab:
        return None

    hostname = gitlab_hostname(host)
    commands = [
        [glab, "auth", "token", "--hostname", hostname],
        [glab, "auth", "token", "-h", hostname],
        [glab, "auth", "token"],
    ]
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if completed.returncode != 0:
            continue
        lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
        if lines:
            return lines[-1]
    return None


def discover_token(cli_token: str | None, *, host: str) -> tuple[str | None, str | None]:
    if cli_token:
        return cli_token, "--token"
    for env_name in (
        "GITLAB_DEPLOY_TOKEN",
        "GITLAB_PRIVATE_TOKEN",
        "GITLAB_TOKEN",
        "GLAB_TOKEN",
        "CI_JOB_TOKEN",
    ):
        value = os.environ.get(env_name)
        if value:
            return value, env_name
    glab_token = token_from_glab(host)
    if glab_token:
        return glab_token, "glab auth token"
    return None, None


def auth_candidates(*, cli_token: str | None, token_type: str, host: str) -> list[AuthCandidate]:
    token, source = discover_token(cli_token, host=host)
    if not token:
        return [AuthCandidate("unauthenticated", None, None)]

    if token_type != "auto":
        return [auth_candidate_for(token_type, token, label_prefix=source)]

    if source == "GITLAB_DEPLOY_TOKEN":
        order = ["deploy", "private", "bearer"]
    elif source == "CI_JOB_TOKEN":
        order = ["job"]
    elif source == "glab auth token":
        # `glab auth login` may store either a PAT-like token or an OAuth
        # token. Try GitLab's private-token header first, then OAuth bearer.
        order = ["private", "bearer"]
    else:
        order = ["private", "deploy", "bearer", "job"]
    candidates = [auth_candidate_for(candidate, token, label_prefix=source) for candidate in order]
    # If the project enables GitLab's "Allow anyone to pull from Package Registry"
    # option, an old or insufficient local token should not prevent no-token
    # downloads.  Keep explicit --token-type behavior strict, but in auto mode
    # fall back to an unauthenticated request after token-shaped attempts fail.
    candidates.append(AuthCandidate("unauthenticated fallback", None, None))
    return candidates


def http_error_excerpt(exc: HTTPError) -> str:
    try:
        body = exc.read(512)
    except OSError:
        body = b""
    text = body.decode("utf-8", errors="replace").strip()
    return f": {text}" if text else ""


def strip_sensitive_headers_for_redirect(headers: dict[str, str], old_url: str, new_url: str) -> dict[str, str]:
    old_host = urlparse(old_url).netloc
    new_host = urlparse(new_url).netloc
    if old_host == new_host:
        return dict(headers)
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in TokenSafeRedirectHandler.SENSITIVE_HEADERS
    }


def python_stream_download(
    *,
    url: str,
    destination: Path,
    headers: dict[str, str],
    timeout: float,
    quiet: bool,
) -> None:
    opener = build_opener(TokenSafeRedirectHandler)
    request = Request(url, headers=headers)
    try:
        with opener.open(request, timeout=timeout) as response, destination.open("wb") as handle:
            total_header = response.headers.get("Content-Length")
            total = int(total_header) if total_header and total_header.isdecimal() else None
            downloaded = 0
            next_report = PROGRESS_STEP_BYTES
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                if not quiet and downloaded >= next_report:
                    print(
                        f"  downloaded {format_bytes(downloaded)}"
                        + (f" / {format_bytes(total)}" if total else ""),
                        file=sys.stderr,
                    )
                    next_report += PROGRESS_STEP_BYTES
            if total is not None and downloaded != total:
                raise DownloadFailure(
                    f"incomplete download: expected {total} bytes, received {downloaded} bytes"
                )
    except HTTPError as exc:
        raise DownloadFailure(
            f"HTTP {exc.code} while downloading{http_error_excerpt(exc)}",
            status=exc.code,
        ) from exc
    except URLError as exc:
        raise DownloadFailure(f"network error while downloading: {exc.reason}") from exc


def curl_config_quote(value: str) -> str:
    clean = value.replace("\r", "").replace("\n", "")
    return '"' + clean.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_curl_config(path: Path, headers: dict[str, str]) -> None:
    lines: list[str] = []
    for key, value in headers.items():
        if key.lower() == "user-agent":
            lines.append(f"user-agent = {curl_config_quote(value)}")
        else:
            lines.append(f"header = {curl_config_quote(f'{key}: {value}')}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def curl_stream_download(
    *,
    url: str,
    destination: Path,
    headers: dict[str, str],
    timeout: float,
    quiet: bool,
) -> None:
    """Download with curl while stripping token headers on cross-host redirects."""

    curl = shutil.which("curl")
    if not curl:
        raise DownloadFailure("curl fallback requested but curl is not installed")

    current_url = url
    current_headers = dict(headers)
    redirects_remaining = 8
    with tempfile.TemporaryDirectory(prefix=".fetch_kb_curl-", dir=str(destination.parent)) as temp_name:
        temp_dir = Path(temp_name)
        while True:
            body_path = temp_dir / "body"
            config_path = temp_dir / "curl-config"
            body_path.unlink(missing_ok=True)
            write_curl_config(config_path, current_headers)
            command = [
                curl,
                "--silent",
                "--show-error",
                "--config",
                str(config_path),
                "--connect-timeout",
                str(max(1, min(int(timeout), 60))),
                "--output",
                str(body_path),
                "--write-out",
                "%{http_code}\t%{redirect_url}",
                current_url,
            ]
            if not quiet:
                print(f"Downloading via curl: {current_url}", file=sys.stderr)
            completed = subprocess.run(command, text=True, capture_output=True)
            if completed.returncode != 0:
                stderr = completed.stderr.strip()
                raise DownloadFailure(f"curl failed while downloading: {stderr or completed.returncode}")

            status_text, _, redirect_url = completed.stdout.partition("\t")
            try:
                status = int(status_text.strip())
            except ValueError as exc:
                raise DownloadFailure(f"curl returned an unparsable HTTP status: {completed.stdout!r}") from exc

            if 200 <= status < 300:
                body_path.replace(destination)
                return

            if status in {301, 302, 303, 307, 308}:
                redirect_url = redirect_url.strip()
                if not redirect_url:
                    raise DownloadFailure(f"HTTP {status} redirect without Location", status=status)
                redirects_remaining -= 1
                if redirects_remaining < 0:
                    raise DownloadFailure("too many redirects while downloading")
                current_headers = strip_sensitive_headers_for_redirect(current_headers, current_url, redirect_url)
                current_url = redirect_url
                continue

            excerpt = ""
            if body_path.exists():
                excerpt_text = body_path.read_bytes()[:512].decode("utf-8", errors="replace").strip()
                excerpt = f": {excerpt_text}" if excerpt_text else ""
            raise DownloadFailure(f"HTTP {status} while downloading{excerpt}", status=status)


def stream_download(
    *,
    url: str,
    destination: Path,
    headers: dict[str, str],
    timeout: float,
    quiet: bool,
    download_tool: str,
) -> None:
    if download_tool == "curl":
        curl_stream_download(
            url=url,
            destination=destination,
            headers=headers,
            timeout=timeout,
            quiet=quiet,
        )
        return

    try:
        python_stream_download(
            url=url,
            destination=destination,
            headers=headers,
            timeout=timeout,
            quiet=quiet,
        )
    except DownloadFailure as exc:
        if download_tool == "python":
            raise
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc) or not shutil.which("curl"):
            raise
        if not quiet:
            print("Python TLS verification failed; retrying download with curl.", file=sys.stderr)
        curl_stream_download(
            url=url,
            destination=destination,
            headers=headers,
            timeout=timeout,
            quiet=quiet,
        )


def download_archive(
    *,
    url: str,
    destination: Path,
    candidates: Iterable[AuthCandidate],
    timeout: float,
    quiet: bool,
    download_tool: str,
) -> None:
    failures: list[str] = []
    candidate_list = list(candidates)
    for candidate in candidate_list:
        if not quiet:
            print(f"Downloading with {candidate.label}: {url}", file=sys.stderr)
        try:
            stream_download(
                url=url,
                destination=destination,
                headers=candidate.headers(),
                timeout=timeout,
                quiet=quiet,
                download_tool=download_tool,
            )
            return
        except DownloadFailure as exc:
            failures.append(f"{candidate.label}: {exc}")
            destination.unlink(missing_ok=True)
            if exc.status not in {401, 403, 404}:
                raise

    details = "\n  - ".join(failures)
    raise SystemExit(
        "failed to download package archive.\n"
        f"attempts:\n  - {details}\n"
        "Check that the release exists and the repository is accessible. "
        "For GitHub: verify the repo and tag with `gh release view`. "
        "For GitLab: enable `Allow anyone to pull from Package Registry`, "
        "run `glab auth login`, set GITLAB_DEPLOY_TOKEN, or pass --token/--token-type."
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path, spec: ArchiveSpec, *, skip_sha256: bool) -> None:
    actual_size = path.stat().st_size
    if actual_size != spec.size_bytes:
        raise SystemExit(
            f"size mismatch for {spec.archive_name}: "
            f"expected {spec.size_bytes} bytes, got {actual_size} bytes"
        )
    if skip_sha256:
        return
    actual_sha = sha256_file(path)
    if actual_sha != spec.sha256:
        raise SystemExit(
            f"sha256 mismatch for {spec.archive_name}: expected {spec.sha256}, got {actual_sha}"
        )


def resolve_zstd_binary(value: str) -> str:
    if os.sep in value:
        path = Path(value).expanduser()
        if path.is_file():
            return str(path)
        raise SystemExit(f"zstd binary not found: {path}")
    found = shutil.which(value)
    if found:
        return found
    raise SystemExit("zstd CLI is required. Install zstd or pass --zstd-bin /path/to/zstd.")


def validate_sqlite_header(path: Path) -> None:
    with path.open("rb") as handle:
        header = handle.read(len(SQLITE_HEADER))
    if header != SQLITE_HEADER:
        raise SystemExit(f"decompressed file is not a SQLite database: {path}")


def decompress_archive(*, zstd_bin: str, archive: Path, output: Path, quiet: bool) -> None:
    if not quiet:
        print(f"Decompressing {archive.name} -> {output.name}", file=sys.stderr)
    command = [zstd_bin, "-d", "-q", "-f", "-o", str(output), str(archive)]
    completed = subprocess.run(command, text=True, capture_output=True)
    if completed.returncode != 0:
        if completed.stderr:
            sys.stderr.write(completed.stderr)
        raise SystemExit(f"zstd decompression failed for {archive}")


def selected_variants(value: str) -> list[str]:
    if value == "all":
        return ALL_VARIANTS
    return [value]


def install_variant(
    *,
    spec: ArchiveSpec,
    dest_dir: Path,
    url: str,
    auths: list[AuthCandidate],
    zstd_bin: str,
    timeout: float,
    force: bool,
    keep_archives: bool,
    skip_sha256: bool,
    quiet: bool,
    download_tool: str,
) -> str:
    target = dest_dir / spec.sqlite_name
    if target.exists() and not force:
        validate_sqlite_header(target)
        return f"skip {spec.variant}: {target} already exists (use --force to replace)"

    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".fetch_kb-", dir=str(dest_dir)) as temp_name:
        temp_dir = Path(temp_name)
        archive_path = temp_dir / spec.archive_name
        sqlite_path = temp_dir / spec.sqlite_name

        download_archive(
            url=url,
            destination=archive_path,
            candidates=auths,
            timeout=timeout,
            quiet=quiet,
            download_tool=download_tool,
        )
        verify_archive(archive_path, spec, skip_sha256=skip_sha256)

        if keep_archives:
            shutil.copy2(archive_path, dest_dir / spec.archive_name)

        decompress_archive(zstd_bin=zstd_bin, archive=archive_path, output=sqlite_path, quiet=quiet)
        validate_sqlite_header(sqlite_path)
        sqlite_path.replace(target)

    return f"installed {spec.variant}: {target}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Download eos-config-assistant slim/lite KB archives and install "
            "the uncompressed SQLite DBs under knowledge/. Supports GitHub "
            "Releases (default) and GitLab Package Registry."
        )
    )
    parser.add_argument(
        "--source",
        choices=["github", "gitlab"],
        default="github",
        help="Download source. Default: github.",
    )
    parser.add_argument(
        "--variant",
        choices=["all", *ALL_VARIANTS],
        default="all",
        help="KB variant to fetch. Default: all (lite and slim).",
    )
    parser.add_argument("--root", type=Path, help="Runtime repository root. Default: auto-detect.")
    parser.add_argument(
        "--dest-dir",
        type=Path,
        help="Directory for uncompressed SQLite DBs. Default: <root>/knowledge.",
    )
    parser.add_argument(
        "--github-repo",
        default=DEFAULT_GITHUB_REPO,
        help=f"GitHub owner/repo. Default: {DEFAULT_GITHUB_REPO}",
    )
    parser.add_argument(
        "--github-tag",
        default=DEFAULT_GITHUB_TAG,
        help=f"GitHub release tag. Default: {DEFAULT_GITHUB_TAG}",
    )
    parser.add_argument("--host", default=DEFAULT_GITLAB_HOST, help=f"GitLab host. Default: {DEFAULT_GITLAB_HOST}")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"GitLab project path or id. Default: {DEFAULT_PROJECT}")
    parser.add_argument("--package", default=DEFAULT_PACKAGE, help=f"Generic package name. Default: {DEFAULT_PACKAGE}")
    parser.add_argument(
        "--package-version",
        default=DEFAULT_PACKAGE_VERSION,
        help=f"Generic package version. Default: {DEFAULT_PACKAGE_VERSION}",
    )
    parser.add_argument(
        "--token",
        help=(
            "Explicit GitLab token. Default auth discovery is env vars, then "
            "`glab auth login` via `glab auth token`, then unauthenticated."
        ),
    )
    parser.add_argument(
        "--token-type",
        choices=["auto", "private", "deploy", "job", "bearer"],
        default="auto",
        help="Token header type. Default: auto, based on token/env source.",
    )
    parser.add_argument("--zstd-bin", default=os.environ.get("ZSTD", "zstd"), help="zstd executable. Default: zstd.")
    parser.add_argument(
        "--download-tool",
        choices=["auto", "python", "curl"],
        default="auto",
        help="Download implementation. Default: auto (Python urllib, with curl fallback for local CA issues).",
    )
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout per request in seconds.")
    parser.add_argument("--force", action="store_true", help="Replace existing SQLite DB files.")
    parser.add_argument(
        "--keep-archives",
        action="store_true",
        help="Keep downloaded .zst archives in the destination directory after decompression.",
    )
    parser.add_argument(
        "--skip-sha256",
        action="store_true",
        help="Skip pinned SHA256 verification. Size is still checked.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress progress messages.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    script_path = Path(__file__)
    root = args.root.expanduser().resolve() if args.root else find_runtime_root(script_path)
    dest_dir = args.dest_dir.expanduser().resolve() if args.dest_dir else root / "knowledge"
    if args.source == "github":
        auths = [AuthCandidate("unauthenticated", None, None)]
    else:
        auths = auth_candidates(cli_token=args.token, token_type=args.token_type, host=args.host)
    zstd_bin = resolve_zstd_binary(args.zstd_bin)

    results: list[str] = []
    for variant in selected_variants(args.variant):
        spec = ARCHIVES[variant]
        if args.source == "github":
            url = build_github_release_url(
                repo=args.github_repo,
                tag=args.github_tag,
                archive_name=spec.archive_name,
            )
        else:
            url = build_package_url(
                host=args.host,
                project=args.project,
                package=args.package,
                package_version=args.package_version,
                archive_name=spec.archive_name,
            )
        results.append(
            install_variant(
                spec=spec,
                dest_dir=dest_dir,
                url=url,
                auths=auths,
                zstd_bin=zstd_bin,
                timeout=args.timeout,
                force=args.force,
                keep_archives=args.keep_archives,
                skip_sha256=args.skip_sha256,
                quiet=args.quiet,
                download_tool=args.download_tool,
            )
        )

    for result in results:
        print(result)
    print(f"KB directory: {dest_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
