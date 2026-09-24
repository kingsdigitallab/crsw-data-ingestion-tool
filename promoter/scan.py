"""Find deposits in staging: every staging/<user>/<id>/_deposit.json;
and (r9) find datasets in place: every <strand>/<project>/<sensitivity>/
<state>/<dataset>/dataset.<dataset>.json."""
import re
from dataclasses import dataclass
from typing import Iterator, Optional

from crsw_deposit import keys
from crsw_web.deposits import CONTROL_NAME, STATUS_COMPLETE, Deposit, DepositStore

_RECORD_KEY_RE = re.compile(
    r"^(?P<prefix>(%s)/[a-z0-9-]+/(%s)/(%s)/(?P<dataset>[a-z0-9-]+))"
    r"/dataset\.(?P=dataset)\.json$" % (
        "|".join(keys.STRANDS), "|".join(keys.SENSITIVITIES),
        "|".join(keys.STATES)))


@dataclass(frozen=True)
class DatasetRef:
    prefix: str        # the five-part identifier
    record_key: str


def list_datasets(client, bucket: str) -> Iterator[DatasetRef]:
    """Every dataset record in place, by walking the four strand
    prefixes. The bucket is the index: no register to keep in step.
    Sorted by prefix so runs are repeatable."""
    paginator = client.get_paginator("list_objects_v2")
    found = []
    for strand in keys.STRANDS:
        for page in paginator.paginate(Bucket=bucket, Prefix=strand + "/"):
            for obj in page.get("Contents", []):
                m = _RECORD_KEY_RE.match(obj["Key"])
                if m:
                    found.append(DatasetRef(m.group("prefix"), obj["Key"]))
    for ref in sorted(found, key=lambda r: r.prefix):
        yield ref


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
