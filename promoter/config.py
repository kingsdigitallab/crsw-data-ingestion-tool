"""Promoter configuration from PROMOTER_* environment variables."""
import json
import os
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple, Union

from crsw_deposit import keys

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
    # The KCL LLM platform, for the embeddings behind meaning-based search
    # (finding-and-reuse.md §5). Off unless both URL and key are set.
    llm_base_url: str = ""
    llm_api_key: str = ""
    # A blank model name switches that function off.
    llm_embed_model: str = "arc:embedvl"
    # Keep the first N numbers of each vector (0 = all). Half the numbers
    # at half precision is a quarter of the storage; see crsw_web/llm.py.
    llm_embed_dims: int = 1024
    # Which sensitivities' metadata (and, later, contents) may be sent to
    # the platform. Green only until the Centre allows amber.
    llm_sensitivities: Tuple[str, ...] = ("green",)

    # Searching inside documents (finding-and-reuse.md §7): off unless
    # PROMOTER_PASSAGES=1, and then only for datasets of the allowed
    # sensitivities that are not excluded. Caps so one mistaken deposit
    # cannot fill the VM: a file over the size cap is skipped, and the
    # walk stops at the passage cap.
    passages: bool = False
    passages_max_file_bytes: int = 50 * 1024 * 1024
    passages_max_per_dataset: int = 20000
    passages_exclude: Tuple[str, ...] = ()

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key)

    @property
    def embed_enabled(self) -> bool:
        return self.llm_enabled and bool(self.llm_embed_model)

    @property
    def passages_enabled(self) -> bool:
        # Passages live under the index prefix; blank means no index at all.
        return self.passages and self.embed_enabled and bool(self.index_prefix)

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
        llm_url = (env.get(ENV_PREFIX + "LLM_BASE_URL") or "").strip()
        llm_key = (env.get(ENV_PREFIX + "LLM_API_KEY") or "").strip()
        if bool(llm_url) != bool(llm_key):
            raise ConfigError("%sLLM_BASE_URL and %sLLM_API_KEY must be set together "
                              "(both blank turns embeddings off)" % (ENV_PREFIX, ENV_PREFIX))
        sens = tuple(s.strip() for s in (env.get(ENV_PREFIX + "LLM_SENSITIVITIES") or "green").split(",")
                     if s.strip())
        bad = [s for s in sens if s not in keys.SENSITIVITIES]
        if bad:
            raise ConfigError("%sLLM_SENSITIVITIES may only name %s, got %r"
                              % (ENV_PREFIX, "/".join(keys.SENSITIVITIES), ", ".join(bad)))
        dims = opt_int("LLM_EMBED_DIMS")
        max_file = opt_int("PASSAGES_MAX_FILE_BYTES")
        max_passages = opt_int("PASSAGES_MAX_PER_DATASET")
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
            llm_base_url=llm_url,
            llm_api_key=llm_key,
            llm_embed_model=env.get(ENV_PREFIX + "LLM_EMBED_MODEL", "arc:embedvl").strip(),
            llm_embed_dims=1024 if dims is None else dims,
            llm_sensitivities=sens,
            passages=(env.get(ENV_PREFIX + "PASSAGES") or "").strip() == "1",
            passages_max_file_bytes=cls.passages_max_file_bytes if max_file is None else max_file,
            passages_max_per_dataset=(cls.passages_max_per_dataset if max_passages is None
                                      else max_passages),
            passages_exclude=tuple(
                p.strip().strip("/") for p in
                (env.get(ENV_PREFIX + "PASSAGES_EXCLUDE") or "").split(",") if p.strip()),
        )

    def user_may_deposit_to(self, user: str, strand: str) -> bool:
        if self.authorised == "*":
            return True
        allowed = list(self.authorised.get(user, [])) + list(self.authorised.get("*", []))
        return strand in allowed or "*" in allowed
