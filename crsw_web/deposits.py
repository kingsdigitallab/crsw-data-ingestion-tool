"""Deposit state between the three API calls.

The service has no database and must survive restarts and multiple
workers, so state is a small control object in the staging area:

    staging/<user>/<deposit-id>/_deposit.json

beside the final-prefix subtree the files land in. The promoter strips
staging/<user>/<deposit-id>/ and skips _deposit.json. This is service
bookkeeping, not a storage convention, which is why it lives here and
not in crsw_deposit."""
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from botocore.exceptions import ClientError

CONTROL_NAME = "_deposit.json"
STATUS_OPEN = "open"
STATUS_COMPLETE = "complete"


@dataclass
class Deposit:
    id: str
    user: str
    created: str
    status: str
    meta: Dict
    prefix: str                 # the final dataset prefix, e.g. rs2/csac/green/0_raw/x
    dataset_uuid: str
    entries: List[Dict] = field(default_factory=list)   # manifest entries, as stored
    record_key: Optional[str] = None                     # set at finalise (staged key)
    finalised: Optional[str] = None

    def entry_for(self, member: str) -> Optional[Dict]:
        return next((e for e in self.entries if e["path"] == member), None)

    def put_entry(self, entry: Dict) -> None:
        """Replace any entry at the same path; keep manifest order by path."""
        self.entries = sorted(
            [e for e in self.entries if e["path"] != entry["path"]] + [entry],
            key=lambda e: e["path"])

    def drop_entry(self, member: str) -> bool:
        before = len(self.entries)
        self.entries = [e for e in self.entries if e["path"] != member]
        return len(self.entries) != before


class DepositStore:
    def __init__(self, client, bucket: str, staging_prefix: str):
        self.client = client
        self.bucket = bucket
        self.staging_prefix = staging_prefix.strip("/")

    # --- key layout ----------------------------------------------------
    def root(self, user: str, deposit_id: str) -> str:
        return "%s/%s/%s" % (self.staging_prefix, user, deposit_id)

    def control_key(self, user: str, deposit_id: str) -> str:
        return self.root(user, deposit_id) + "/" + CONTROL_NAME

    def staged_key(self, user: str, deposit_id: str, final_key: str) -> str:
        """Where a final object key lives while in staging: the final
        key under the deposit root, so promotion is a prefix strip."""
        return self.root(user, deposit_id) + "/" + final_key

    # --- persistence -----------------------------------------------------
    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex[:12]

    def create(self, user: str, meta: Dict, prefix: str, dataset_uuid: str,
               now: str) -> Deposit:
        dep = Deposit(id=self.new_id(), user=user, created=now,
                      status=STATUS_OPEN, meta=meta, prefix=prefix,
                      dataset_uuid=dataset_uuid)
        self.save(dep)
        return dep

    def save(self, dep: Deposit) -> None:
        body = json.dumps(asdict(dep), indent=2, ensure_ascii=False).encode("utf-8")
        self.client.put_object(Bucket=self.bucket,
                               Key=self.control_key(dep.user, dep.id),
                               Body=body, ContentType="application/json")

    def load(self, user: str, deposit_id: str) -> Optional[Deposit]:
        """The deposit, or None if it does not exist FOR THIS USER - the
        user is part of the key, so another user's deposit is simply
        absent rather than forbidden."""
        try:
            obj = self.client.get_object(Bucket=self.bucket,
                                         Key=self.control_key(user, deposit_id))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("NoSuchKey", "404"):
                return None
            raise
        data = json.loads(obj["Body"].read().decode("utf-8"))
        return Deposit(**data)

    # --- object helpers used by the routes --------------------------------
    def stored_size(self, key: str) -> Optional[int]:
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey",
                                                           "NotFound"):
                return None
            raise
        return head["ContentLength"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)
