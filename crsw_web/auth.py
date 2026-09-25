"""Identity: the one seam between the service and KCL sign-in.

Sign-in itself is not this service's job. The KCL reverse proxy in
front of the VM requires authentication, restricts to allowed groups,
can restrict to the KCL network, and terminates TLS. What reaches us
is a plain HTTP request carrying the signed-in user in a header. This
module turns that header into a User, and refuses to believe it from
anywhere but the proxy. Request handlers depend on `User` and nothing
else; the same seam is where a later egress feature adds per-prefix
authorisation.

Modes:
  placeholder  local development; a fixed username from config
  proxy        production; identity from the proxy's header, source
               checked against CRSW_TRUSTED_PROXY_CIDRS"""
import ipaddress
import re
from dataclasses import dataclass
from typing import Callable, List, Mapping, Optional, Tuple

from fastapi import HTTPException, Request

from .config import Settings


@dataclass(frozen=True)
class User:
    username: str   # the KCL username, e.g. k1078591 - what the record's
                    # depositor field holds, so CLI and web deposits agree
    groups: Tuple[str, ...] = ()   # as the proxy reports them; the read
                                   # role's amber rule reads these


class NotAuthenticated(Exception):
    pass


class NotAuthorised(Exception):
    pass


def _split_groups(raw: str) -> List[str]:
    return [g.strip() for g in re.split(r"[,; ]+", raw or "") if g.strip()]


def user_from_headers(headers: Mapping[str, str], settings: Settings) -> User:
    """Map the proxy's headers to a User, or raise. Pure; no request."""
    raw = (headers.get(settings.proxy_user_header) or "").strip()
    if not raw:
        raise NotAuthenticated(
            "no identity received from the proxy (header %r absent or empty)"
            % settings.proxy_user_header)
    username = raw
    if settings.proxy_username_pattern:
        m = re.match(settings.proxy_username_pattern, raw)
        if not m or not m.group(1):
            raise NotAuthorised("identity %r does not match the expected form" % raw)
        username = m.group(1)
    username = username.strip().lower()
    groups: List[str] = []
    if settings.proxy_groups_header:
        groups = _split_groups(headers.get(settings.proxy_groups_header) or "")
        if settings.proxy_required_group and settings.proxy_required_group not in groups:
            raise NotAuthorised(
                "%s is not a member of %s" % (username, settings.proxy_required_group))
    return User(username=username, groups=tuple(groups))


PEER_HEADER = "x-sidecar-peer"


def peer_address(request: Request) -> Optional[str]:
    """The address that connected to the nginx sidecar. nginx always sets
    X-Sidecar-Peer from its own $remote_addr (a client cannot supply it,
    and the app port is reachable only from the sidecar). request.client
    is NOT that: uvicorn --proxy-headers rewrites it from X-Forwarded-For,
    whose leftmost entry is the browser behind the KCL proxy."""
    return request.headers.get(PEER_HEADER) or (
        request.client.host if request.client else None)


def peer_is_trusted(host: Optional[str], cidrs) -> bool:
    """True if `host` (the immediate client as the sidecar saw it) is
    inside one of the trusted proxy ranges. No ranges configured means
    no check (local development)."""
    if not cidrs:
        return True
    try:
        addr = ipaddress.ip_address(host or "")
    except ValueError:
        return False
    return any(addr in net for net in cidrs)


def make_authenticator(settings: Settings) -> Callable[[Request], User]:
    """A FastAPI dependency resolving the current user for this
    deployment's auth mode. Constructed once at app creation so an
    unsupported mode fails at startup, not on the first request."""
    if settings.auth_mode == "placeholder":
        user = User(username=settings.dev_user, groups=tuple(settings.dev_groups))

        def placeholder(request: Request) -> User:
            return user
        return placeholder

    if settings.auth_mode == "proxy":
        cidrs = settings.trusted_proxy_networks()

        def proxy(request: Request) -> User:
            if not peer_is_trusted(peer_address(request), cidrs):
                raise HTTPException(
                    status_code=403,
                    detail="requests are only accepted from the KCL proxy")
            try:
                return user_from_headers(request.headers, settings)
            except NotAuthenticated as e:
                raise HTTPException(status_code=401, detail=str(e))
            except NotAuthorised as e:
                raise HTTPException(status_code=403, detail=str(e))
        return proxy

    raise ValueError("unknown auth mode %r" % settings.auth_mode)
