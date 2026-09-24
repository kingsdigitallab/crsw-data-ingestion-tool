"""Service configuration, from the environment only.

The same image serves the KCL-only phase and the pilot; only the
environment differs (plan: "config by environment"). Secrets arrive as
env vars or a mounted .env file, never from the image or the repo."""
import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Mapping, Optional, Tuple

ENV_PREFIX = "CRSW_"

REQUIRED = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
            "STAGING_PREFIX")
# placeholder: local development. proxy: production behind the KCL
# reverse proxy, which does sign-in and group restriction and forwards
# the user in a header. There is deliberately no in-app OIDC.
AUTH_MODES = ("placeholder", "proxy")


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
    # proxy mode
    proxy_user_header: str = ""            # discovered on first deploy
    proxy_groups_header: str = ""          # optional
    proxy_required_group: str = "er_prj_kdl_slavery"
    proxy_username_pattern: str = ""       # optional regex, group 1 = username
    trusted_proxy_cidrs: Tuple[str, ...] = ()
    debug_headers: bool = False            # first-deploy diagnostic only
    # limits
    user_quota_bytes: Optional[int] = None
    user_max_open_deposits: int = 5
    max_members_per_deposit: int = 10000
    # how often the running service re-fetches the vocabulary; 0 = only
    # at start-up
    vocab_refresh_seconds: int = 600

    def trusted_proxy_networks(self):
        return [ipaddress.ip_network(c, strict=False) for c in self.trusted_proxy_cidrs]

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        def get(name, default=""):
            return (env.get(ENV_PREFIX + name) or default).strip()

        def opt_int(name, default=None):
            raw = get(name)
            if not raw:
                return default
            try:
                return int(raw)
            except ValueError:
                raise ConfigError("%s%s must be an integer, got %r" % (ENV_PREFIX, name, raw))

        missing = [ENV_PREFIX + k for k in REQUIRED if not get(k)]
        if missing:
            raise ConfigError(
                "missing required environment variable(s): %s. "
                "See .env.example." % ", ".join(missing))
        auth_mode = get("AUTH_MODE", "placeholder")
        if auth_mode not in AUTH_MODES:
            raise ConfigError("%sAUTH_MODE must be one of %s, got %r"
                              % (ENV_PREFIX, "/".join(AUTH_MODES), auth_mode))
        prefix = get("STAGING_PREFIX").strip("/")
        if not prefix.startswith("staging"):
            # Belt and braces: the key is meant to be staging-only, but
            # the config must not point elsewhere either.
            raise ConfigError("%sSTAGING_PREFIX must be under staging/, got %r"
                              % (ENV_PREFIX, prefix))
        user_header = get("PROXY_USER_HEADER")
        if auth_mode == "proxy" and not user_header:
            raise ConfigError("%sPROXY_USER_HEADER is required in proxy mode "
                              "(discover it with CRSW_DEBUG_HEADERS=1 and "
                              "GET /auth/headers)" % ENV_PREFIX)
        cidrs = tuple(c.strip() for c in get("TRUSTED_PROXY_CIDRS").split(",") if c.strip())
        for c in cidrs:
            try:
                ipaddress.ip_network(c, strict=False)
            except ValueError:
                raise ConfigError("%sTRUSTED_PROXY_CIDRS: %r is not a network" % (ENV_PREFIX, c))
        pattern = get("PROXY_USERNAME_PATTERN")
        if pattern:
            try:
                import re
                if re.compile(pattern).groups < 1:
                    raise ConfigError("%sPROXY_USERNAME_PATTERN needs one capture group"
                                      % ENV_PREFIX)
            except re.error as e:
                raise ConfigError("%sPROXY_USERNAME_PATTERN: %s" % (ENV_PREFIX, e))
        return cls(
            s3_endpoint=get("S3_ENDPOINT"),
            s3_access_key=get("S3_ACCESS_KEY"),
            s3_secret_key=get("S3_SECRET_KEY"),
            s3_bucket=get("S3_BUCKET"),
            staging_prefix=prefix,
            auth_mode=auth_mode,
            vocab_refresh_seconds=opt_int("VOCAB_REFRESH_SECONDS", 600),
            dev_user=get("DEV_USER", "k1078591"),
            max_body_bytes=opt_int("MAX_BODY_BYTES", cls.max_body_bytes),
            proxy_user_header=user_header,
            proxy_groups_header=get("PROXY_GROUPS_HEADER"),
            proxy_required_group=get("PROXY_REQUIRED_GROUP", cls.proxy_required_group),
            proxy_username_pattern=pattern,
            trusted_proxy_cidrs=cidrs,
            debug_headers=get("DEBUG_HEADERS") == "1",
            user_quota_bytes=opt_int("USER_QUOTA_BYTES"),
            user_max_open_deposits=opt_int("USER_MAX_OPEN_DEPOSITS", cls.user_max_open_deposits),
            max_members_per_deposit=opt_int("MAX_MEMBERS_PER_DEPOSIT", cls.max_members_per_deposit),
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
