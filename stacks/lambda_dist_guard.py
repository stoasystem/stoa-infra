"""Fail-fast verification for the prebuilt backend Lambda dist asset."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys


DEFAULT_BACKEND_ROOT = Path(__file__).resolve().parents[2] / "stoa-backend"
DIST_SCRIPT = "scripts/build_lambda_dist.py"


@dataclass(frozen=True)
class LambdaDistAsset:
    path: Path
    asset_hash: str


def verify_lambda_dist(
    backend_root: Path | None = None,
    *,
    dist_name: str = "dist",
) -> LambdaDistAsset:
    """Verify the backend Lambda dist manifest before CDK assets are read."""
    backend_root = (backend_root or DEFAULT_BACKEND_ROOT).resolve()
    dist_dir = backend_root / dist_name
    script = backend_root / DIST_SCRIPT

    if not script.exists():
        raise RuntimeError(
            "Missing backend Lambda dist verifier at "
            f"{script}. Build/check out stoa-backend before CDK synth."
        )
    if not dist_dir.exists():
        raise RuntimeError(
            "Missing backend Lambda dist directory at "
            f"{dist_dir}. Run `python scripts/build_lambda_dist.py` in stoa-backend."
        )

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--verify-only",
            "--repo-root",
            str(backend_root),
            "--dist",
            str(dist_dir),
        ],
        cwd=backend_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = "\n".join(part for part in (result.stdout.strip(), result.stderr.strip()) if part)
        raise RuntimeError(
            "Backend Lambda dist provenance verification failed before CDK synth.\n"
            "Rebuild with `python scripts/build_lambda_dist.py` in stoa-backend.\n"
            f"{detail}"
        )
    print(result.stdout.strip())
    manifest = json.loads((dist_dir / ".stoa-build-manifest.json").read_text(encoding="utf-8"))
    asset_hash = manifest.get("cdk_asset_hash")
    if not isinstance(asset_hash, str) or len(asset_hash) != 64:
        raise RuntimeError("Backend Lambda dist manifest is missing a valid cdk_asset_hash.")
    return LambdaDistAsset(path=dist_dir, asset_hash=asset_hash)
