#!/usr/bin/env python3
"""Pure planning of per-app storage changes; shared cache/models never auto-deleted."""
import argparse
import json
from pathlib import Path

from rebuild import dependency_budget

def _integer(value):
    return type(value) is int and value >= 0

def plan(action, app, ledger, free_bytes, new_app_bytes=0):
    config = dependency_budget.config()
    active = {g["app"] for g in config["install_groups"]}
    if app not in active:
        raise ValueError("unknown/offline app (Auto Script excluded)")
    if action not in {"install", "remove"}:
        raise ValueError("unsupported operation")
    if not all(_integer(n) for n in (free_bytes, new_app_bytes)):
        raise ValueError("invalid measured sizes")
    if not isinstance(ledger, dict) or set(ledger) != {"apps", "shared_uv_bytes", "shared_models"}:
        raise ValueError("invalid storage ledger")
    if not isinstance(ledger["apps"], dict) or not set(ledger["apps"]).issubset(active):
        raise ValueError("unknown ledger app")
    if not _integer(ledger["shared_uv_bytes"]) or not isinstance(ledger["shared_models"], dict):
        raise ValueError("invalid shared accounting")
    total = ledger["shared_uv_bytes"]
    for app_id, usage in ledger["apps"].items():
        if not _integer(usage):
            raise ValueError("invalid app bytes")
        total += usage
    for model_id, model in ledger["shared_models"].items():
        if not isinstance(model_id, str) or not model_id or not isinstance(model, dict) or set(model) != {"bytes", "users", "sha256"}:
            raise ValueError("model must specify immutable identity and owners")
        if not _integer(model["bytes"]) or not isinstance(model["sha256"], str) or len(model["sha256"]) != 64 or any(c not in "0123456789abcdef" for c in model["sha256"]):
            raise ValueError("invalid model hash or size")
        users = model["users"]
        if not isinstance(users, list) or len(set(users)) != len(users) or not set(users).issubset(active):
            raise ValueError("invalid model owners")
        total += model["bytes"]
    if action == "install":
        if app in ledger["apps"]:
            raise ValueError("app already installed; updating requires measured replacement plan")
        projected = total + new_app_bytes
        free_after = free_bytes - new_app_bytes
        # No artificial 30 GB application cap. Admit based on measured host
        # free space after this install, keeping a minimum safety reserve.
        allowed = free_after >= config["minimum_free_bytes"]
        # Note: shared cached wheel downloads and model growth need their own
        # measured reservation as part of new_app_bytes before installation.
        return {"action": action, "app": app, "admitted": allowed,
                "projected_bytes": projected, "free_after_bytes": free_after,
                "shared_resources_deleted": [], "destructive_actions": False}
    if app not in ledger["apps"]:
        raise ValueError("app is not installed")
    users_remaining = {mid: [u for u in model["users"] if u != app]
                       for mid, model in ledger["shared_models"].items()}
    return {"action": action, "app": app, "admitted": True,
            "estimated_reclaimable_app_bytes": ledger["apps"][app],
            "retained_models": list(users_remaining),
            "shared_resources_deleted": [], "requires_backup_and_agent": True,
            "destructive_actions": False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("install", "remove"))
    p.add_argument("--app", required=True)
    p.add_argument("--ledger", required=True, help="measured inventory JSON, never a live mutation")
    p.add_argument("--free-bytes", required=True, type=int)
    p.add_argument("--new-app-bytes", type=int, default=0)
    a = p.parse_args()
    try:
        ledger = json.loads(Path(a.ledger).read_text(encoding="utf8"))
        print(json.dumps(plan(a.action, a.app, ledger, a.free_bytes, a.new_app_bytes), indent=2))
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        p.exit(1, "STORAGE_ADMISSION_BLOCKED: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
