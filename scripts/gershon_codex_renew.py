#!/usr/bin/env python3
"""Renew Harvey's Codex login as soon as Codex is willing to.

Codex only rewrites auth.json when last_refresh is older than 8 days, so a
call every 3 days never renewed it and Harvey paused itself. Run every 15 min
(harvey-codex-renew.timer); Codex is called only once the login is >= 8 days
old, so it is a cheap no-op otherwise."""
import json, subprocess, sys
from datetime import datetime, timezone

AUTH = "/home/harvey/.codex/auth.json"


def age_hours(last_refresh, now=None):
    lr = datetime.fromisoformat(last_refresh.replace("Z", "")[:26]).replace(tzinfo=timezone.utc)
    return ((now or datetime.now(timezone.utc)) - lr).total_seconds() / 3600


def main():
    d = json.load(open(AUTH))
    age = age_hours(d["last_refresh"])
    print(f"last_refresh {d['last_refresh']} age {age:.1f}h")
    if age < 8 * 24 + 0.05:
        return 0
    r = subprocess.run(["codex", "-c", "cli_auth_credentials_store=file", "exec",
                        "--skip-git-repo-check", "Reply with OK only."],
                       capture_output=True, text=True, timeout=280)
    print(r.stdout[-300:], r.stderr[-300:])
    print("after:", json.load(open(AUTH)).get("last_refresh"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
