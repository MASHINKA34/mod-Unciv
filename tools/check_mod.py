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
  5. Cross-reference (mod-ci does not do this for extension mods): every
     uniqueTo/replaces/unitType/tech/resource reference exists in mod + vanilla,
     every unique and modifier matches a template from the jar's UniqueType
     class, object-name parameters and `for every [X]` countables exist.

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
    LOG.info("Step 1/5: checking top-level layout as seen by the in-game downloader")
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
    LOG.info("Step 2/5: checking JSON, translations and image file names")
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
    image_count, non_ascii = 0, []
    for image_dir in image_dirs:
        for path in sorted(image_dir.rglob("*")):
            if not path.is_file():
                continue
            name = rel(path)
            if not name.isascii():
                non_ascii.append(name)
            if path.suffix.lower() in IMAGE_EXTENSIONS:
                image_count += 1
            elif path.name != "TexturePacker.settings":
                LOG.warning("%s: not an image, the packer ignores it", name)
    LOG.info("Found %d image(s) in %s", image_count, [p.name for p in image_dirs] or "no Images folder")
    # The mod is Russian-only by design, so object (= image) names are Cyrillic. Unciv docs advise ASCII
    # because some desktop locales read atlases in a non-UTF-8 charset; Android always uses UTF-8.
    if non_ascii:
        LOG.warning("%d image path(s) are non-ASCII (e.g. %s): fine on Android, desktop players need a UTF-8 "
                    "locale; the packed atlas must be UTF-8 (checked below)", len(non_ascii), non_ascii[0])
    for atlas in sorted(REPO_ROOT.glob("*.atlas")):
        if read_utf8_no_bom(atlas) is None:
            errors += 1
        else:
            LOG.debug("%s: valid UTF-8", rel(atlas))
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
    LOG.info("Step 3/5: locating Unciv.jar (requested version=%s, offline=%s)", version or "latest", offline)
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

def invalidate_stale_atlas() -> None:
    """Unciv's packer only compares modification times, so renamed or deleted images leave a stale
    game.atlas behind. Compare region names with the files in Images/ and force a repack on mismatch."""
    images, atlas = REPO_ROOT / "Images", REPO_ROOT / "game.atlas"
    if not images.is_dir() or not atlas.is_file():
        LOG.debug("Atlas freshness check skipped (Images/ or game.atlas missing)")
        return
    expected = {p.relative_to(images).with_suffix("").as_posix() for p in images.rglob("*")
                if p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS}
    text = atlas.read_text(encoding="utf-8", errors="replace").splitlines()
    packed = {line.strip() for line in text
              if line.strip() and not line.startswith((" ", "\t")) and ":" not in line and not line.endswith(".png")}
    if expected == packed:
        LOG.info("game.atlas lists the same %d image(s) as Images/: no forced repack", len(packed))
        return
    LOG.warning("game.atlas is stale: %d image(s) missing from it %s, %d no longer in Images/ %s; deleting "
                "game.atlas and game*.png to force a repack", len(expected - packed), sorted(expected - packed)[:5],
                len(packed - expected), sorted(packed - expected)[:5])
    for path in [atlas, *REPO_ROOT.glob("game*.png")]:
        path.unlink()
        LOG.info("Deleted %s", rel(path))


def run_mod_ci(jar: Path, allow_warnings: bool) -> int:
    LOG.info("Step 4/5: running Unciv mod-ci (pack images + autoupdate uniques + validate ruleset)")
    java = shutil.which("java")
    if not java:
        raise ToolingError("java not found in PATH: install JDK 21 (e.g. Eclipse Temurin 21) and retry")
    version = run_cmd([java, "-version"], check=False)
    LOG.info("Using %s (%s)", java, next((l for l in version.stderr.splitlines() if "version" in l), "unknown"))

    has_git = shutil.which("git") is not None
    before = git_status_snapshot() if has_git else set()
    invalidate_stale_atlas()

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


# --------------------------------------------------------------------------- step 5: cross-reference

VANILLA_DIR = "jsons/Civ V - Gods & Kings/"
UNIQUE_TYPE_CLASS = "com/unciv/models/ruleset/unique/UniqueType.class"
STAT_NAMES = {"Production", "Food", "Gold", "Science", "Culture", "Happiness", "Faith"}
CIV_FILTER_KEYWORDS = {"All", "all", "Major", "City-States", "City-State", "Human player", "AI player"}
SIMPLE_COUNTABLES = {"turns", "year", "Cities", "Units", "Completed Policy branches"}
PLACEHOLDER = re.compile(r"\[[^\]]*\]")
# Template placeholders that must name one concrete object; values: collections that may contain it
NAME_PARAMS = {
    "resource": ("TileResources",), "stockpiledResource": ("TileResources",),
    "stockpile": ("TileResources", "<stat>"), "buildingName": ("Buildings",),
    "promotion": ("UnitPromotions",), "tech": ("Techs",), "era": ("Eras",),
    "terrainName": ("Terrains",), "improvementName": ("TileImprovements",),
}
# (json file, field, collections the value must exist in) for plain object references
OBJECT_REFERENCES = [
    ("Units", "unitType", ("UnitTypes",)), ("Units", "uniqueTo", ("Nations", "<civFilter>")),
    ("Units", "replaces", ("Units",)), ("Units", "upgradesTo", ("Units",)),
    ("Units", "requiredTech", ("Techs",)), ("Units", "obsoleteTech", ("Techs",)),
    ("Units", "requiredResource", ("TileResources",)), ("Units", "promotions", ("UnitPromotions",)),
    ("Buildings", "uniqueTo", ("Nations", "<civFilter>")), ("Buildings", "replaces", ("Buildings",)),
    ("Buildings", "requiredTech", ("Techs",)), ("Buildings", "requiredBuilding", ("Buildings",)),
    ("Buildings", "requiredResource", ("TileResources",)), ("Buildings", "specialistSlots", ("Specialists",)),
    ("TileResources", "revealedBy", ("Techs",)), ("TileResources", "improvement", ("TileImprovements",)),
    ("TileResources", "terrainsCanBeFoundOn", ("Terrains",)),
]


def parse_lenient_json(text: str):
    """JSON plus the libGDX extras Unciv's own files use: // and /* */ comments, trailing commas."""
    out, i, n = [], 0, len(text)

    def skip_blank(j: int) -> int:
        while j < n:
            if text[j] in " \t\r\n":
                j += 1
            elif text.startswith("//", j):
                k = text.find("\n", j)
                j = n if k < 0 else k
            elif text.startswith("/*", j):
                k = text.find("*/", j + 2)
                j = n if k < 0 else k + 2
            else:
                break
        return j

    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                j += 2 if text[j] == "\\" else 1
            out.append(text[i:j + 1])
            i = j + 1
        elif text.startswith("//", i) or text.startswith("/*", i):
            i = skip_blank(i)
        elif c == "," and skip_blank(i + 1) < n and text[skip_blank(i + 1)] in "]}":
            i += 1  # trailing comma
        else:
            out.append(c)
            i += 1
    return json.loads("".join(out))


def class_utf8_constants(data: bytes) -> list[str]:
    """All CONSTANT_Utf8 strings of a .class file (where Kotlin keeps enum template literals)."""
    import struct
    count, pos, strings, index = struct.unpack_from(">H", data, 8)[0], 10, [], 1
    while index < count:
        tag = data[pos]
        if tag == 1:
            length = struct.unpack_from(">H", data, pos + 1)[0]
            strings.append(data[pos + 3:pos + 3 + length].decode("utf-8", errors="replace"))
            pos += 3 + length
        elif tag in (3, 4, 9, 10, 11, 12, 17, 18):
            pos += 5
        elif tag in (5, 6):
            pos += 9
            index += 1  # 8-byte constants take two slots
        elif tag in (7, 8, 16, 19, 20):
            pos += 3
        elif tag == 15:
            pos += 4
        else:
            raise ToolingError(f"Unknown constant pool tag {tag} at offset {pos} in {UNIQUE_TYPE_CLASS}")
        index += 1
    return strings


def object_names(ruleset: dict, collection: str) -> set[str]:
    data = ruleset.get(collection)
    if not isinstance(data, list):
        return set()
    names = set()
    for obj in data:
        if not isinstance(obj, dict):
            continue
        if "name" in obj:
            names.add(obj["name"])
        # Techs.json is a list of columns, each holding its own "techs" list
        names.update(t["name"] for t in obj.get("techs", []) if isinstance(t, dict) and "name" in t)
    return names


def load_reference_data(jar: Path) -> tuple[dict, dict, list[tuple[re.Pattern, list[str], str]]]:
    import zipfile
    started = time.perf_counter()
    vanilla, mod = {}, {}
    with zipfile.ZipFile(jar) as z:
        for name in z.namelist():
            if name.startswith(VANILLA_DIR) and name.endswith(".json") and name.count("/") == 2:
                stem = name[len(VANILLA_DIR):-5]
                try:
                    vanilla[stem] = parse_lenient_json(z.read(name).decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    LOG.warning("Cannot parse vanilla %s from %s (%s): references to it are not checked",
                                name, jar.name, e)
        templates = [s for s in class_utf8_constants(z.read(UNIQUE_TYPE_CLASS)) if s and "\n" not in s]
    for path in sorted((REPO_ROOT / "jsons").glob("*.json")):
        mod[path.stem] = json.loads(path.read_text(encoding="utf-8"))

    compiled = []
    for t in templates:
        parts = PLACEHOLDER.split(t)
        names = [p[1:-1] for p in PLACEHOLDER.findall(t)]
        pattern = r"\[((?:[^\[\]]|\[[^\[\]]*\])*)\]".join(re.escape(p) for p in parts)
        compiled.append((re.compile("^" + pattern + "$"), names, t))
    LOG.info("Loaded vanilla rules (%d files) and %d unique template candidates from %s in %dms",
             len(vanilla), len(compiled), jar.name, (time.perf_counter() - started) * 1000)
    return vanilla, mod, compiled


def split_unique(unique: str) -> tuple[str, list[str]]:
    main, modifiers, depth, current = "", [], 0, ""
    for ch in unique:
        if ch == "<" and depth == 0:
            depth, current = 1, ""
        elif ch == ">" and depth == 1:
            depth = 0
            modifiers.append(current.strip())
        elif depth:
            current += ch
        else:
            main += ch
    return main.strip(), modifiers


def iter_uniques(obj, where: str):
    if isinstance(obj, dict):
        label = f"{where}/{obj['name']}" if "name" in obj else where
        for key, value in obj.items():
            if key in ("uniques", "unitUniques") and isinstance(value, list):
                for unique in value:
                    yield f"{label}.{key}", unique
            else:
                yield from iter_uniques(value, label)
    elif isinstance(obj, list):
        for item in obj:
            yield from iter_uniques(item, where)


def run_cross_reference(jar: Path) -> int:
    LOG.info("Step 5/5: cross-referencing objects and uniques against vanilla rules (mod-ci skips these "
             "for extension mods)")
    vanilla, mod, templates = load_reference_data(jar)
    combined = {c: object_names(vanilla, c) | object_names(mod, c) for c in set(vanilla) | set(mod)}
    combined["Specialists"] = combined.get("Specialists", set())
    errors = 0

    def known(value: str, collections: tuple[str, ...]) -> bool:
        for collection in collections:
            if collection == "<stat>" and value in STAT_NAMES:
                return True
            if collection == "<civFilter>" and value in CIV_FILTER_KEYWORDS:
                return True
            if value in combined.get(collection, set()):
                return True
        return False

    checked_refs = 0
    for file, field, collections in OBJECT_REFERENCES:
        for obj in mod.get(file, []) if isinstance(mod.get(file), list) else []:
            raw = obj.get(field)
            values = list(raw) if isinstance(raw, (list, dict)) else [raw] if raw is not None else []
            for value in values:
                checked_refs += 1
                if not known(value, collections):
                    LOG.error("jsons/%s.json: %s.%s = \"%s\" does not exist in %s (mod + vanilla)",
                              file, obj.get("name"), field, value, "/".join(collections))
                    errors += 1

    checked_uniques = 0
    for file, data in mod.items():
        for where, unique in iter_uniques(data, f"jsons/{file}.json"):
            main, modifiers = split_unique(unique)
            for kind, text in [("unique", main)] + [("modifier", m) for m in modifiers]:
                checked_uniques += 1
                matches = [(names, m.groups()) for pattern, names, _ in templates if (m := pattern.match(text))]
                if not matches:
                    LOG.error("%s: %s \"%s\" matches no Unciv unique template (typo?) in \"%s\"",
                              where, kind, text, unique)
                    errors += 1
                    continue
                names, params = matches[0]
                LOG.debug("%s: %s \"%s\" ok, params=%s", where, kind, text, dict(zip(names, params)))
                for name, value in zip(names, params):
                    if name in NAME_PARAMS and not known(value, NAME_PARAMS[name]):
                        LOG.error("%s: [%s] in \"%s\" must be an existing %s (mod + vanilla)",
                                  where, value, unique, "/".join(NAME_PARAMS[name]))
                        errors += 1
                    if name == "countable" and "[" not in value and not value.lstrip("-").isdigit() \
                            and value not in SIMPLE_COUNTABLES | STAT_NAMES \
                            and value not in combined.get("TileResources", set()):
                        LOG.error("%s: countable [%s] in \"%s\" is neither a number, a stat, a resource nor a "
                                  "known keyword - Unciv would silently count 0", where, value, unique)
                        errors += 1
    LOG.info("Cross-reference: %d object reference(s), %d unique part(s) checked, %d error(s)",
             checked_refs, checked_uniques, errors)
    return errors


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
            LOG.info("Steps 3-5 skipped (--skip-unciv): images NOT packed, ruleset NOT validated by Unciv")
        elif errors:
            LOG.error("Skipping Unciv mod-ci because %d static error(s) must be fixed first", errors)
        else:
            jar = ensure_unciv_jar(args.unciv_version, args.offline)
            errors += run_mod_ci(jar, args.allow_warnings)
            errors += run_cross_reference(jar)
    except ToolingError as e:
        LOG.error("Tooling failure, mod state unknown: %s", e)
        LOG.info("check_mod end: result=TOOLING_FAILURE in %.1fs", time.perf_counter() - started)
        return 2

    result = "OK" if errors == 0 else f"FAILED ({errors} error(s))"
    LOG.info("check_mod end: result=%s in %.1fs", result, time.perf_counter() - started)
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
