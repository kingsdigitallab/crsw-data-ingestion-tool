"""Service configuration, from the environment only.

The same image serves the KCL-only phase and the pilot; only the
environment differs (plan: "config by environment"). Secrets arrive as
env vars or a mounted .env file, never from the image or the repo."""
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

ENV_PREFIX = "CRSW_"

REQUIRED = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
            "STAGING_PREFIX")
AUTH_MODES = ("placeholder", "oidc")


class ConfigError(RuntimeError):
    """Raised at startup with a message that names every missing or
    invalid variable, so a misconfigured container fails once, clearly,
    rather than on the first request."""


@dataclass(frozen=True)
class Settings:
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    staging_prefix: str          # no trailing slash, e.g. "staging/_test"
    auth_mode: str = "placeholder"
    dev_user: str = "k1078591"   # placeholder-mode identity
    max_body_bytes: int = 5 * 1024 ** 3

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env
        missing = [ENV_PREFIX + k for k in REQUIRED
                   if not (env.get(ENV_PREFIX + k) or "").strip()]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): %s. "
                "See .env.example." % ", ".join(missing))
        auth_mode = env.get(ENV_PREFIX + "AUTH_MODE", "placeholder").strip()
        if auth_mode not in AUTH_MODES:
            raise ConfigError("%sAUTH_MODE must be one of %s, got %r"
                              % (ENV_PREFIX, "/".join(AUTH_MODES), auth_mode))
        prefix = env[ENV_PREFIX + "STAGING_PREFIX"].strip().strip("/")
        if not prefix.startswith("staging"):
            # Belt and braces: the key is meant to be staging-only, but
            # the config must not point elsewhere either.
            raise ConfigError("%sSTAGING_PREFIX must be under staging/, got %r"
                              % (ENV_PREFIX, prefix))
        max_body = env.get(ENV_PREFIX + "MAX_BODY_BYTES")
        return cls(
            s3_endpoint=env[ENV_PREFIX + "S3_ENDPOINT"].strip(),
            s3_access_key=env[ENV_PREFIX + "S3_ACCESS_KEY"].strip(),
            s3_secret_key=env[ENV_PREFIX + "S3_SECRET_KEY"].strip(),
            s3_bucket=env[ENV_PREFIX + "S3_BUCKET"].strip(),
            staging_prefix=prefix,
            auth_mode=auth_mode,
            dev_user=env.get(ENV_PREFIX + "DEV_USER", "k1078591").strip(),
            max_body_bytes=int(max_body) if max_body else cls.max_body_bytes,
        )


def load_dotenv(path=".env", env: Optional[dict] = None) -> int:
    """Read KEY=VALUE lines into `env` (default os.environ) without
    overriding values already set. Local-development convenience only;
    compose passes env_file itself. Returns the number of keys set."""
    env = os.environ if env is None else env
    p = Path(path)
    if not p.is_file():
        return 0
    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in env:
            env[key] = value
            count += 1
    return count
