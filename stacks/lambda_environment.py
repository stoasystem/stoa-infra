"""Merge declared Lambda environment with the live function snapshot.

CloudFormation replaces the entire Environment map on every deploy. Keys that
ops set outside CDK (audit material, checkout origins) would otherwise vanish
and the API would fail to boot, as it did when code was updated without
STRIPE_CHECKOUT_WEB_ORIGINS.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


PRODUCTION_CHECKOUT_ORIGIN = "https://app.stoaedu.ch"
REQUIRED_PRODUCTION_KEYS = (
    "AUTHORIZATION_AUDIT_ACTIVE_KEY",
    "AUTHORIZATION_AUDIT_ACTIVE_KEY_ID",
    "STRIPE_CHECKOUT_WEB_ORIGINS",
)


def checkout_origins_for(env_name: str) -> str:
    if env_name == "production":
        return json.dumps([PRODUCTION_CHECKOUT_ORIGIN], separators=(",", ":"))
    return json.dumps(["http://localhost:5173"], separators=(",", ":"))


def load_live_lambda_environment(function_name: str, *, env_name: str) -> dict[str, str] | None:
    """Read a pre-fetched snapshot. Lookup is done in CI so synth stays testable."""
    require = os.environ.get("STOA_REQUIRE_LIVE_LAMBDA_ENV") == "1" and env_name == "production"
    path_value = os.environ.get("STOA_LIVE_LAMBDA_ENV_FILE", "").strip()
    if not path_value:
        if require:
            raise RuntimeError(
                "production deploy requires STOA_LIVE_LAMBDA_ENV_FILE "
                "(a snapshot of the live Lambda environment maps)"
            )
        return None
    path = Path(path_value)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("live Lambda environment snapshot is unreadable") from exc
    if not isinstance(payload, dict) or function_name not in payload:
        if require:
            raise RuntimeError(f"live Lambda environment snapshot is missing {function_name}")
        return None
    raw = payload[function_name]
    if raw is None:
        # Queried and not deployed yet, so there is no live environment to
        # preserve. A name absent from the snapshot still fails above.
        return None
    if not isinstance(raw, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in raw.items()):
        raise RuntimeError(f"live Lambda environment for {function_name} is malformed")
    return dict(raw)


def merge_lambda_environment(
    declared: dict[str, str],
    live: dict[str, str] | None,
    *,
    env_name: str,
) -> dict[str, str]:
    """Declared keys win; live keys that CDK does not manage are preserved."""
    merged = dict(declared)
    if live:
        for key, value in live.items():
            if key not in merged:
                merged[key] = value
    if env_name == "production" and live is not None:
        missing = [key for key in REQUIRED_PRODUCTION_KEYS if not merged.get(key)]
        if missing:
            raise RuntimeError(
                "refusing to synth a production Lambda environment that would drop "
                + ", ".join(missing)
            )
    return merged
