"""Promoter configuration from PROMOTER_* environment variables."""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Union

ENV_PREFIX = "PROMOTER_"
REQUIRED = ("S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_BUCKET",
            "STAGING_PREFIX")

Authorised = Union[str, Dict[str, List[str]]]   # "*" or {user: [strands]}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class PromoterConfig:
    s3_endpoint: str
    s3_access_key: str
    s3_secret_key: str
    s3_bucket: str
    staging_prefix: str
    max_object_bytes: Optional[int] = None
    max_deposit_bytes: Optional[int] = None
    authorised: Authorised = "*"
    log_path: str = "promoter.log"
    # Where each run's log lines are kept in the bucket; blank disables.
    audit_prefix: str = "audit/promoter"
    # Where the index of every record in place is written; blank disables.
    index_prefix: str = "index"

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "PromoterConfig":
        env = os.environ if env is None else env
        missing = [ENV_PREFIX + k for k in REQUIRED
                   if not (env.get(ENV_PREFIX + k) or "").strip()]
        if missing:
            raise ConfigError("missing required environment variable(s): %s. "
                              "See .env.promoter.example." % ", ".join(missing))

        def opt_int(name):
            raw = (env.get(ENV_PREFIX + name) or "").strip()
            if not raw:
                return None
            try:
                return int(raw)
            except ValueError:
                raise ConfigError("%s%s must be an integer, got %r"
                                  % (ENV_PREFIX, name, raw))

        raw_auth = (env.get(ENV_PREFIX + "AUTHORISED") or "*").strip()
        if raw_auth == "*":
            authorised: Authorised = "*"
        else:
            try:
                parsed = json.loads(raw_auth)
            except ValueError as e:
                raise ConfigError("%sAUTHORISED must be * or a JSON object "
                                  "{user: [strands]}: %s" % (ENV_PREFIX, e))
            if not isinstance(parsed, dict) or not all(
                    isinstance(v, list) for v in parsed.values()):
                raise ConfigError("%sAUTHORISED must map users to lists of "
                                  "strands" % ENV_PREFIX)
            authorised = parsed
        return cls(
            s3_endpoint=env[ENV_PREFIX + "S3_ENDPOINT"].strip(),
            s3_access_key=env[ENV_PREFIX + "S3_ACCESS_KEY"].strip(),
            s3_secret_key=env[ENV_PREFIX + "S3_SECRET_KEY"].strip(),
            s3_bucket=env[ENV_PREFIX + "S3_BUCKET"].strip(),
            staging_prefix=env[ENV_PREFIX + "STAGING_PREFIX"].strip().strip("/"),
            max_object_bytes=opt_int("MAX_OBJECT_BYTES"),
            max_deposit_bytes=opt_int("MAX_DEPOSIT_BYTES"),
            authorised=authorised,
            log_path=(env.get(ENV_PREFIX + "LOG_PATH") or "promoter.log").strip(),
            audit_prefix=env.get(ENV_PREFIX + "AUDIT_PREFIX", "audit/promoter").strip().strip("/"),
            index_prefix=env.get(ENV_PREFIX + "INDEX_PREFIX", "index").strip().strip("/"),
        )

    def user_may_deposit_to(self, user: str, strand: str) -> bool:
        if self.authorised == "*":
            return True
        allowed = list(self.authorised.get(user, [])) + list(self.authorised.get("*", []))
        return strand in allowed or "*" in allowed
