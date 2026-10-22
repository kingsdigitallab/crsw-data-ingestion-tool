"""FastAPI application factory.

Routes are thin: every rule about keys, records, labels and noise is a
call into crsw_deposit. Run with:

    uvicorn --factory crsw_web.app:create_app
"""
import hashlib
import json
import logging
import re
from datetime import timezone
from email.utils import format_datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode

from botocore.exceptions import ClientError
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from markupsafe import Markup, escape
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

import crsw_deposit
from crsw_deposit import (authority, deposit_logic, keys, labels as labels_mod, noise,
                          places as places_mod, record, vocab)
from . import s3
from .access import may_download
from .auth import User, make_authenticator, peer_address
from .catalogue import PER_CHOICES, PER_DEFAULT, Catalogue, group_projects, page_of
from .llm import Platform, PlatformError
from .passages import (open_index, split_sentences, mark_closest, word_pattern,
                       question_stems)
from .config import Settings
from .deposits import STATUS_COMPLETE, STATUS_OPEN, Deposit, DepositStore
from .vocabulary import VocabularyCache
from .metadata import SOURCE_TYPES, validate_meta
from .version import release
from . import quota
from .upload import TooLarge, stream_to_s3

CONNECTIVITY_SAMPLE = 20
DOWNLOAD_CHUNK = 8 * 1024 * 1024   # one chunk in memory at a time, never the file
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
               read_client=None, platform=None, passage_index=None,
               places_dict: Optional[Dict] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    current_user = make_authenticator(settings)
    client = s3_client or s3.make_client(settings)
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
    # r11 §4: the places file, through the same cache class over its own
    # chain (a places document is not an authority file, so the cache's
    # vocabulary-shaped reads come out empty and are never asked for).
    if places_dict is not None:
        places_cache = VocabularyCache(places_dict, "injected", refresh_seconds=0)
    elif vocab_dict is not None:
        # An injected vocabulary (tests) gets the bundled places, no fetch.
        bundled = json.loads(places_mod.bundled_places_path().read_text(encoding="utf-8"))
        places_cache = VocabularyCache(bundled, "bundled", refresh_seconds=0)
    else:
        loaded_places, places_source = places_mod.load_places()
        places_cache = VocabularyCache(loaded_places, places_source,
                                       refresh_seconds=settings.vocab_refresh_seconds,
                                       loader=places_mod.load_places)
    # The read role: a second, read-only client over the read key and the
    # promoter's index. Off (every /datasets route 404) unless configured.
    # The catalogue asks the vocabulary and places caches at query time,
    # so a subject or place filter follows the hierarchy of the file in
    # hand.
    if read_client is None and settings.read_enabled:
        read_client = s3.make_read_client(settings)
    catalogue = (Catalogue(read_client, settings.s3_bucket, settings.index_prefix,
                           settings.index_refresh_seconds, vocabulary=vocab_cache.current,
                           places=places_cache.current)
                 if read_client is not None else None)
    # Search by meaning: the KCL LLM platform, only with the read role.
    if platform is None and catalogue is not None and settings.llm_enabled:
        platform = Platform.from_settings(settings)
    if catalogue is None:
        platform = None
    # The search page needs the embedding model; a blank name is off.
    search_enabled = platform is not None and bool(platform.embed_model)
    # The VM's copy of the passages, the page's second group.
    if passage_index is None and search_enabled and settings.passages_enabled:
        passage_index = open_index(read_client, settings.s3_bucket, settings.index_prefix,
                                   settings.passages_path, settings.index_refresh_seconds,
                                   settings.llm_embed_dims or None)
    if not search_enabled:
        passage_index = None
    store = DepositStore(client, settings.s3_bucket, settings.staging_prefix)

    app = FastAPI(title="CRSW web deposit", version=crsw_deposit.__version__,
                  docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.vocab_cache = vocab_cache
    app.state.places_cache = places_cache
    app.state.catalogue = catalogue
    app.state.platform = platform
    app.state.passage_index = passage_index
    read_enabled = catalogue is not None
    passages_enabled = passage_index is not None
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["search_enabled"] = search_enabled
    templates.env.globals["passages_enabled"] = passages_enabled
    # The release the image was built from (deploy/Dockerfile's
    # CRSW_VERSION), in every masthead and /health, so a deploy shows.
    release_tag = release()
    templates.env.globals["release"] = release_tag

    def emphasise(text: str, question: str) -> Markup:
        """The text escaped, with the question's words in <b>."""
        pat = word_pattern(question)
        safe = escape(text or "")
        if pat is None:
            return safe
        return Markup(pat.sub(lambda m: "<b>%s</b>" % escape(m.group(0)), str(safe)))
    templates.env.filters["emphasise"] = emphasise

    # --- Phase 3: the browser form ----------------------------------------
    views_held: Dict[str, object] = {}

    def vocab_views(vocab_dict: dict) -> Dict:
        """What the templates derive from the vocabulary (facets, domains,
        activities, the form's facet trees, labels, scope notes), built
        once per document: the cache swaps the document whole on a
        refresh, so identity says whether anything changed."""
        held = views_held.get("held")
        if held is None or held[0] is not vocab_dict:
            facets = vocab.facets(vocab_dict)
            views = {
                "facets": facets,
                "domains": vocab.domains(vocab_dict),
                "activities": vocab.activities(vocab_dict),
                # r9 §1.8: narrower terms sit indented under their broader
                # term; a scope note, where the term has one, is hover text
                # and an expand control (no JS). The checkbox text stays the
                # slug: the script and the picker prefill match on it.
                "facet_tree": {name: form_terms(vocab_dict, name, terms)
                               for name, terms in facets.items()},
                "labels": authority.labels_of(vocab_dict),
                "scopes": authority.scopes_of(vocab_dict),
                # r11 §3: the providers list, for the form's select and the
                # dataset page's links.
                "providers": vocab.providers(vocab_dict),
            }
            held = (vocab_dict, views)
            views_held["held"] = held
        return held[1]

    # r11 §4: the places, as the two selects and the dataset page want
    # them, built once per places document (the cache swaps it whole).
    places_held: Dict[str, object] = {}

    def group_places(options: Sequence[Dict]) -> List[Tuple[str, List[Dict]]]:
        """[(group, [place, ...])] for a place select: the world on its
        own, then one group per region holding the region and everything
        inside it, indented by figure spaces; codes the file does not
        know under "other"."""
        groups: List[Tuple[str, List[Dict]]] = []
        for p in options:
            top = p["depth"] <= 1 and p.get("kind")
            name = "other" if not p.get("kind") else (
                p["label"] if top else (groups[-1][0] if groups else "other"))
            if not groups or top or groups[-1][0] != name:
                groups.append((name, []))
            groups[-1][1].append(dict(p, pad="  " * max(p["depth"] - 1, 0)))
        return groups

    def place_views(places_doc: dict) -> Dict:
        held = places_held.get("held")
        if held is None or held[0] is not places_doc:
            by_code = places_mod.places_by_code(places_doc)
            labels = places_mod.labels_of(places_doc)
            options = [{"code": c, "depth": d, "label": labels.get(c, c),
                        "kind": by_code[c].get("kind") or ""}
                       for c, d in places_mod.tree(places_doc)]
            # For the form's type-ahead: each place with the labels of
            # what contains it, nearest first, so "Colombia" can be found
            # and shown as "South America › Latin America and the Caribbean".
            typeahead = [{"code": o["code"], "label": o["label"], "kind": o["kind"],
                          "path": [labels.get(a, a)
                                   for a in places_mod.ancestors(places_doc, o["code"])]}
                         for o in options]
            held = (places_doc, {"groups": group_places(options), "labels": labels,
                                 "options": typeahead})
            places_held["held"] = held
        return held[1]

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: User = Depends(current_user)):
        vocab_dict = vocab_cache.current()
        v = vocab_views(vocab_dict)
        return templates.TemplateResponse(request, "index.html", {
            "user": user,
            "read_enabled": read_enabled,
            "strands": keys.STRANDS,
            "domains": v["domains"],
            "facets": v["facets"],
            "facet_tree": v["facet_tree"],
            "vocabulary_version": vocab_dict.get("vocabulary_version"),
            "source_types": [(c, SOURCE_TYPE_HELP.get(c, "")) for c in SOURCE_TYPES],
            "activities": v["activities"],
            "providers": v["providers"],
            "places": place_views(places_cache.current())["options"],
            "rules": client_rules(),
        })

    def form_terms(vocab_dict: dict, facet: str, terms: Sequence[str]) -> List[Dict]:
        if not authority.is_authority(vocab_dict):
            return [{"slug": t, "depth": 0, "label": t, "scope": None} for t in terms]
        scopes = authority.scopes_of(vocab_dict)
        labels = authority.labels_of(vocab_dict)
        return [{"slug": slug, "depth": depth, "label": labels.get(slug, slug),
                 "scope": scopes.get(slug)}
                for slug, depth in authority.tree(vocab_dict, facet)]

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
                                      "subject", "version", "provider", "spatial")} | {
            "abstract": abstract[:200] + ("…" if len(abstract) > 200 else "")}

    def search_args(q, strand, state, sensitivity, subject, project, place=""):
        return dict(q=q, strand=strand or None, state=state or None,
                    sensitivity=sensitivity or None, subject=subject or None,
                    project=project or None, place=place or None)

    def place_view(cat: Catalogue, place: str) -> Optional[Dict]:
        """How a place filter was read (r11 §4): the codes it matched
        through the places hierarchy and how (exact, hierarchy, unknown),
        with the asked place's label for the count line. None without a
        place filter."""
        if not place:
            return None
        matched, how = cat.expand_place(place)
        labels = place_views(places_cache.current())["labels"]
        return {"asked": place, "matched": matched, "how": how,
                "label": labels.get(place, place)}

    def place_public(view: Optional[Dict]) -> Optional[Dict]:
        return {k: view[k] for k in ("asked", "matched", "how")} if view else None

    def subject_view(cat: Catalogue, subject: str, results=()) -> Optional[Dict]:
        """How a subject filter was read, for the page and the JSON feed:
        the slugs it matched through the vocabulary and how (exact,
        narrower, replaced, removed), a notice when the asked slug is
        retired, and the option the form shows as chosen. `narrower`
        counts only the narrower terms the results actually carry, so
        the count line never names terms the dropdown shows as unused.
        None without a subject filter."""
        if not subject:
            return None
        matched, how = cat.expand(subject)
        carried = {s for r in results for s in (r.get("subject") or [])}
        view = {"asked": subject, "matched": matched, "how": how,
                "narrower": len(set(matched[1:]) & carried) if how == "narrower" else 0,
                "notice": None, "selected": subject}
        if how == "replaced":
            successors = authority.map_subjects([subject], vocab_cache.current())[0]
            view["notice"] = ("%s is no longer a current term; showing %s."
                              % (subject, ", ".join(successors)))
            view["selected"] = successors[0] if len(successors) == 1 else ""
        elif how == "removed":
            view["notice"] = ("%s was retired with no successor; nothing carries it now."
                              % subject)
            view["selected"] = ""
        return view

    def subject_public(view: Optional[Dict]) -> Optional[Dict]:
        return {k: view[k] for k in ("asked", "matched", "how")} if view else None

    def subject_groups(cat: Catalogue):
        """[(facet, [term, ...])] for the subject select: the whole
        vocabulary in tree order, each term with the figure spaces that
        indent it under its parent."""
        groups = []
        for t in cat.term_tree():
            t = dict(t, pad="\u2007\u2007" * t["depth"])
            facet = t["facet"] or "other"
            if not groups or groups[-1][0] != facet:
                groups.append((facet, []))
            groups[-1][1].append(t)
        return groups

    PER_COOKIE = "crsw_per"

    def per_from(per: Optional[int], request: Request) -> Tuple[int, bool]:
        """The page size: the address's `per` when it is a choice (and
        then it is remembered in a cookie), else the cookie's, else the
        default. The flag says whether to set the cookie."""
        if per in PER_CHOICES:
            return per, True
        try:
            saved = int(request.cookies.get(PER_COOKIE, ""))
        except ValueError:
            saved = 0
        return (saved if saved in PER_CHOICES else PER_DEFAULT), False

    @app.get("/datasets", response_class=HTMLResponse)
    def datasets_page(request: Request, q: str = "", strand: str = "", state: str = "",
                      sensitivity: str = "", subject: str = "", project: str = "",
                      place: str = "", page: int = 1,
                      per: Optional[int] = None, user: User = Depends(current_user)):
        cat = need_read()
        per, remember = per_from(per, request)
        results = cat.search(**search_args(q, strand, state, sensitivity, subject,
                                           project, place))
        view = subject_view(cat, subject, results)
        # Projects first (r11 find page): the list folds into projects,
        # ten a page, closed while browsing and open once something
        # narrowed the list.
        groups = group_projects(results)
        shown, page, pages, per = page_of(groups, page, per)
        for g in shown:
            g["size"] = record.human_bytes(g["bytes"])
        query = [(k, v) for k, v in (("q", q), ("strand", strand), ("state", state),
                                     ("sensitivity", sensitivity), ("subject", subject),
                                     ("project", project), ("place", place)) if v]

        def pager_url(to_page: int, to_per: int) -> str:
            return "/datasets?" + urlencode(query + [("page", to_page), ("per", to_per)])

        response = templates.TemplateResponse(request, "datasets.html", {
            "user": user, "q": q, "strand": strand, "state": state,
            "sensitivity": sensitivity, "subject": view["selected"] if view else subject,
            "strands": keys.STRANDS, "states": keys.STATES,
            "sensitivities": keys.SENSITIVITIES, "subject_groups": subject_groups(cat),
            "subject_filter": view, "subject_notice": view["notice"] if view else None,
            "place": place, "place_groups": group_places(cat.place_options()),
            "place_filter": place_view(cat, place),
            "results": results, "groups": shown,
            "opened": bool(q or strand or state or sensitivity or subject or project or place),
            "n_projects": len(groups), "page": page, "pages": pages, "per": per,
            "per_choices": PER_CHOICES, "per_default": PER_DEFAULT, "pager_url": pager_url,
            "first": (page - 1) * per + 1, "last": min(page * per, len(groups)),
            "sizes": {r["identifier"]: record.human_bytes(r.get("bytes") or 0) for r in results},
            "built_at": cat.built_at(), "error": cat.error,
        })
        if remember:
            response.set_cookie(PER_COOKIE, str(per), max_age=365 * 86400, httponly=True,
                                samesite="lax")
        return response

    @app.get("/datasets.json")
    def datasets_json(q: str = "", strand: str = "", state: str = "", sensitivity: str = "",
                      subject: str = "", project: str = "", place: str = "", limit: int = 50,
                      user: User = Depends(current_user)):
        cat = need_read()
        limit = max(1, min(limit, 500))
        rows = cat.search(limit=limit,
                          **search_args(q, strand, state, sensitivity, subject, project, place))
        out = {"datasets": [public_row(r) for r in rows], "built_at": cat.built_at()}
        view = subject_view(cat, subject, rows)
        if view:
            out["subject_filter"] = subject_public(view)
            if view["notice"]:
                out["notice"] = view["notice"]
        pview = place_view(cat, place)
        if pview:
            out["place_filter"] = place_public(pview)
        return out

    # --- search inside documents (finding-and-reuse.md §7) ------------------

    SEARCH_TOP = 20          # passages the reranker reads

    def need_search():
        if not search_enabled:
            raise HTTPException(status_code=404, detail="Not Found")

    def need_passages():
        if passage_index is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return passage_index

    SORTS = ("words", "meaning")   # words first (the default), or by meaning alone

    def search_passages(user: User, q: str, limit: int, sort: str = "words") -> Dict:
        """Closest passages of files this user may download, best first.
        The question is embedded, the copy searched, the download rule
        applied to each hit, then the reranker reads the top few."""
        need_search()
        index = passage_index
        cat = need_read()
        q = " ".join((q or "").split())[:500]
        sort = sort if sort in SORTS else "words"
        # Only passages whose vector was made with what the alias resolves
        # to today can be scored against the question's. The promoter
        # rewrites the rest; until then they wait, and the page says so.
        model = platform.embed_fingerprint()
        out = {"q": q, "results": [], "notice": None, "steps": [],
               "stats": index.stats(model=model) if index is not None else None,
               "sort": sort, "can_explain": bool(platform.chat_model)}
        if not q or index is None:
            return out
        if out["stats"]["passages"] and not out["stats"]["current"]:
            out["notice"] = ("These passages were made with a different embedding model; "
                             "search inside documents is off until the promoter rewrites them.")
            return out
        try:
            # The question is a query: it carries the model's instruction.
            # The passages it is scored against were embedded bare.
            vec = platform.embed([q], instruction=settings.llm_query_instruction)[0]
        except PlatformError as e:
            out["notice"] = "Search inside documents is unavailable (%s)." % e
            return out
        out["steps"].append("meaning")
        # With amber served to nobody, do not even pull amber rows.
        sens = ("green",) if settings.amber_access == "off" else None
        stems = question_stems(q)
        hits = index.search(vec, limit=limit * 5, sensitivities=sens, phrase=q, stems=stems,
                            by_meaning=(sort == "meaning"), model=model)
        allowed = []
        for h in hits:
            strand = (h["identifier"] or "").split("/", 1)[0]
            if may_download(user, h["sensitivity"], strand, settings) is None:
                allowed.append(h)
        # Passages that contain the words asked stay in front; the reranker
        # orders only those found by meaning. Sorting by meaning alone puts
        # everything in one list by similarity, the order the copy already
        # returned (each still says why), and the reranker, which would
        # reorder it, is not asked.
        if sort == "meaning":
            pinned, rest = [], allowed
        else:
            pinned = [h for h in allowed if h["why"] != "meaning"]
            rest = [h for h in allowed if h["why"] == "meaning"]
        room = max(0, SEARCH_TOP - len(pinned))
        top, tail = rest[:room], rest[room:]
        if sort != "meaning" and platform.rerank_model and len(top) > 1:
            sendable = [i for i, h in enumerate(top)
                        if h["sensitivity"] in settings.llm_sensitivities]
            if len(sendable) > 1:
                try:
                    order = platform.rerank(q, [top[i]["text"] for i in sendable])
                    held = [top[i] for i in range(len(top)) if i not in sendable]
                    top = [top[sendable[j]] for j in order] + held
                    out["steps"].append("rerank")
                except PlatformError as e:
                    out["notice"] = "The reranker is unavailable (%s)." % e
        results = (pinned + top + tail)[:limit]
        pat = word_pattern(q)
        phrase_re = re.compile(re.escape(q), re.IGNORECASE) if q else None
        for h in results:
            row = cat.get(h["identifier"]) or {}
            h["dataset"] = row.get("dataset") or h["identifier"].rsplit("/", 1)[-1]
            h["score"] = round(float(h["score"]), 3)
            h["sentences"] = [{"text": s, "marked": False, "how": None}
                              for s in split_sentences(h["text"])]
            # A passage found by its words is marked by its words: the
            # sentences holding the phrase, else every telling word. No
            # call to the platform needed to say why.
            if h["why"] == "phrase":
                for s in h["sentences"]:
                    if phrase_re.search(s["text"]):
                        s["marked"], s["how"] = True, "phrase"
            if h["why"] == "words" or (h["why"] == "phrase"
                                        and not any(s["marked"] for s in h["sentences"])):
                for s in h["sentences"]:
                    found = {m.group(1).lower() for m in pat.finditer(s["text"])} if pat else set()
                    if stems and found.issuperset(stems):
                        s["marked"], s["how"] = True, "words"
        # Which sentence of each passage found by meaning is closest: one
        # call for them all. A one-sentence passage has nothing to choose.
        todo = [h for h in results if h["why"] == "meaning" and len(h["sentences"]) > 1
                and not any(s["marked"] for s in h["sentences"])]
        if todo:
            texts = [s["text"] for h in todo for s in h["sentences"]]
            try:
                vectors = platform.embed(texts)
            except PlatformError as e:
                out["notice"] = ((out["notice"] + " ") if out["notice"] else "") + \
                    "Highlighting is unavailable (%s)." % e
            else:
                i = 0
                for h in todo:
                    n = len(h["sentences"])
                    for s, marked in zip(h["sentences"], mark_closest(vectors[i:i + n], vec)):
                        s["marked"], s["how"] = marked, ("meaning" if marked else None)
                    i += n
                out["steps"].append("highlight")
        # A meaning-only result is strong when it is within the band of
        # the best and at least the floor (both settings;
        # weak-band-handoff.md): the band keeps a good best's neighbours,
        # the floor stops a poor best, as on a query the corpus has
        # nothing for, from showing noise as strong. Weak results are
        # folded on the page, never dropped. Words first: the best is the
        # best meaning score, and with word matches present every
        # meaning-only result is weak by comparison. By meaning: one list
        # by similarity, so the best is its top whatever found it, the
        # band and floor alone decide, and a word match is never weak.
        by_meaning = sort == "meaning"
        has_words = not by_meaning and any(h["why"] != "meaning" for h in results)
        best = max((float(h["score"]) for h in results
                    if by_meaning or h["why"] == "meaning"), default=0.0)
        cut = max(best - settings.search_weak_band, settings.search_weak_floor)
        for h in results:
            h["weak"] = h["why"] == "meaning" and (has_words or float(h["score"]) < cut)
        out["results"] = results
        out["strong"] = [h for h in results if not h["weak"]]
        out["weak"] = [h for h in results if h["weak"]]
        log.info("search user=%s chars=%d steps=%s hits=%d shown=%d",
                 user.username, len(q), "+".join(out["steps"]) or "-", len(hits), len(results))
        return out

    @app.get("/search", response_class=HTMLResponse)
    def search_page(request: Request, q: str = "", sort: str = "words",
                    user: User = Depends(current_user)):
        found = search_passages(user, q, limit=20, sort=sort)
        return templates.TemplateResponse(request, "search.html", {
            "user": user, **found,
            "error": passage_index.error if passage_index is not None else None,
            "sensitivities": settings.llm_sensitivities,
        })

    @app.get("/search/why")
    def search_why(q: str = "", identifier: str = "", member: str = "", position: int = 0,
                   user: User = Depends(current_user)):
        """The chat model's one sentence on what connects a passage to the
        question, fetched when a reader asks. The passage text goes to
        the platform, so only for the sensitivities that may be sent, and
        only for a passage this reader could download."""
        index = need_passages()
        q = " ".join((q or "").split())[:500]
        if not q or not platform.chat_model:
            raise HTTPException(status_code=404, detail="Not Found")
        h = index.get(identifier, member, position)
        if h is None:
            raise HTTPException(status_code=404, detail="no such passage")
        strand = (h["identifier"] or "").split("/", 1)[0]
        if may_download(user, h["sensitivity"], strand, settings) is not None:
            raise HTTPException(status_code=403, detail="not yours to read")
        if h["sensitivity"] not in settings.llm_sensitivities:
            raise HTTPException(status_code=403,
                                detail="this passage's sensitivity may not be sent to the platform")
        try:
            why = platform.explain(q, h["text"])
        except PlatformError as e:
            return JSONResponse({"why": None, "notice": "No explanation just now (%s)." % e},
                                status_code=503)
        log.info("search-why user=%s dataset=%s member=%s", user.username, identifier, member)
        return {"why": why, "notice": None}

    @app.get("/search.json")
    def search_json(q: str = "", limit: int = 20, sort: str = "words",
                    user: User = Depends(current_user)):
        found = search_passages(user, q, limit=max(1, min(limit, 100)), sort=sort)
        return {"q": found["q"], "notice": found["notice"], "steps": found["steps"],
                "stats": found["stats"], "sort": found["sort"],
                "passages": [{k: h.get(k) for k in ("identifier", "dataset", "member", "page",
                                                     "position", "text", "score", "sensitivity",
                                                     "why", "weak", "sentences")}
                             for h in found["results"]]}

    def load_record_text_or_404(cat: Catalogue, identifier: str):
        """The record as parsed and as stored, from one GET."""
        if cat.get(identifier) is None and not keys.dataset_prefix_ok(identifier):
            raise HTTPException(status_code=404, detail="no such dataset")
        try:
            text = cat.record_text(identifier)
        except Exception as exc:
            raise storage_error(exc)
        if text is None:
            raise HTTPException(status_code=404, detail="no such dataset")
        try:
            return record.parse_record(text), text
        except record.RecordParseError:
            raise HTTPException(status_code=404, detail="no such dataset")

    def load_record_or_404(cat: Catalogue, identifier: str) -> Dict:
        return load_record_text_or_404(cat, identifier)[0]

    @app.get("/datasets/{identifier:path}/files/{member:path}")
    def download(identifier: str, member: str, request: Request,
                 user: User = Depends(current_user)):
        """Stream one member of a dataset from the store to the browser
        (finding-and-reuse.md §1: the Ceph gateway is VPN-only, so the
        bytes pass through here; one chunk in memory, nothing on disk).
        Only members the record's manifest names are served, so this
        can never fetch an arbitrary key. A Range header is passed
        through so a large download can resume."""
        cat = need_read()
        rec = load_record_or_404(cat, identifier)
        refusal = may_download(user, rec.get("sensitivity"), rec.get("strand"), settings)
        if refusal:
            raise HTTPException(status_code=403, detail=refusal)
        entry = next((f for f in rec.get("files") or [] if f.get("path") == member), None)
        if entry is None:
            raise HTTPException(status_code=404, detail="no such file in this dataset")
        args = {"Bucket": settings.s3_bucket, "Key": cat.member_key(identifier, member)}
        wanted = request.headers.get("range")
        if wanted:
            args["Range"] = wanted
        try:
            obj = read_client.get_object(**args)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "InvalidRange":
                return Response(status_code=416,
                                headers={"Content-Range": "bytes */%d" % entry["bytes"]})
            if code in ("NoSuchKey", "404", "NotFound"):
                raise HTTPException(status_code=404,
                                    detail="the record names this file but the store "
                                           "has no object for it")
            raise storage_error(exc)
        except Exception as exc:
            raise storage_error(exc)
        name = member.rsplit("/", 1)[-1]
        headers = {
            "Content-Length": str(obj["ContentLength"]),
            "Accept-Ranges": "bytes",
            "Content-Disposition": "attachment; filename=\"%s\"; filename*=UTF-8''%s"
                                   % (name.encode("ascii", "replace").decode(), quote(name)),
        }
        if obj.get("ETag"):
            headers["ETag"] = obj["ETag"]
        if obj.get("LastModified"):
            headers["Last-Modified"] = format_datetime(
                obj["LastModified"].astimezone(timezone.utc), usegmt=True)
        status = 200
        if obj.get("ContentRange"):
            status = 206
            headers["Content-Range"] = obj["ContentRange"]
        body = obj["Body"]

        def chunks():
            try:
                for chunk in body.iter_chunks(DOWNLOAD_CHUNK):
                    yield chunk
            finally:
                body.close()

        log.info("download user=%s dataset=%s member=%s bytes=%s range=%s",
                 user.username, identifier, member, obj["ContentLength"], wanted or "-")
        # Always an attachment of unspecified type: the browser saves it
        # as named and never renders it, and the type table on the host
        # (Windows says .csv is Excel) cannot leak into the response.
        return StreamingResponse(chunks(), status_code=status, headers=headers,
                                 media_type="application/octet-stream")

    @app.get("/datasets/{identifier:path}/record")
    def dataset_record(identifier: str, user: User = Depends(current_user)):
        cat = need_read()
        _, text = load_record_text_or_404(cat, identifier)
        return Response(content=text, media_type="application/json")

    @app.get("/datasets/{identifier:path}", response_class=HTMLResponse)
    def dataset_page(identifier: str, request: Request, user: User = Depends(current_user)):
        cat = need_read()
        rec = load_record_or_404(cat, identifier)
        files = rec.get("files") or []
        derived = [r for r in cat.rows() if identifier in (r.get("derived_from_identifiers") or [])]
        v = vocab_views(vocab_cache.current())
        labels, scopes = v["labels"], v["scopes"]
        return templates.TemplateResponse(request, "dataset.html", {
            "user": user, "identifier": identifier, "rec": rec, "derived": derived,
            # Each subject as its label, slug and scope note, linked to the filter.
            "subjects": [{"slug": s, "label": labels.get(s), "scope": scopes.get(s)}
                         for s in (rec.get("subject") or [])],
            # r11: provider entries by slug (name, url) and place labels.
            "providers": {p["slug"]: p for p in v["providers"]},
            "place_labels": place_views(places_cache.current())["labels"],
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
                "release": release_tag, "auth_mode": settings.auth_mode}

    @app.get("/whoami")
    def whoami(user: User = Depends(current_user)):
        return {"username": user.username, "name": user.display_name,
                "auth_mode": settings.auth_mode}

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
                "providers": vocab.providers(vocab_dict),
                "places_version": places_cache.current().get("places_version"),
                "places_source": places_cache.source,
                "strands": list(keys.STRANDS), "states": list(keys.STATES),
                "sensitivities": list(keys.SENSITIVITIES)}

    # --- depositing to a dataset that exists (read role) ------------------
    def pending_rows(prefix: str) -> list:
        """Deposits to `prefix` still in staging: not in the index yet,
        but a researcher should know they are adding to them."""
        try:
            deps = store.deposits_for(prefix)
        except Exception as exc:
            raise storage_error(exc)
        return [{"id": d.id, "user": d.user, "created": d.created, "status": d.status,
                 "files": len(d.entries)} for d in deps]

    def destination(meta: Dict) -> Dict:
        """What is already at, or on its way to, the dataset prefix a
        deposit names. Empty when the read role is off."""
        prefix = deposit_logic.dataset_prefix(meta)
        out = {"identifier": prefix, "exists": False, "summary": None, "defaults": {},
               "manifest": [], "elsewhere": [], "similar": {"project": [], "dataset": []},
               "pending": []}
        if catalogue is None:
            return out
        row = catalogue.get(prefix)
        if row is not None:
            out["exists"] = True
            out["summary"] = public_row(row)
            rec = catalogue.record(prefix)
            if rec:
                out["defaults"] = deposit_logic.defaults_from_record(rec)
                out["manifest"] = [{"path": f["path"], "checksum_sha256": f.get("checksum_sha256"),
                                    "bytes": f.get("bytes")} for f in rec.get("files") or []]
        in_strand = catalogue.search(strand=meta["strand"])
        in_project = [r for r in in_strand if r.get("project") == meta["project"]]
        out["elsewhere"] = [public_row(r) for r in in_project
                            if r.get("dataset") == meta["dataset"] and r.get("identifier") != prefix]
        out["similar"]["project"] = keys.similar_projects(
            meta["project"], sorted({r.get("project") for r in in_strand if r.get("project")}))
        out["similar"]["dataset"] = keys.similar_projects(
            meta["dataset"], sorted({r.get("dataset") for r in in_project if r.get("dataset")}))
        out["pending"] = pending_rows(prefix)
        return out

    @app.get("/deposits/lookup")
    def lookup(strand: str = "", project: str = "", sensitivity: str = "", state: str = "",
               dataset: str = "", user: User = Depends(current_user)):
        need_read()
        meta = {"strand": strand, "project": keys.normalise_project(project),
                "sensitivity": sensitivity, "state": state,
                "dataset": keys.normalise_project(dataset)}
        if (strand not in keys.STRANDS or sensitivity not in keys.SENSITIVITIES
                or state not in keys.STATES or not meta["project"] or not meta["dataset"]):
            raise HTTPException(status_code=422, detail="strand, project, sensitivity, state "
                                                        "and dataset are all needed")
        return destination(meta)

    # --- Phase 2: the deposit API -----------------------------------------
    @app.post("/deposits", status_code=201)
    def create_deposit(form: Dict, user: User = Depends(current_user)):
        meta, errors, warnings = validate_meta(form, vocab_cache.current(),
                                               places_cache.current())
        if errors:
            raise HTTPException(status_code=422,
                                detail={"errors": errors, "warnings": warnings})
        prefix = deposit_logic.dataset_prefix(meta)
        dest = destination(meta) if catalogue is not None else None
        merges_with = existing_uuid = None
        if dest and (dest["exists"] or dest["pending"]):
            if dest["exists"]:
                merges_with = prefix
                existing_uuid = (dest["summary"] or {}).get("dataset_uuid")
                message = ("dataset %s already exists (%s files); your files will be "
                           "added to it" % (prefix, dest["summary"]["files"]))
                current = dest["defaults"].get("version")
                if not deposit_logic.version_not_lower(meta.get("version"), current):
                    raise HTTPException(status_code=422, detail={
                        "errors": {"version": "%s is lower than the dataset's %s; keep it "
                                              "or raise it" % (meta["version"], current)},
                        "warnings": warnings})
            else:
                message = ("a deposit to %s is waiting for promotion; your files will be "
                           "added to the same dataset" % prefix)
            if not bool(form.get("confirm_existing")):
                raise HTTPException(status_code=409, detail={
                    "message": message, "identifier": prefix, "summary": dest["summary"],
                    "pending": dest["pending"]})
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
            if merges_with:
                dep.merges_with, dep.existing_uuid = merges_with, existing_uuid
                store.save(dep)
        except HTTPException:
            raise
        except Exception as exc:
            raise storage_error(exc)
        log.info("deposit created user=%s deposit=%s prefix=%s merges_with=%s",
                 user.username, dep.id, prefix, merges_with)
        out = {"id": dep.id, "prefix": prefix,
               "staging_prefix": store.root(dep.user, dep.id) + "/" + prefix,
               "dataset_uuid": dep.dataset_uuid, "warnings": warnings}
        if catalogue is not None:
            out["merges_with"] = merges_with
        return out

    @app.get("/deposits/{deposit_id}")
    def get_deposit(deposit_id: str, user: User = Depends(current_user)):
        dep = load_or_404(user, deposit_id)
        return dep

    @app.put("/deposits/{deposit_id}/files/{member:path}")
    async def put_file(deposit_id: str, member: str, request: Request,
                       include_noise: bool = False,
                       user: User = Depends(current_user)):
        # This handler is async because it streams the body; every
        # blocking storage call goes to the threadpool so a slow one
        # never stalls the other requests on this worker.
        dep = await run_in_threadpool(load_or_404, user, deposit_id)
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
                use = await run_in_threadpool(quota.usage, store, dep.user)
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
        stored = await run_in_threadpool(store.stored_size, staged)
        if stored != size:
            await run_in_threadpool(store.delete, staged)
            raise HTTPException(
                status_code=502,
                detail="the upload appeared to finish but the stored object "
                       "is missing or the wrong size (expected %d, stored %s). "
                       "Re-send the file." % (size, stored))

        entry = record.manifest_entry(member, checksum, size,
                                      fmt=record.guess_format(member))
        dep.put_entry(entry)
        try:
            await run_in_threadpool(store.save, dep)
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
            vocab.activity_codes(vocab_cache.current()),
            provider_slugs=vocab.provider_slugs(vocab_cache.current()),
            place_codes=places_mod.codes(places_cache.current()))
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
        out = {"id": dep.id, "record_key": record_key,
               "dataset_uuid": dep.dataset_uuid,
               "files": len(union), "warnings": warnings,
               "staging_prefix": staged_prefix}
        if catalogue is not None:
            # What this deposit does to the dataset, by the CLI's own rule;
            # the promoter does the merge itself.
            existing = catalogue.record(dep.merges_with) if dep.merges_with else None
            old = (existing or {}).get("files") or []
            unchanged = record.unchanged_paths(old, dep.entries)
            _merged, _added, changed = record.merge_manifest(old, dep.entries)
            paths = {e["path"] for e in dep.entries}
            before = {e["path"] for e in old}
            out.update({"merges_with": dep.merges_with,
                        "added": sorted(paths - before),
                        "updated": sorted(set(changed) & paths),
                        "unchanged": sorted(unchanged)})
        return out

    return app
