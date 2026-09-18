"""Identity: the one seam between the service and KCL SSO.

Request handlers depend on `User` and nothing else. Swapping the
placeholder for OIDC (KCL SSO, group er_prj_kdl_slavery) changes this
module and the config, not the handlers. The same seam is where a
later egress feature adds per-prefix authorisation."""
from dataclasses import dataclass
from typing import Callable

from fastapi import Request

from .config import Settings


@dataclass(frozen=True)
class User:
    username: str   # the KCL username, e.g. k1078591 - what the record's
                    # depositor field holds, so CLI and web deposits agree


def make_authenticator(settings: Settings) -> Callable[[Request], User]:
    """A FastAPI dependency resolving the current user for this
    deployment's auth mode. Constructed once at app creation so an
    unsupported mode fails at startup, not on the first request."""
    if settings.auth_mode == "placeholder":
        user = User(username=settings.dev_user)

        def placeholder(request: Request) -> User:
            return user
        return placeholder

    if settings.auth_mode == "oidc":
        raise NotImplementedError(
            "CRSW_AUTH_MODE=oidc needs the SSO registration from eResearch "
            "(plan Phase 5). Use CRSW_AUTH_MODE=placeholder for now.")

    raise ValueError("unknown auth mode %r" % settings.auth_mode)
