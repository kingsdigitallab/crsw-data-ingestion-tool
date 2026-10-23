"""Who may download what. The one rule, pure, so the dataset page and
the download route cannot disagree.

Green data: any signed-in Centre member. Amber: by CRSW_AMBER_ACCESS,
a Centre policy decision recorded in finding-and-reuse.md §1: `off`
(listed and described, files not served; the page names the steward),
`groups` (members of the strand's group as the proxy reports it) or
`all` (any signed-in member; the laptop and the KCL-only phase). A
person may always download what they deposited (r12). Red never reaches
the store, so it is never served."""
from typing import Iterable, Optional

from .auth import User
from .config import Settings


def may_download(user: User, sensitivity: Optional[str], strand: Optional[str],
                 settings: Settings, depositors: Iterable[str] = ()) -> Optional[str]:
    """None when allowed, else the reason in plain words. `depositors`
    is the record's (or index row's) list: a person may download what
    they deposited, whatever CRSW_AMBER_ACCESS says (r12)."""
    if sensitivity == "green":
        return None
    if sensitivity == "amber":
        if user.username and user.username in (depositors or ()):
            return None
        mode = settings.amber_access
        if mode == "all":
            return None
        if mode == "groups":
            wanted = settings.amber_group_template.format(strand=strand or "")
            if wanted in user.groups:
                return None
            return ("amber data in %s is available to members of %s; ask the "
                    "dataset's steward for access" % (strand, wanted))
        return ("amber data is not downloadable through this service yet; "
                "ask the dataset's steward")
    return "%s data is not served" % (sensitivity or "unclassified")
