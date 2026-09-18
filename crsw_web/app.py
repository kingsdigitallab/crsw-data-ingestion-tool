"""FastAPI application factory.

Phase 1 skeleton: health, identity, and a connectivity check that
lists the staging prefix with the service key. Deposit endpoints
arrive in Phase 2. Run with:

    uvicorn --factory crsw_web.app:create_app
"""
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException

import crsw_deposit
from . import s3
from .auth import User, make_authenticator
from .config import Settings

CONNECTIVITY_SAMPLE = 20


def create_app(settings: Optional[Settings] = None,
               s3_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    current_user = make_authenticator(settings)
    client = s3_client or s3.make_client(settings)

    app = FastAPI(title="CRSW web deposit", version=crsw_deposit.__version__,
                  docs_url=None, redoc_url=None)
    app.state.settings = settings

    @app.get("/health")
    def health():
        return {"status": "ok",
                "version": crsw_deposit.__version__,
                "auth_mode": settings.auth_mode}

    @app.get("/whoami")
    def whoami(user: User = Depends(current_user)):
        return {"username": user.username}

    @app.get("/connectivity")
    def connectivity(user: User = Depends(current_user)):
        """Prove the service key can list the staging prefix. An empty
        prefix is a pass; a rejected key or unreachable endpoint is
        reported in words, not S3 error codes."""
        try:
            keys = s3.list_prefix(client, settings.s3_bucket,
                                  settings.staging_prefix,
                                  limit=CONNECTIVITY_SAMPLE)
        except Exception as exc:  # translated below; never a bare 500
            raise HTTPException(
                status_code=502,
                detail=s3.translate_error(exc, settings))
        return {"endpoint": settings.s3_endpoint,
                "bucket": settings.s3_bucket,
                "prefix": settings.staging_prefix + "/",
                "sample": keys,
                "sample_limit": CONNECTIVITY_SAMPLE,
                "checked_by": user.username}

    return app
