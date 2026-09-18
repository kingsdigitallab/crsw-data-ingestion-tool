"""Find deposits in staging: every staging/<user>/<id>/_deposit.json."""
from typing import Iterator, Optional

from crsw_web.deposits import CONTROL_NAME, STATUS_COMPLETE, Deposit, DepositStore


def list_deposits(store: DepositStore, user: Optional[str] = None,
                  deposit_id: Optional[str] = None,
                  only_complete: bool = True) -> Iterator[Deposit]:
    """Yield deposits under the staging prefix, oldest created first.
    Filters by user and/or deposit id when given."""
    prefix = store.staging_prefix + "/"
    if user:
        prefix += user + "/"
        if deposit_id:
            prefix += deposit_id + "/"
    paginator = store.client.get_paginator("list_objects_v2")
    found = []
    for page in paginator.paginate(Bucket=store.bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith("/" + CONTROL_NAME):
                continue
            rel = key[len(store.staging_prefix) + 1:].split("/")
            if len(rel) != 3:          # <user>/<id>/_deposit.json exactly
                continue
            u, d = rel[0], rel[1]
            if deposit_id and d != deposit_id:
                continue
            dep = store.load(u, d)
            if dep is None:
                continue
            if only_complete and dep.status != STATUS_COMPLETE:
                continue
            found.append(dep)
    found.sort(key=lambda d: (d.created, d.id))
    for dep in found:
        yield dep
