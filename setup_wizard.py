"""First-time configuration wizard for Records Tracker.

Runs interactively and writes:

  credentials.json   - portal login (portal_url / username / password)
  config.json        - app settings (including optional anthropic_api_key)

Safe to re-run: existing values are shown as defaults and kept on blank input.
"""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).parent
CREDS_PATH = ROOT / "credentials.json"
CONFIG_PATH = ROOT / "config.json"

DEFAULT_PORTAL_URL = "https://stpetefl.mycusthelp.com/WEBAPP/_rs/Login.aspx"
DEFAULT_CONFIG = {
    "headless": True,
    "download_timeout_seconds": 120,
    "page_load_timeout_seconds": 60,
    "max_pages_to_scrape": 50,
    "polite_delay_seconds": 1.5,
    "download_attachments": True,
    "skip_already_downloaded": True,
    "_models_comment": (
        "Optional. Override the AI model used per task to tune cost vs. quality, "
        "e.g. add  \"models\": {\"summarize\": \"claude-haiku-4-5-20251001\"}  to "
        "make summaries cheaper. Keys: classify, summarize, audit, chat, ask. "
        "Omit this and the program uses its tuned defaults (cheap Haiku for "
        "classification, Sonnet where reasoning matters), which update with the program."
    ),
    "human_delay": {
        "records": {
            "min_seconds": 20,
            "max_seconds": 75,
            "long_pause_chance": 0.10,
            "long_pause_min_seconds": 90,
            "long_pause_max_seconds": 300,
        },
        "pages": {
            "min_seconds": 7,
            "max_seconds": 22,
            "long_pause_chance": 0.05,
            "long_pause_min_seconds": 45,
            "long_pause_max_seconds": 120,
        },
    },
}


def banner() -> None:
    print()
    print("=" * 62)
    print("  Records Tracker - Configuration Wizard")
    print("=" * 62)
    print()
    print("This sets up:")
    print("  1. Your login for the St. Petersburg public records portal.")
    print("  2. (Optional) An Anthropic API key for the AI features.")
    print()
    print("Your login and API key are stored ONLY on this computer,")
    print("in plain text files in this folder. Do not share those files.")
    print()


def prompt(label: str, default: str | None = None, secret: bool = False,
           allow_blank: bool = False) -> str:
    suffix = ""
    if default:
        if secret:
            suffix = " [leave blank to keep existing]"
        else:
            suffix = f" [{default}]"
    while True:
        try:
            if secret:
                value = getpass.getpass(f"  {label}{suffix}: ")
            else:
                value = input(f"  {label}{suffix}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  Cancelled.")
            sys.exit(1)
        if not value:
            if default is not None:
                return default
            if allow_blank:
                return ""
            print("    (this field is required)")
            continue
        return value


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Best-effort: restrict perms. On Windows this is mostly a no-op but
    # does not hurt.
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def configure_credentials() -> None:
    print("-- Portal login --")
    existing = load_json(CREDS_PATH)
    portal_url = prompt(
        "Portal URL",
        default=existing.get("portal_url") or DEFAULT_PORTAL_URL,
    )
    default_user = existing.get("username") or None
    username = prompt("Portal email (username)", default=default_user)
    has_existing_pw = bool(existing.get("password"))
    password = prompt(
        "Portal password",
        default=existing.get("password") if has_existing_pw else None,
        secret=True,
    )

    write_json(CREDS_PATH, {
        "portal_url": portal_url,
        "username": username,
        "password": password,
    })
    print(f"  Saved to {CREDS_PATH.name}")
    print()


def configure_api_key() -> None:
    print("-- AI API key (optional but recommended) --")
    print("  The AI features (Chapter 119 compliance audits, chat about your")
    print("  records, classifications, summaries) need an Anthropic API key.")
    print("  Without a key the scraper still works, but the AI features won't.")
    print("  Get a key here:  https://console.anthropic.com/")
    print()

    existing = load_json(CONFIG_PATH)
    # Seed any missing defaults, but don't overwrite user's existing settings.
    for k, v in DEFAULT_CONFIG.items():
        existing.setdefault(k, v)

    current_key = existing.get("anthropic_api_key") or ""
    masked = ("..." + current_key[-4:]) if current_key else ""
    default_display = masked if masked else None
    api_key = prompt(
        "Anthropic API key (blank to skip)",
        default=default_display,
        allow_blank=True,
    )

    if api_key and api_key != default_display:
        existing["anthropic_api_key"] = api_key.strip()
        print("  API key saved.")
    elif current_key:
        # User pressed Enter - keep the masked default (i.e. existing key)
        existing["anthropic_api_key"] = current_key
        print("  Keeping existing API key.")
    else:
        # User skipped and no existing key
        existing.pop("anthropic_api_key", None)
        print("  No API key saved. AI features will be disabled.")
        print(f"  To add later, edit {CONFIG_PATH.name} and set \"anthropic_api_key\".")

    write_json(CONFIG_PATH, existing)
    print(f"  Settings saved to {CONFIG_PATH.name}")
    print()


def main() -> int:
    banner()
    configure_credentials()
    configure_api_key()
    print("All set. You can close this window.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
