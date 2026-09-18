"""Per-user limits, computed from what is in staging.

The service has no database, so a user's usage is the size of what
they have under staging/<user>/ right now: open and complete deposits
alike. Promotion moves deposits out of staging and frees the quota by
itself. Listing a user's prefix is cheap at pilot scale."""
from dataclasses import dataclass
from typing import Optional

from .config import Settings
from .deposits import CONTROL_NAME, STATUS_OPEN, DepositStore


@dataclass(frozen=True)
class Usage:
    bytes_in_staging: int
    open_deposits: int
    deposits: int


def usage(store: DepositStore, user: str) -> Usage:
    prefix = "%s/%s/" % (store.staging_prefix, user)
    total = 0
    control_keys = []
    paginator = store.client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=store.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            if obj["Key"].endswith("/" + CONTROL_NAME):
                control_keys.append(obj["Key"])
            else:
                total += obj["Size"]
    open_count = 0
    for key in control_keys:
        deposit_id = key[len(prefix):].split("/")[0]
        dep = store.load(user, deposit_id)
        if dep is not None and dep.status == STATUS_OPEN:
            open_count += 1
    return Usage(bytes_in_staging=total, open_deposits=open_count,
                 deposits=len(control_keys))


def remaining_bytes(settings: Settings, use: Usage) -> Optional[int]:
    """Bytes this user may still add, or None when unlimited."""
    if settings.user_quota_bytes is None:
        return None
    return max(0, settings.user_quota_bytes - use.bytes_in_staging)


def quota_message(settings: Settings, use: Usage, wanted: int) -> str:
    return ("this upload of %d bytes would take you over your %d-byte staging "
            "quota (%d bytes currently in staging). Finalise or delete "
            "deposits, or wait for them to be promoted, then try again."
            % (wanted, settings.user_quota_bytes, use.bytes_in_staging))
