"""One JSON line per action, to a file and to stdout. The log is the
record of every promotion; the control object is deleted with the rest
of the staged deposit once it has moved."""
import json
import sys
from typing import Optional

from crsw_deposit import record


class Log:
    STDOUT_PATHS = ("/dev/stdout", "-")

    def __init__(self, path: Optional[str], echo=True):
        # A stdout path (the container's setting) means stdout only,
        # otherwise every line would be printed twice.
        if path in self.STDOUT_PATHS:
            path, echo = None, True
        self.path = path
        self.echo = echo
        self.lines = []      # kept in memory too, for tests and the summary

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
