"""Check GitHub for a newer version and fetch it.

By default this tracks published GitHub RELEASES (the "release" channel), not the
bleeding edge of the main branch — so a user only ever sees a deliberate, tagged
release, and a stray work-in-progress commit can't auto-deploy to everyone. (Set
update_check.channel = "main" in config.json to track the branch tip instead.)

The check is unauthenticated, so the GitHub repo must be PUBLIC; it is best-effort
and degrades silently to "no update" on any error, so it can never block the UI.
The download is a source archive (a zip wrapping a top-level folder) that
apply_update.py flattens — so publishing an update is: bump VERSION.txt, commit,
tag, push, and cut a GitHub release.
"""
from __future__ import annotations

import json
import logging
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT, project_paths

log = logging.getLogger(__name__)

DEFAULT_OWNER = "Pyrogenez"
DEFAULT_REPO = "RecordsTracker"
DEFAULT_BRANCH = "main"
DEFAULT_CHANNEL = "release"          # "release" (tagged) or "main" (branch tip)
CHECK_INTERVAL_SECONDS = 6 * 3600
NET_TIMEOUT = 8
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024


def _config() -> dict:
    cfg = {"enabled": True, "owner": DEFAULT_OWNER, "repo": DEFAULT_REPO,
           "branch": DEFAULT_BRANCH, "channel": DEFAULT_CHANNEL}
    try:
        raw = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        section = raw.get("update_check") or {}
        if isinstance(section, dict):
            for k in cfg:
                if section.get(k) is not None:
                    cfg[k] = section[k]
    except Exception:
        pass
    return cfg


def local_version() -> str:
    try:
        return (PROJECT_ROOT / "VERSION.txt").read_text(encoding="utf-8").strip()
    except OSError:
        return "0.0.0"


def parse_version(s: str) -> tuple:
    nums = []
    for part in (s or "").strip().lstrip("vV").split("."):
        m = "".join(ch for ch in part if ch.isdigit())
        nums.append(int(m) if m else 0)
    return tuple(nums) or (0,)


def is_newer(remote: str, current: str) -> bool:
    return parse_version(remote) > parse_version(current)


def repo_url(cfg: dict | None = None) -> str:
    cfg = cfg or _config()
    return f"https://github.com/{cfg['owner']}/{cfg['repo']}"


def _req(url: str, accept: str | None = None) -> urllib.request.Request:
    headers = {"User-Agent": "RecordsTracker-updater"}
    if accept:
        headers["Accept"] = accept
    return urllib.request.Request(url, headers=headers)


def _remote_info(cfg: dict) -> dict | None:
    """Return {latest, tag, notes_url, notes, download_url} for the configured
    channel, or None if unreachable / nothing published yet."""
    owner, repo = cfg["owner"], cfg["repo"]
    channel = cfg.get("channel") or DEFAULT_CHANNEL
    try:
        if channel == "main":
            with urllib.request.urlopen(_req(
                f"https://raw.githubusercontent.com/{owner}/{repo}/{cfg['branch']}/VERSION.txt"
            ), timeout=NET_TIMEOUT) as r:
                v = r.read(64).decode("utf-8", "replace").strip()
            if not v:
                return None
            return {"latest": v, "tag": cfg["branch"],
                    "notes_url": f"{repo_url(cfg)}/commits/{cfg['branch']}", "notes": "",
                    "download_url": f"https://codeload.github.com/{owner}/{repo}/zip/refs/heads/{cfg['branch']}"}
        # release channel
        with urllib.request.urlopen(_req(
            f"https://api.github.com/repos/{owner}/{repo}/releases/latest",
            "application/vnd.github+json"), timeout=NET_TIMEOUT) as r:
            rel = json.loads(r.read().decode("utf-8", "replace"))
        tag = (rel.get("tag_name") or "").strip()
        if not tag:
            return None
        return {"latest": tag.lstrip("vV"), "tag": tag,
                "notes_url": rel.get("html_url") or repo_url(cfg),
                "notes": (rel.get("body") or "")[:1500],
                "download_url": f"https://codeload.github.com/{owner}/{repo}/zip/refs/tags/{tag}"}
    except Exception as e:  # noqa: BLE001
        log.info("Update check could not reach GitHub: %s", e)
        return None


def latest_remote_version(cfg: dict | None = None) -> str | None:
    info = _remote_info(cfg or _config())
    return info["latest"] if info else None


def _cache_path() -> Path:
    return project_paths()["data"] / ".update_check.json"


def check(force: bool = False) -> dict:
    """Return cached update status, re-checking GitHub at most every
    CHECK_INTERVAL_SECONDS (or immediately when force=True). Never raises."""
    current = local_version()
    cache_path = _cache_path()
    now = datetime.now(timezone.utc)
    if not force and cache_path.exists():
        try:
            cached_ = json.loads(cache_path.read_text(encoding="utf-8"))
            checked = datetime.fromisoformat(cached_.get("checked_at"))
            if (now - checked).total_seconds() < CHECK_INTERVAL_SECONDS:
                cached_["current"] = current
                cached_["available"] = bool(cached_.get("latest")) and is_newer(
                    cached_["latest"], current)
                return cached_
        except Exception:
            pass

    cfg = _config()
    result = {"checked_at": now.isoformat(timespec="seconds"), "current": current,
              "latest": None, "available": False, "enabled": bool(cfg.get("enabled")),
              "repo_url": repo_url(cfg), "notes_url": repo_url(cfg), "notes": "",
              "channel": cfg.get("channel") or DEFAULT_CHANNEL}
    if cfg.get("enabled"):
        info = _remote_info(cfg)
        if info:
            result.update(latest=info["latest"], notes_url=info["notes_url"],
                          notes=info["notes"], tag=info["tag"],
                          available=is_newer(info["latest"], current))
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def cached() -> dict:
    """Last cached status WITHOUT any network call (safe on every page render)."""
    current = local_version()
    try:
        c = json.loads(_cache_path().read_text(encoding="utf-8"))
        c["current"] = current
        c["available"] = bool(c.get("latest")) and is_newer(c["latest"], current)
        c.setdefault("notes_url", c.get("repo_url"))
        return c
    except Exception:
        return {"current": current, "latest": None, "available": False,
                "enabled": True, "repo_url": repo_url(), "notes_url": repo_url()}


def download_update(version: str | None = None, cfg: dict | None = None) -> Path:
    """Download the latest release/branch source archive into the project folder
    as update-<version>.zip (the name apply_update.py looks for)."""
    cfg = cfg or _config()
    info = _remote_info(cfg)
    if not info:
        raise RuntimeError("Could not determine the latest version to download.")
    version = info["latest"]
    dest = PROJECT_ROOT / f"update-{version}.zip"
    tmp = dest.with_suffix(".zip.part")
    with urllib.request.urlopen(_req(info["download_url"]), timeout=NET_TIMEOUT) as resp:
        total = 0
        with open(tmp, "wb") as fh:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    fh.close()
                    tmp.unlink(missing_ok=True)
                    raise RuntimeError("Update download exceeded the size limit; aborting.")
                fh.write(chunk)
    os.replace(tmp, dest)
    log.info("Downloaded update %s (%d bytes) -> %s", version, total, dest.name)
    return dest
