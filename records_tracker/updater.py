"""Check GitHub for a newer version and fetch it.

When the program's author pushes a new version to GitHub (bumping VERSION.txt),
each user's copy can see it and offer to update. The check is unauthenticated
(so the GitHub repo, or at least its raw VERSION.txt + branch archive, must be
PUBLIC), best-effort, and fully degrades to "no update" on any error so it can
never break the app or block the UI.

The download is the branch's source archive (a zip wrapping a top-level folder),
which apply_update.py already knows how to flatten — so publishing an update is
just `git push`, no release step required.
"""
from __future__ import annotations

import json
import logging
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .config import PROJECT_ROOT, project_paths

log = logging.getLogger(__name__)

DEFAULT_OWNER = "Pyrogenez"
DEFAULT_REPO = "RecordsTracker"
DEFAULT_BRANCH = "main"
CHECK_INTERVAL_SECONDS = 6 * 3600   # re-check at most this often
NET_TIMEOUT = 8
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # sanity cap (the app is < 1 MB of code)


def _config() -> dict:
    cfg = {"enabled": True, "owner": DEFAULT_OWNER, "repo": DEFAULT_REPO,
           "branch": DEFAULT_BRANCH}
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


def _raw_version_url(cfg: dict) -> str:
    return (f"https://raw.githubusercontent.com/{cfg['owner']}/{cfg['repo']}/"
            f"{cfg['branch']}/VERSION.txt")


def archive_url(cfg: dict | None = None) -> str:
    cfg = cfg or _config()
    return (f"https://codeload.github.com/{cfg['owner']}/{cfg['repo']}/zip/"
            f"refs/heads/{cfg['branch']}")


def repo_url(cfg: dict | None = None) -> str:
    cfg = cfg or _config()
    return f"https://github.com/{cfg['owner']}/{cfg['repo']}"


def latest_remote_version(cfg: dict | None = None) -> str | None:
    cfg = cfg or _config()
    try:
        req = urllib.request.Request(_raw_version_url(cfg),
                                     headers={"User-Agent": "RecordsTracker-updater"})
        with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
            return resp.read(64).decode("utf-8", "replace").strip()
    except Exception as e:  # noqa: BLE001
        log.info("Update check could not reach GitHub: %s", e)
        return None


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
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            checked = datetime.fromisoformat(cached.get("checked_at"))
            if (now - checked).total_seconds() < CHECK_INTERVAL_SECONDS:
                cached["current"] = current
                cached["available"] = bool(cached.get("latest")) and is_newer(
                    cached["latest"], current)
                return cached
        except Exception:
            pass

    cfg = _config()
    result = {"checked_at": now.isoformat(timespec="seconds"), "current": current,
              "latest": None, "available": False, "enabled": bool(cfg.get("enabled")),
              "repo_url": repo_url(cfg)}
    if cfg.get("enabled"):
        latest = latest_remote_version(cfg)
        if latest:
            result["latest"] = latest
            result["available"] = is_newer(latest, current)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(result), encoding="utf-8")
    except OSError:
        pass
    return result


def cached() -> dict:
    """Return the last cached update status WITHOUT any network call (safe to use
    on every page render). Recomputes 'available' against the live local version."""
    current = local_version()
    try:
        c = json.loads(_cache_path().read_text(encoding="utf-8"))
        c["current"] = current
        c["available"] = bool(c.get("latest")) and is_newer(c["latest"], current)
        return c
    except Exception:
        return {"current": current, "latest": None, "available": False,
                "enabled": True, "repo_url": repo_url()}


def download_update(version: str | None = None, cfg: dict | None = None) -> Path:
    """Download the branch source archive into the project folder as
    update-<version>.zip (the name apply_update.py looks for). Returns the path."""
    cfg = cfg or _config()
    version = version or (latest_remote_version(cfg) or "latest")
    dest = PROJECT_ROOT / f"update-{version}.zip"
    req = urllib.request.Request(archive_url(cfg),
                                 headers={"User-Agent": "RecordsTracker-updater"})
    with urllib.request.urlopen(req, timeout=NET_TIMEOUT) as resp:
        total = 0
        tmp = dest.with_suffix(".zip.part")
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
    import os
    os.replace(tmp, dest)
    log.info("Downloaded update %s (%d bytes) -> %s", version, total, dest.name)
    return dest
