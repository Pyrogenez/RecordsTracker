"""Load config and credentials from disk."""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Credentials:
    portal_url: str
    username: str
    password: str


@dataclass(frozen=True)
class Config:
    headless: bool
    download_timeout_seconds: int
    page_load_timeout_seconds: int
    max_pages_to_scrape: int
    polite_delay_seconds: float
    download_attachments: bool
    skip_already_downloaded: bool
    # Human-like pacing overrides (empty dict means "use module defaults").
    # Populated from the `human_delay` section of config.json:
    #   { "human_delay": { "records": {...}, "pages": {...} } }
    human_delay_records: dict
    human_delay_pages: dict


def load_credentials(path: Path | None = None) -> Credentials:
    path = path or (PROJECT_ROOT / "credentials.json")
    if not path.exists():
        example = PROJECT_ROOT / "credentials.example.json"
        sys.stderr.write(
            f"ERROR: credentials.json not found at {path}\n"
            f"Copy {example} to credentials.json and fill in your real login.\n"
        )
        raise SystemExit(2)
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    required = {"portal_url", "username", "password"}
    missing = required - raw.keys()
    if missing:
        raise SystemExit(
            f"ERROR: credentials.json is missing required fields: {sorted(missing)}"
        )
    if raw["password"].startswith("your-") or raw["username"].startswith("your-"):
        raise SystemExit(
            "ERROR: credentials.json still has placeholder values. Edit it with real login info."
        )
    return Credentials(
        portal_url=raw["portal_url"].strip(),
        username=raw["username"].strip(),
        password=raw["password"],
    )


def load_config(path: Path | None = None) -> Config:
    path = path or (PROJECT_ROOT / "config.json")
    with path.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    hd = raw.get("human_delay") or {}
    return Config(
        headless=bool(raw.get("headless", True)),
        download_timeout_seconds=int(raw.get("download_timeout_seconds", 120)),
        page_load_timeout_seconds=int(raw.get("page_load_timeout_seconds", 60)),
        max_pages_to_scrape=int(raw.get("max_pages_to_scrape", 50)),
        polite_delay_seconds=float(raw.get("polite_delay_seconds", 1.5)),
        download_attachments=bool(raw.get("download_attachments", True)),
        skip_already_downloaded=bool(raw.get("skip_already_downloaded", True)),
        human_delay_records=dict(hd.get("records") or {}),
        human_delay_pages=dict(hd.get("pages") or {}),
    )


def project_paths() -> dict[str, Path]:
    return {
        "root": PROJECT_ROOT,
        "data": PROJECT_ROOT / "data",
        "downloads": PROJECT_ROOT / "data" / "downloads",
        "database": PROJECT_ROOT / "data" / "records.db",
        "excel": PROJECT_ROOT / "data" / "records_analysis.xlsx",
        "logs": PROJECT_ROOT / "logs",
    }
