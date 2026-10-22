"""The release the running service was built from, for the page header
and /health, so a deploy can be seen to have happened.

The image carries no git metadata (.git is out of the build context),
so the tag is passed in at build time: `CRSW_VERSION` is a build
argument in deploy/Dockerfile that compose takes from the shell
(`export CRSW_VERSION=$(git describe --tags)` before the build, as the
runbook's upgrade line does). On a laptop with no such variable the
checkout's own `git describe` is used; failing both, "dev"."""
import os
import subprocess
from pathlib import Path
from typing import Mapping, Optional

ENV_NAME = "CRSW_VERSION"
UNKNOWN = "dev"


def release(env: Optional[Mapping[str, str]] = None) -> str:
    env = os.environ if env is None else env
    value = (env.get(ENV_NAME) or "").strip()
    if value and value != "unknown":
        return value
    try:
        out = subprocess.run(["git", "describe", "--tags", "--always", "--dirty"],
                             cwd=str(Path(__file__).resolve().parent.parent),
                             capture_output=True, text=True, timeout=5)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return UNKNOWN
