"""FastAPI application factory.

Routes are thin: every rule about keys, records, labels and noise is a
call into crsw_deposit. Run with:

    uvicorn --factory crsw_web.app:create_app
"""
import hashlib
import logging
from pathlib import Path
from typing import Dict, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import crsw_deposit
from crsw_deposit import authority, deposit_logic, keys, labels as labels_mod, noise, record, vocab
from . import s3
from .access import may_download
from .auth import User, make_authenticator, peer_address
from .catalogue import Catalogue
from .config import Settings
from .deposits import STATUS_COMPLETE, STATUS_OPEN, Deposit, DepositStore
from .vocabulary import VocabularyCache
from .metadata import SOURCE_TYPES, validate_meta
from . import quota
from .upload import TooLarge, stream_to_s3

CONNECTIVITY_SAMPLE = 20
log = logging.getLogger("crsw_web")
HERE = Path(__file__).resolve().parent
SOURCE_TYPE_HELP = {
    "archive": "existing collection or repository",
    "survey": "primary data collection instrument",
    "scrape": "automated extraction from an online source",
    "instrument": "sensor, satellite, or other device output",
    "partner": "supplied by a partner organisation",
    "derived": "produced from other data already held",
    "other": "none of the above",
}


def client_rules() -> Dict:
    """The rules the browser pre-checks with. Read from crsw_deposit so
    the page can never disagree with the server; the server still
    enforces every one of them."""
    return {
        "noise_file_names": sorted(noise.NOISE_FILE_NAMES),
        "noise_file_prefixes": list(noise.NOISE_FILE_PREFIXES),
        "noise_dir_names": sorted(noise.NOISE_DIR_NAMES),
        "reserved_record_pattern": keys.RESERVED_RECORD_RE.pattern,
        "problem_chars": keys.PROBLEM_CHARS,
    }


def create_app(settings: Optional[Settings] = None,
               s3_client=None, vocab_dict: Optional[Dict] = None,
               read_client=None) -> FastAPI:
    settings = settings or Settings.from_env()
    current_user = make_authenticator(settings)
    client = s3_client or s3.make_client(settings)
    # The read role: a second, read-only client over the read key and the
    # promoter's index. Off (every /datasets route 404) unless configured.
    if read_client is None and settings.read_enabled:
        read_client = s3.make_read_client(settings)
    catalogue = (Catalogue(read_client, settings.s3_bucket, settings.index_prefix,
                           settings.index_refresh_seconds)
                 if read_client is not None else None)
    if vocab_dict is None:
        # fetch -> cache -> bundled. The cache write is best-effort, so a
        # read-only container root just means the bundled copy is used.
        # While running, the cache re-fetches every
        # CRSW_VOCAB_REFRESH_SECONDS so a merged vocabulary change reaches
        # the form without a restart.
        loaded, source = vocab.load_vocabulary()
        vocab_cache = VocabularyCache(loaded, source,
                                      refresh_seconds=settings.vocab_refresh_seconds)
    else:
        # Injected (tests): held as given, never refreshed.
        vocab_cache = VocabularyCache(vocab_dict, "injected", refresh_seconds=0)
    store = DepositStore(client, settings.s3_bucket, settings.staging_prefix)

    app = FastAPI(title="CRSW web deposit", version=crsw_deposit.__version__,
                  docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.vocab_cache = vocab_cache
    app.state.catalogue = catalogue
    read_enabled = catalogue is not None
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))

    # --- Phase 3: the browser form ----------------------------------------
    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: User = Depends(current_user)):
        vocab_dict = vocab_cache.current()
        facets = vocab.facets(vocab_dict)
        return templates.TemplateResponse(request, "index.html", {
            "user": user,
            "read_enabled": read_enabled,
            "strands": keys.STRANDS,
            "domains": vocab.domains(vocab_dict),
            "facets": facets,
            # r9 §1.8: narrower terms sit indented under their broader term
            "facet_tree": {name: authority.tree(vocab_dict, name)
                           if authority.is_authority(vocab_dict)
                           else [(t, 0) for t in terms]
                           for name, terms in facets.items()},
            "vocabulary_version": vocab_dict.get("vocabulary_version"),
            "source_types": [(c, SOURCE_TYPE_HELP.get(c, "")) for c in SOURCE_TYPES],
            "activities": vocab.activities(vocab_dict),
            "rules": client_rules(),
        })

    @app.get("/deposits/{deposit_id}/summary", response_class=HTMLResponse)
    def summary(deposit_id: str, request: Request,
                user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        record_text = None
        if dep.record_key:
            try:
                obj = client.get_object(Bucket=settings.s3_bucket, Key=dep.record_key)
                record_text = obj["Body"].read().decode("utf-8")
            except Exception:
                record_text = None
        return templates.TemplateResponse(request, "summary.html", {
            "user": user, "dep": dep, "record_text": record_text,
            "staging_root": store.root(dep.user, dep.id),
            "read_enabled": read_enabled,
        })

    # --- the read role: find, browse (finding-and-reuse.md §§1-3) ------------
    def need_read() -> Catalogue:
        if catalogue is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return catalogue

    def public_row(r: Dict) -> Dict:
        abstract = r.get("abstract") or ""
        return {k: r.get(k) for k in ("identifier", "dataset_uuid", "dataset", "project",
                                      "strand", "state", "sensitivity", "domain",
                                      "depositor", "files", "bytes", "modified",
                                      "subject", "version")} | {
            "abstract": abstract[:200] + ("…" if len(abstract) > 200 else "")}

    def search_args(q, strand, state, sensitivity, subject, project):
        return dict(q=q, strand=strand or None, state=state or None,
                    sensitivity=sensitivity or None, subject=subject or None,
                    project=project or None)

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets_page(request: Request, q: str = "", strand: str = "", state: str = "",
                      sensitivity: str = "", subject: str = "", project: str = "",
                      user: User = Depends(current_user)):
        cat = need_read()
        results = cat.search(**search_args(q, strand, state, sensitivity, subject, project))
        return templates.TemplateResponse(request, "datasets.html", {
            "user": user, "q": q, "strand": strand, "state": state,
            "sensitivity": sensitivity, "subject": subject,
            "strands": keys.STRANDS, "states": keys.STATES,
            "sensitivities": keys.SENSITIVITIES, "subjects": cat.subjects(),
            "results": results,
            "sizes": {r["identifier"]: record.human_bytes(r.get("bytes") or 0) for r in results},
            "built_at": cat.built_at(), "error": cat.error,
        })

    @app.get("/datasets.json")
    def datasets_json(q: str = "", strand: str = "", state: str = "", sensitivity: str = "",
                      subject: str = "", project: str = "", limit: int = 50,
                      user: User = Depends(current_user)):
        cat = need_read()
        rows = cat.search(limit=max(1, min(limit, 500)),
                          **search_args(q, strand, state, sensitivity, subject, project))
        return {"datasets": [public_row(r) for r in rows], "built_at": cat.built_at()}

    def load_record_or_404(cat: Catalogue, identifier: str) -> Dict:
        if cat.get(identifier) is None and not keys.dataset_prefix_ok(identifier):
            raise HTTPException(status_code=404, detail="no such dataset")
        try:
            rec = cat.record(identifier)
        except Exception as exc:
            raise storage_error(exc)
        if rec is None:
            raise HTTPException(status_code=404, detail="no such dataset")
        return rec

    @app.get("/datasets/{identifier:path}/record")
    def dataset_record(identifier: str, user: User = Depends(current_user)):
        cat = need_read()
        load_record_or_404(cat, identifier)
        return Response(content=cat.record_text(identifier), media_type="application/json")

    @app.get("/datasets/{identifier:path}", response_class=HTMLResponse)
    def dataset_page(identifier: str, request: Request, user: User = Depends(current_user)):
        cat = need_read()
        rec = load_record_or_404(cat, identifier)
        files = rec.get("files") or []
        derived = [r for r in cat.rows() if identifier in (r.get("derived_from_identifiers") or [])]
        return templates.TemplateResponse(request, "dataset.html", {
            "user": user, "identifier": identifier, "rec": rec, "derived": derived,
            "refusal": may_download(user, rec.get("sensitivity"), rec.get("strand"), settings),
            "sizes": {f["path"]: record.human_bytes(f.get("bytes") or 0) for f in files},
            "total": record.human_bytes(sum(int(f.get("bytes") or 0) for f in files)),
        })

    def storage_error(exc: Exception) -> HTTPException:
        return HTTPException(status_code=502,
                             detail=s3.translate_error(exc, settings))

    def load_or_404(user: User, deposit_id: str) -> Deposit:
        try:
            dep = store.load(user.username, deposit_id)
        except Exception as exc:
            raise storage_error(exc)
        if dep is None:
            raise HTTPException(status_code=404, detail="no such deposit")
        return dep

    def must_be_open(dep: Deposit) -> None:
        if dep.status != STATUS_OPEN:
            raise HTTPException(status_code=409,
                                detail="deposit %s is already %s"
                                % (dep.id, dep.status))

    def labels_for(dep: Deposit, checksum: str) -> Dict[str, str]:
        return labels_mod.object_labels(dep.dataset_uuid, checksum,
                                        dep.meta["sensitivity"], dep.user)

    # --- Phase 1 ----------------------------------------------------------
    @app.get("/health")
    def health():
        return {"status": "ok", "version": crsw_deposit.__version__,
                "auth_mode": settings.auth_mode}

    @app.get("/whoami")
    def whoami(user: User = Depends(current_user)):
        return {"username": user.username, "auth_mode": settings.auth_mode}

    @app.get("/auth/headers")
    def auth_headers(request: Request):
        """First-deploy diagnostic: what does the KCL proxy send us? Only
        exists while CRSW_DEBUG_HEADERS=1; read it once, set
        CRSW_PROXY_USER_HEADER, then unset the flag."""
        if not settings.debug_headers:
            raise HTTPException(status_code=404, detail="Not Found")
        hidden = {"cookie", "authorization", "proxy-authorization"}
        return {"peer": peer_address(request),
                "headers": {k: v for k, v in request.headers.items()
                            if k.lower() not in hidden}}

    @app.get("/connectivity")
    def connectivity(user: User = Depends(current_user)):
        try:
            sample = s3.list_prefix(client, settings.s3_bucket,
                                    settings.staging_prefix,
                                    limit=CONNECTIVITY_SAMPLE)
        except Exception as exc:
            raise storage_error(exc)
        return {"endpoint": settings.s3_endpoint, "bucket": settings.s3_bucket,
                "prefix": settings.staging_prefix + "/", "sample": sample,
                "sample_limit": CONNECTIVITY_SAMPLE, "checked_by": user.username}

    @app.get("/vocabulary")
    def vocabulary():
        vocab_dict = vocab_cache.current()
        return {"vocabulary_version": vocab_dict.get("vocabulary_version"),
                "vocabulary_source": vocab_cache.source,
                "vocabulary_loaded": vocab_cache.loaded,
                "facets": vocab.facets(vocab_dict),
                "domains": vocab.domains(vocab_dict),
                "strands": list(keys.STRANDS), "states": list(keys.STATES),
                "sensitivities": list(keys.SENSITIVITIES)}

    # --- Phase 2: the deposit API -----------------------------------------
    @app.post("/deposits", status_code=201)
    def create_deposit(form: Dict, user: User = Depends(current_user)):
        meta, errors, warnings = validate_meta(form, vocab_cache.current())
        if errors:
            raise HTTPException(status_code=422,
                                detail={"errors": errors, "warnings": warnings})
        prefix = deposit_logic.dataset_prefix(meta)
        try:
            use = quota.usage(store, user.username)
            if use.open_deposits >= settings.user_max_open_deposits:
                raise HTTPException(
                    status_code=429,
                    detail="you have %d deposits still open; finalise or delete "
                           "one before starting another (limit %d)"
                           % (use.open_deposits, settings.user_max_open_deposits))
            dep = store.create(user.username, meta, prefix,
                               deposit_logic.dataset_uuid_for(None),
                               record.utc_now_iso())
        except HTTPException:
            raise
        except Exception as exc:
            raise storage_error(exc)
        log.info("deposit created user=%s deposit=%s prefix=%s", user.username, dep.id, prefix)
        return {"id": dep.id, "prefix": prefix,
                "staging_prefix": store.root(dep.user, dep.id) + "/" + prefix,
                "dataset_uuid": dep.dataset_uuid, "warnings": warnings}

    @app.get("/deposits/{deposit_id}")
    def get_deposit(deposit_id: str, user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        return dep

    @app.put("/deposits/{deposit_id}/files/{member:path}")
    async def put_file(deposit_id: str, member: str, request: Request,
                       include_noise: bool = False,
                       user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        must_be_open(dep)

        member = keys.normalise_member_path(member.split("/"))
        problem = keys.member_path_error(member)
        if problem:
            raise HTTPException(status_code=422, detail=problem)
        if keys.is_reserved_member(member):
            raise HTTPException(
                status_code=422,
                detail="%r matches dataset.*.json, which is reserved for "
                       "dataset records" % member)
        if noise.is_noise_member(member) and not include_noise:
            raise HTTPException(
                status_code=422,
                detail="%r looks like OS/editor noise and is excluded by "
                       "default; re-send with ?include_noise=1 to deposit "
                       "it anyway" % member)
        try:
            (_, final_key), = deposit_logic.plan_keys(dep.meta, [member])
        except (ValueError, keys.RedDataError) as e:
            raise HTTPException(status_code=422, detail=str(e))

        declared = request.headers.get("content-length")
        declared_n = int(declared) if declared and declared.isdigit() else None
        if declared_n is not None and declared_n > settings.max_body_bytes:
            raise HTTPException(status_code=413,
                                detail="body exceeds the %d-byte limit"
                                % settings.max_body_bytes)
        if dep.entry_for(member) is None and len(dep.entries) >= settings.max_members_per_deposit:
            raise HTTPException(status_code=413,
                                detail="a deposit may hold at most %d files"
                                % settings.max_members_per_deposit)

        # Per-user staging quota: what is there now plus this body.
        limit = settings.max_body_bytes
        if settings.user_quota_bytes is not None:
            try:
                use = quota.usage(store, dep.user)
            except Exception as exc:
                raise storage_error(exc)
            remaining = quota.remaining_bytes(settings, use)
            if declared_n is not None and declared_n > remaining:
                raise HTTPException(status_code=413,
                                    detail=quota.quota_message(settings, use, declared_n))
            limit = min(limit, remaining)

        staged = store.staged_key(dep.user, dep.id, final_key)
        try:
            size, checksum = await stream_to_s3(
                client, settings.s3_bucket, staged, request.stream(),
                lambda c: labels_for(dep, c), max_bytes=limit)
        except TooLarge as e:
            raise HTTPException(status_code=413, detail=str(e))
        except Exception as exc:
            raise storage_error(exc)

        # Size, never checksum-vs-ETag (r2 §0).
        stored = store.stored_size(staged)
        if stored != size:
            store.delete(staged)
            raise HTTPException(
                status_code=502,
                detail="the upload appeared to finish but the stored object "
                       "is missing or the wrong size (expected %d, stored %s). "
                       "Re-send the file." % (size, stored))

        entry = record.manifest_entry(member, checksum, size,
                                      fmt=record.guess_format(member))
        dep.put_entry(entry)
        try:
            store.save(dep)
        except Exception as exc:
            raise storage_error(exc)
        log.info("file stored user=%s deposit=%s member=%s bytes=%d",
                 dep.user, dep.id, member, size)
        return {"member": member, "key": final_key, "staged_key": staged,
                "bytes": size, "checksum_sha256": checksum,
                "format": entry.get("format")}

    @app.delete("/deposits/{deposit_id}/files/{member:path}", status_code=204)
    def delete_file(deposit_id: str, member: str,
                    user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        must_be_open(dep)
        member = keys.normalise_member_path(member.split("/"))
        if not dep.drop_entry(member):
            raise HTTPException(status_code=404, detail="no such member")
        (_, final_key), = deposit_logic.plan_keys(dep.meta, [member])
        try:
            store.delete(store.staged_key(dep.user, dep.id, final_key))
            store.save(dep)
        except Exception as exc:
            raise storage_error(exc)
        log.info("file removed user=%s deposit=%s member=%s", dep.user, dep.id, member)
        return Response(status_code=204)

    @app.post("/deposits/{deposit_id}/finalise")
    def finalise(deposit_id: str, user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        must_be_open(dep)
        if not dep.entries:
            raise HTTPException(status_code=409, detail="no files deposited yet")

        staged_prefix = store.staged_key(dep.user, dep.id, dep.prefix)
        try:
            problems = deposit_logic.completion_problems(
                staged_prefix, dep.entries, store.stored_size)
        except Exception as exc:
            raise storage_error(exc)
        if problems:
            raise HTTPException(status_code=409, detail={"problems": problems})

        rec, union, _added, _updated = deposit_logic.assemble_record(
            dep.meta, None, dep.entries, dep.user, record.utc_now_iso(),
            dep.dataset_uuid)
        errors, warnings = record.validate_record(
            rec, vocab_cache.terms, vocab_cache.domain_codes,
            vocab.activity_codes(vocab_cache.current()))
        if errors:
            raise HTTPException(status_code=422,
                                detail={"errors": errors, "warnings": warnings})

        data = deposit_logic.record_bytes(rec)
        checksum = hashlib.sha256(data).hexdigest()
        record_key = staged_prefix + "/" + keys.record_filename(dep.meta["dataset"])
        try:
            client.put_object(Bucket=settings.s3_bucket, Key=record_key,
                              Body=data, ContentType="application/json",
                              Metadata=labels_for(dep, checksum))
            back = client.get_object(Bucket=settings.s3_bucket, Key=record_key)
            record.parse_record(back["Body"].read().decode("utf-8"))
        except record.RecordParseError as e:
            raise HTTPException(status_code=502,
                                detail="record round-trip failed: %s" % e)
        except Exception as exc:
            raise storage_error(exc)

        dep.status = STATUS_COMPLETE
        dep.record_key = record_key
        dep.finalised = rec["modified"]
        try:
            store.save(dep)
        except Exception as exc:
            raise storage_error(exc)
        log.info("deposit finalised user=%s deposit=%s files=%d record=%s",
                 dep.user, dep.id, len(union), record_key)
        return {"id": dep.id, "record_key": record_key,
                "dataset_uuid": dep.dataset_uuid,
                "files": len(union), "warnings": warnings,
                "staging_prefix": staged_prefix}

    return app
