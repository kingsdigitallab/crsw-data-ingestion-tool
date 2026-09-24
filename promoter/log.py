"""One JSON line per action, to a file and to stdout. The log is the
record of every promotion; the control object is deleted with the rest
of the staged deposit once it has moved."""
import json
import secrets
from datetime import datetime, timezone
import sys
from typing import Optional

from crsw_deposit import record


def new_run_id() -> str:
    """`20260924T163000123456Z-ab12`: UTC to the microsecond so runs sort
    in time order, plus a few random hex digits."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return "%s-%s" % (stamp, secrets.token_hex(2))


class Log:
    STDOUT_PATHS = ("/dev/stdout", "-")

    def __init__(self, path: Optional[str], echo=True, run_id: Optional[str] = None):
        # A stdout path (the container's setting) means stdout only,
        # otherwise every line would be printed twice.
        if path in self.STDOUT_PATHS:
            path, echo = None, True
        self.path = path
        self.echo = echo
        self.lines = []      # kept in memory too, for tests and the audit object
        # One id per run: the audit object's name, and on the start line.
        self.run_id = run_id or new_run_id()

    def write(self, action: str, deposit=None, **detail) -> dict:
        entry = {"when": record.utc_now_iso(), "action": action}
        if deposit is not None:
            entry.update({"deposit": deposit.id, "user": deposit.user,
                          "prefix": deposit.prefix})
        entry.update(detail)
        line = json.dumps(entry, ensure_ascii=False)
        self.lines.append(entry)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        if self.echo:
            print(line, file=sys.stdout)
        return entry
