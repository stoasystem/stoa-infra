"""Write a live Lambda environment snapshot for CDK synth to merge."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


FUNCTIONS = ("stoa-api", "stoa-weekly-report", "stoa-dispatch-reconciler")


def snapshot_lambda_environments(
    *,
    region: str = "eu-central-2",
    runner: object | None = None,
) -> dict[str, dict[str, str] | None]:
    run = runner or subprocess.check_output
    snapshot: dict[str, dict[str, str] | None] = {}
    for name in FUNCTIONS:
        try:
            raw = run(
                [
                    "aws",
                    "lambda",
                    "get-function-configuration",
                    "--function-name",
                    name,
                    "--region",
                    region,
                    "--query",
                    "Environment.Variables",
                    "--output",
                    "json",
                ],
                text=True,
            )
        except subprocess.CalledProcessError:
            # A function CDK is about to create for the first time has no live
            # environment to preserve. Recorded as null so that a name nobody
            # queried is still an error.
            snapshot[name] = None
            continue
        parsed = json.loads(raw)
        if not isinstance(parsed, dict) or any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in parsed.items()
        ):
            raise RuntimeError(f"unexpected environment for {name}")
        snapshot[name] = parsed
    return snapshot


def main() -> int:
    target = Path(os.environ["RUNNER_TEMP"]) / "live-lambda-env.json"
    snapshot = snapshot_lambda_environments()
    target.write_text(json.dumps(snapshot), encoding="utf-8")
    github_env = Path(os.environ["GITHUB_ENV"])
    with github_env.open("a", encoding="utf-8") as handle:
        handle.write(f"STOA_LIVE_LAMBDA_ENV_FILE={target}\n")
        handle.write("STOA_REQUIRE_LIVE_LAMBDA_ENV=1\n")
    for name, values in snapshot.items():
        print(f"{name}: {'not deployed yet' if values is None else f'{len(values)} keys'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
