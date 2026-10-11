#!/usr/bin/env python3
"""Validate this Unciv mod and pack its images, locally or in CI.

Steps:
  1. Layout: simulate what the in-game GitHub downloader receives (git archive
     honours export-ignore) and apply Unciv's own "good vs bad top-level entry"
     rule, which otherwise fails with "Invalid Mod archive structure".
  2. Text files: strict JSON parse of jsons/**/*.json, UTF-8 without BOM,
     translation line format, ASCII-only image paths.
  3. Unciv.jar: official release from github.com/yairm210/Unciv, cached in
     tools/.cache/unciv/<version>/.
  4. `java -jar Unciv.jar mod-ci` in the repo root: packs Images*/ into
     game.atlas + game*.png + Atlases.json, auto-updates deprecated uniques,
     validates the ruleset against vanilla. Files it changed are listed so
     they can be committed (Android cannot pack atlases itself).

Usage:
  python tools/check_mod.py                    # full check with latest Unciv
  python tools/check_mod.py --skip-unciv       # only steps 1-2, no download
  python tools/check_mod.py --unciv-version 4.22.8
  python tools/check_mod.py --offline          # use cached jar only

Env:
  LOG_LEVEL     DEBUG | INFO (default) | WARNING | ERROR
  GITHUB_TOKEN  optional, raises GitHub API rate limit (never logged)

Exit codes: 0 = ok, 1 = mod has errors, 2 = tooling failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

LOG = logging.getLogger("check_mod")

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "tools" / ".cache" / "unciv"
RELEASES_API = "https://api.github.com/repos/yairm210/Unciv/releases"
HTTP_TIMEOUT_S = 30
HTTP_ATTEMPTS = 3
MOD_CI_TIMEOUT_S = 600

# Mirrors goodFolders / goodFiles in Unciv core/.../github/GithubAPI.kt (4.22.x).
GOOD_FOLDERS = [r"Images", r"jsons", r"maps", r"music", r"scenarios", r"sounds",
                r"voices", r"Images\..*", r"\.github"]
GOOD_FILES = [r".*\.atlas", r".*\.png", r"preview\.jpg", r".*\.md", r"Atlases\.json",
              r"\.nomedia", r"license"]
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
# RulesetErrorSeverity names as printed by `mod-ci` (RulesetErrorList.getErrorText)
VALIDATOR_FINDING = re.compile(r"^(OK|WarningOptionsOnly|Warning|ErrorOptionsOnly|Error):(.*)$")
BOM = b"\xef\xbb\xbf"


class ToolingError(Exception):
    """Failure of the check itself (network, java, git), not of the mod."""


# --------------------------------------------------------------------------- logging

class _RunIdFilter(logging.Filter):
    def __init__(self, run_id: str):
        super().__init__()
        self.run_id = run_id

    def filter(self, record: logging.LogRecord) -> bool:
        record.run_id = self.run_id
        return True


def setup_logging() -> str:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except AttributeError:
            pass
    run_id = uuid.uuid4().hex[:8]
    requested = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = logging.getLevelName(requested)
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(_RunIdFilter(run_id))
    handler.setFormatter(logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s [run=%(run_id)s] %(message)s",
        "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level if isinstance(level, int) else logging.INFO)
    if not isinstance(level, int):
        LOG.warning("Unknown LOG_LEVEL=%r, falling back to INFO", requested)
    sys.excepthook = _excepthook
    return run_id


def _excepthook(exc_type, exc, tb) -> None:
    if issubclass(exc_type, KeyboardInterrupt):
        LOG.warning("Interrupted by user (Ctrl+C)")
    else:
        LOG.critical("Unhandled %s: %s", exc_type.__name__, exc, exc_info=(exc_type, exc, tb))
    logging.shutdown()


def rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


# --------------------------------------------------------------------------- helpers

def run_cmd(args: list[str], *, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    LOG.debug("exec: %s (cwd=%s)", " ".join(args), REPO_ROOT)
    started = time.perf_counter()
    try:
        proc = subprocess.run(args, cwd=REPO_ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError as e:
        raise ToolingError(f"Executable not found: {args[0]} ({e})") from e
    except subprocess.TimeoutExpired as e:
        raise ToolingError(f"Command timed out after {timeout}s: {' '.join(args)}") from e
    LOG.debug("exec done in %dms: exit=%d stdout=%dB stderr=%dB", (time.perf_counter() - started) * 1000,
              proc.returncode, len(proc.stdout), len(proc.stderr))
    if check and proc.returncode != 0:
        raise ToolingError(f"Command failed (exit {proc.returncode}): {' '.join(args)}\n{proc.stderr.strip()[:2000]}")
    return proc


def git_status_snapshot() -> set[str]:
    proc = run_cmd(["git", "status", "--porcelain=v1", "-uall"])
    return {line for line in proc.stdout.splitlines() if line.strip()}


# --------------------------------------------------------------------------- step 1: layout

def check_layout() -> int:
    LOG.info("Step 1/4: checking top-level layout as seen by the in-game downloader")
    entries = sorted(p for p in REPO_ROOT.iterdir() if p.name != ".git")
    names = [p.name for p in entries]
    LOG.debug("Top-level entries in working tree (%d): %s", len(names), names)

    ignored: set[str] = set()
    export_ignored: set[str] = set()
    if shutil.which("git"):
        proc = run_cmd(["git", "check-ignore", "--", *names], check=False)
        ignored = {line.strip().rstrip("/") for line in proc.stdout.splitlines() if line.strip()}
        proc = run_cmd(["git", "check-attr", "export-ignore", "--", *names])
        for line in proc.stdout.splitlines():
            path, _, value = (part.strip() for part in line.split(":", 2))
            if value == "set":
                export_ignored.add(path)
    else:
        LOG.warning("git not found in PATH: cannot apply .gitignore/export-ignore, treating every entry as shipped")
    LOG.debug("gitignored=%s export-ignored=%s", sorted(ignored), sorted(export_ignored))

    good, bad = [], []
    for path in entries:
        if path.name in ignored or path.name in export_ignored:
            continue
        patterns = GOOD_FOLDERS if path.is_dir() else GOOD_FILES
        is_good = any(re.fullmatch(p, path.name, re.IGNORECASE) for p in patterns)
        (good if is_good else bad).append(path.name + ("/" if path.is_dir() else ""))
    LOG.info("Shipped top-level entries: good=%d %s, unknown=%d %s", len(good), good, len(bad), bad)

    if not good or len(good) <= len(bad):
        LOG.error("In-game download would fail with 'Invalid Mod archive structure': unknown top-level entries "
                  "%s must be fewer than known ones %s. Add dev-only entries to .gitattributes as "
                  "'/<name> export-ignore'.", bad, good)
        return 1
    if bad:
        LOG.warning("Unknown top-level entries will be shipped to players: %s (consider export-ignore)", bad)
    if not any(n.rstrip("/") == "jsons" or n.startswith("Images") for n in good):
        LOG.warning("Mod has neither jsons/ nor Images*/ yet: it installs but changes nothing")
    return 0


# --------------------------------------------------------------------------- step 2: text files

def read_utf8_no_bom(path: Path) -> str | None:
    data = path.read_bytes()
    if data.startswith(BOM):
        LOG.error("%s: starts with a UTF-8 BOM, Unciv may fail to parse it. Re-save as UTF-8 without BOM",
                  rel(path))
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as e:
        LOG.error("%s: not valid UTF-8 at byte %d (%s)", rel(path), e.start, e.reason)
        return None


def check_text_files() -> int:
    LOG.info("Step 2/4: checking JSON, translations and image file names")
    errors = 0

    json_files = sorted((REPO_ROOT / "jsons").rglob("*.json")) if (REPO_ROOT / "jsons").is_dir() else []
    LOG.info("Parsing %d JSON file(s) under jsons/", len(json_files))
    for path in json_files:
        text = read_utf8_no_bom(path)
        if text is None:
            errors += 1
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            LOG.error("%s:%d:%d invalid JSON: %s (strict JSON required, no comments/trailing commas)",
                      rel(path), e.lineno, e.colno, e.msg)
            errors += 1
            continue
        count = len(data) if isinstance(data, (list, dict)) else 1
        LOG.debug("%s: ok, %dB, top-level %s with %d item(s)", rel(path), len(text.encode()), type(data).__name__,
                  count)
        if isinstance(data, list):
            unnamed = [i for i, item in enumerate(data) if not (isinstance(item, dict) and item.get("name"))]
            if unnamed:
                LOG.warning("%s: %d object(s) without \"name\" at index %s", rel(path), len(unnamed), unnamed[:10])

    props = sorted((REPO_ROOT / "jsons" / "translations").glob("*.properties")) \
        if (REPO_ROOT / "jsons" / "translations").is_dir() else []
    LOG.info("Checking %d translation file(s)", len(props))
    for path in props:
        text = read_utf8_no_bom(path)
        if text is None:
            errors += 1
            continue
        malformed = [n for n, line in enumerate(text.splitlines(), 1)
                     if line.strip() and not line.lstrip().startswith("#") and " = " not in line]
        if malformed:
            LOG.warning("%s: %d line(s) without ' = ' separator, will be ignored by Unciv: lines %s",
                        rel(path), len(malformed), malformed[:20])
        LOG.debug("%s: ok, %d line(s)", rel(path), len(text.splitlines()))

    atlases = REPO_ROOT / "Atlases.json"
    if atlases.exists() and read_utf8_no_bom(atlases) is None:
        errors += 1

    image_dirs = sorted(p for p in REPO_ROOT.iterdir() if p.is_dir() and re.fullmatch(r"Images(\..+)?", p.name))
    image_count = 0
    for image_dir in image_dirs:
        for path in sorted(image_dir.rglob("*")):
            if not path.is_file():
                continue
            name = rel(path)
            if not name.isascii():
                LOG.error("%s: non-ASCII path, the texture packer/atlas requires ASCII-only names", name)
                errors += 1
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                image_count += 1
            elif path.name != "TexturePacker.settings":
                LOG.warning("%s: not an image, the packer ignores it", name)
    LOG.info("Found %d image(s) in %s", image_count, [p.name for p in image_dirs] or "no Images folder")
    return errors


# --------------------------------------------------------------------------- step 3: Unciv.jar

def http_get(url: str, *, accept: str) -> urllib.response.addinfourl:
    headers = {"Accept": accept, "User-Agent": "mod-Unciv-check"}
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(1, HTTP_ATTEMPTS + 1):
        started = time.perf_counter()
        LOG.debug("GET %s (attempt %d/%d, auth=%s)", url, attempt, HTTP_ATTEMPTS,
                  "token" if "Authorization" in headers else "none")
        try:
            resp = urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=HTTP_TIMEOUT_S)
            LOG.debug("GET %s -> %d in %dms", url, resp.status, (time.perf_counter() - started) * 1000)
            return resp
        except urllib.error.HTTPError as e:
            LOG.warning("GET %s -> HTTP %d %s (attempt %d/%d)", url, e.code, e.reason, attempt, HTTP_ATTEMPTS)
            if e.code == 403:
                LOG.warning("GitHub API rate limit is likely exhausted; set GITHUB_TOKEN or retry later")
            if e.code < 500 and e.code != 429:
                raise ToolingError(f"GET {url} failed: HTTP {e.code} {e.reason}") from e
            last = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            LOG.warning("GET %s failed: %s: %s (attempt %d/%d)", url, type(e).__name__,
                        getattr(e, "reason", e), attempt, HTTP_ATTEMPTS)
            last = e
        if attempt < HTTP_ATTEMPTS:
            delay = 2 ** attempt
            LOG.info("Retrying in %ds", delay)
            time.sleep(delay)
    raise ToolingError(f"GET {url} failed after {HTTP_ATTEMPTS} attempts: {last}") from last


def newest_cached_jar() -> Path | None:
    jars = sorted(CACHE_DIR.glob("*/Unciv.jar"), key=lambda p: p.stat().st_mtime, reverse=True)
    return jars[0] if jars else None


def ensure_unciv_jar(version: str | None, offline: bool) -> Path:
    LOG.info("Step 3/4: locating Unciv.jar (requested version=%s, offline=%s)", version or "latest", offline)
    if version and (CACHE_DIR / version / "Unciv.jar").is_file():
        jar = CACHE_DIR / version / "Unciv.jar"
        LOG.info("Cache hit: %s", rel(jar))
        return jar
    if offline:
        jar = newest_cached_jar()
        if not jar:
            raise ToolingError(f"--offline given but no Unciv.jar cached in {rel(CACHE_DIR)}")
        LOG.info("Offline mode: using cached %s", rel(jar))
        return jar

    url = f"{RELEASES_API}/tags/{version}" if version else f"{RELEASES_API}/latest"
    try:
        with http_get(url, accept="application/vnd.github+json") as resp:
            release = json.load(resp)
    except ToolingError as e:
        jar = newest_cached_jar()
        if jar and not version:
            LOG.warning("Cannot query Unciv releases (%s); falling back to cached %s", e, rel(jar))
            return jar
        raise

    tag = release["tag_name"]
    asset = next((a for a in release.get("assets", []) if a["name"] == "Unciv.jar"), None)
    if not asset:
        raise ToolingError(f"Release {tag} has no Unciv.jar asset")
    size = int(asset["size"])
    jar = CACHE_DIR / tag / "Unciv.jar"
    LOG.info("Unciv release %s published %s, Unciv.jar %.1f MB", tag, release.get("published_at"), size / 1e6)
    if jar.is_file() and jar.stat().st_size == size:
        LOG.info("Cache hit: %s", rel(jar))
        return jar
    if jar.is_file():
        LOG.warning("Cached %s has size %d, expected %d: re-downloading", rel(jar), jar.stat().st_size, size)

    jar.parent.mkdir(parents=True, exist_ok=True)
    part = jar.with_suffix(".jar.part")
    LOG.info("Downloading %s -> %s", asset["browser_download_url"], rel(jar))
    started = time.perf_counter()
    done, next_report = 0, 10
    with http_get(asset["browser_download_url"], accept="application/octet-stream") as resp, part.open("wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)
            done += len(chunk)
            if size and done * 100 // size >= next_report:
                LOG.info("Download progress %d%% (%.1f/%.1f MB)", done * 100 // size, done / 1e6, size / 1e6)
                next_report += 10
    elapsed = time.perf_counter() - started
    if done != size:
        part.unlink(missing_ok=True)
        raise ToolingError(f"Unciv.jar download incomplete: got {done} of {size} bytes")
    part.replace(jar)
    LOG.info("Downloaded Unciv.jar %s: %.1f MB in %.1fs (%.1f MB/s)", tag, done / 1e6, elapsed,
             done / 1e6 / max(elapsed, 0.001))
    return jar


# --------------------------------------------------------------------------- step 4: mod-ci

def run_mod_ci(jar: Path, allow_warnings: bool) -> int:
    LOG.info("Step 4/4: running Unciv mod-ci (pack images + autoupdate uniques + validate ruleset)")
    java = shutil.which("java")
    if not java:
        raise ToolingError("java not found in PATH: install JDK 21 (e.g. Eclipse Temurin 21) and retry")
    version = run_cmd([java, "-version"], check=False)
    LOG.info("Using %s (%s)", java, next((l for l in version.stderr.splitlines() if "version" in l), "unknown"))

    has_git = shutil.which("git") is not None
    before = git_status_snapshot() if has_git else set()

    # Force UTF-8 console output, otherwise Windows code pages turn Unciv's bracket glyphs into '?'
    cmd = [java, "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-jar", str(jar), "mod-ci"]
    LOG.info("exec: java -jar %s mod-ci (cwd=%s, timeout=%ds)", rel(jar), REPO_ROOT, MOD_CI_TIMEOUT_S)
    started = time.perf_counter()
    try:
        proc = subprocess.run(cmd, cwd=REPO_ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                              encoding="utf-8", errors="replace", timeout=MOD_CI_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise ToolingError(f"mod-ci timed out after {MOD_CI_TIMEOUT_S}s") from e
    elapsed_ms = (time.perf_counter() - started) * 1000

    # Validator findings look like "<Severity>: <text>", suggestions continue on indented lines.
    # mod-ci exits 0 even for warnings and misspelled uniques, so findings are counted here.
    lines = [l for l in proc.stdout.splitlines() if l.strip()]
    findings: list[list[str]] = []
    for line in lines:
        match = VALIDATOR_FINDING.match(line)
        if line.startswith("Picked up JAVA_TOOL_OPTIONS"):
            LOG.debug("mod-ci | %s", line)
        elif match:
            findings.append([match.group(1), match.group(2).strip(": ")])
        elif findings and line[:1].isspace():
            findings[-1][1] += " | " + line.strip()
        else:
            LOG.info("mod-ci | %s", line)
    LOG.info("mod-ci finished in %dms with exit code %d, %d output line(s), %d validator finding(s)",
             elapsed_ms, proc.returncode, len(lines), len(findings))
    errors = [f for f in findings if f[0].startswith("Error")]
    for severity, text in findings:
        LOG.log(logging.ERROR if severity.startswith("Error") else logging.WARNING,
                "Unciv validator [%s]: %s", severity, text)

    if has_git:
        changed = sorted(git_status_snapshot() - before)
        for entry in changed:
            LOG.info("mod-ci changed: %s", entry)
        if changed:
            LOG.warning("mod-ci modified %d file(s): review with 'git diff' and commit them "
                        "(packed atlas must be in the repo for Android)", len(changed))
        else:
            LOG.info("mod-ci changed no files (atlas already up to date)")

    if proc.returncode not in (0, 1):
        raise ToolingError(f"mod-ci crashed with exit code {proc.returncode}")
    if proc.returncode == 1 or errors:
        LOG.error("Unciv ruleset validation reported %d error(s), %d warning(s)", len(errors),
                  len(findings) - len(errors))
        return 1
    if findings and not allow_warnings:
        LOG.error("Unciv ruleset validation reported %d warning(s); fix them or rerun with --allow-warnings",
                  len(findings))
        return 1
    LOG.info("Unciv ruleset validation passed with %d warning(s)", len(findings))
    return 0


# --------------------------------------------------------------------------- main

def main() -> int:
    parser = argparse.ArgumentParser(description="Validate and pack this Unciv mod.")
    parser.add_argument("--skip-unciv", action="store_true", help="only static checks, no Unciv.jar")
    parser.add_argument("--unciv-version", help="Unciv release tag, e.g. 4.22.8 (default: latest)")
    parser.add_argument("--offline", action="store_true", help="use the newest cached Unciv.jar, no network")
    parser.add_argument("--allow-warnings", action="store_true",
                        help="do not fail on Unciv validator warnings (errors still fail)")
    args = parser.parse_args()

    run_id = setup_logging()
    started = time.perf_counter()
    LOG.info("check_mod start: run=%s repo=%s python=%s platform=%s skip_unciv=%s unciv_version=%s offline=%s",
             run_id, REPO_ROOT, sys.version.split()[0], sys.platform, args.skip_unciv,
             args.unciv_version or "latest", args.offline)

    try:
        errors = check_layout()
        errors += check_text_files()
        if args.skip_unciv:
            LOG.info("Steps 3-4 skipped (--skip-unciv): images NOT packed, ruleset NOT validated by Unciv")
        elif errors:
            LOG.error("Skipping Unciv mod-ci because %d static error(s) must be fixed first", errors)
        else:
            errors += run_mod_ci(ensure_unciv_jar(args.unciv_version, args.offline), args.allow_warnings)
    except ToolingError as e:
        LOG.error("Tooling failure, mod state unknown: %s", e)
        LOG.info("check_mod end: result=TOOLING_FAILURE in %.1fs", time.perf_counter() - started)
        return 2

    result = "OK" if errors == 0 else f"FAILED ({errors} error(s))"
    LOG.info("check_mod end: result=%s in %.1fs", result, time.perf_counter() - started)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
