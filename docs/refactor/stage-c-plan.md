<!-- Generated-by: Claude Code:claude-fable-5-2 ; Prompted-by: Neil Jakeman ; Prompt summary: a written plan for Stage C of the legibility refactor (crsw_web/app.py): routes per router, how shared state is passed in, and the old-against-new comparison; nothing moved yet -->

# Stage C plan: `crsw_web/app.py`

Nothing has moved. This is the plan and the check that will guard it.

## What is there now

`crsw_web/app.py` is 1,282 lines. One function, `create_app`, runs from
line 111 to line 1,282 and holds everything: about 60 lines that build
the shared objects, 22 routes and 26 helpers, all nested inside it. Ruff
gives it a complexity of 193, because a nested function counts towards
the function that contains it.

Every route and helper reaches the shared objects as local variables of
`create_app`. That is why nothing can be lifted out as it stands.

## The shared state

Fourteen things are built once in `create_app` and used by the routes:

- `settings`: the configuration.
- `current_user`: the function that resolves who is signed in.
- `client`: the staging S3 client. `store`: the deposit store over it.
- `read_client` and `catalogue`: the read role. `None` when it is off.
- `platform`: the LLM platform. `None` when it is off.
- `passage_index`: the VM's copy of the passages. `None` when it is off.
- `vocab_cache` and `places_cache`.
- `templates`: the Jinja environment, with its globals and one filter.
- `release_tag`.
- the switches worked out from the above: `read_enabled`, `search_enabled`.
- two small memo dictionaries (`views_held`, `places_held`) that keep the
  form's vocabulary and places lists until the file in hand changes.

## Decision 1: how shared state reaches a route

**Question.** Once a route is no longer nested in `create_app`, how does
it get `settings`, `store` and the rest?

**Option A: one context object, handed to each route by FastAPI.**
`create_app` builds a frozen dataclass, `Context`, with the fourteen
items as fields, and stores it once as `app.state.ctx`. Every route
becomes a top-level function with one extra parameter,
`ctx: Context = Depends(context)`. Each route is then measured on its
own, and can be read and tested on its own.

**Option B: a factory per router.** Each router module has one function,
`make_router(ctx)`, and the routes stay nested inside it. Route bodies
move with no line edited at all. But the nesting stays, so ruff would
still report each factory at roughly the sum of its routes (the deposits
factory about 70), and a reader still has to hold a long enclosing
function in their head.

**Recommendation: A.** It is the only one that makes the code flatter,
which is the point of the stage. Its cost is that route signatures gain
a parameter and each body gains one line at the top naming what it uses
(for example `settings, store = ctx.settings, ctx.store`), so that the
original lines below it stay exactly as they are. Each commit message
will list those added lines, as agreed.

No module-level variables hold state in either option. The tests build
many apps in one process, so anything module-level would leak between
them; the two memo dictionaries in particular go inside the context.

Things that stay as they are under option A:

- `app.state.settings`, `.vocab_cache`, `.places_cache`, `.catalogue`,
  `.platform` and `.passage_index` are still set. The tests read
  `app.state.vocab_cache`.
- `current_user` becomes a top-level function that calls the
  authenticator held in the context. It takes only the request, as now,
  so the OpenAPI description does not change.
- `create_app` keeps its name, its module and its six arguments. The
  image starts `crsw_web.app:create_app`.

## Decision 2: where the routers live

**Question.** A `crsw_web/routes/` folder, or flat files?

**Recommendation: flat files, `crsw_web/routes_*.py`.** `pyproject.toml`
lists the packages by name (`packages = ["crsw_deposit", "crsw_web",
"promoter"]`). A new sub-folder would not be installed into the image
unless that line changed, and the tests, which run from the checkout,
would not notice. Flat files need no packaging change. Short names such
as `deposits.py`, `passages.py` and `catalogue.py` are already taken by
the modules the routes call.

## Routes per router

Six routers, included in this order, which reproduces today's
registration order exactly. Order matters inside two of them and is
kept: `/deposits/lookup` before `/deposits/{deposit_id}`, and the three
`/datasets/{identifier:path}…` routes longest first.

1. `routes_form.py` (the form and its summary page)
   - `GET /`
   - `GET /deposits/{deposit_id}/summary`
2. `routes_find.py` (Find data)
   - `GET /datasets`
   - `GET /datasets.json`
3. `routes_search.py` (Search by meaning or content)
   - `GET /search`
   - `GET /search/why`
   - `GET /search.json`
4. `routes_dataset.py` (one dataset: download, record, page)
   - `GET /datasets/{identifier:path}/files/{member:path}`
   - `GET /datasets/{identifier:path}/record`
   - `GET /datasets/{identifier:path}`
5. `routes_service.py` (about the service itself)
   - `GET /health`
   - `GET /whoami`
   - `GET /privacy`
   - `GET /auth/headers`
   - `GET /connectivity`
   - `GET /vocabulary`
6. `routes_deposits.py` (the deposit API)
   - `GET /deposits/lookup`
   - `POST /deposits`
   - `GET /deposits/{deposit_id}`
   - `PUT /deposits/{deposit_id}/files/{member:path}`
   - `DELETE /deposits/{deposit_id}/files/{member:path}`
   - `POST /deposits/{deposit_id}/finalise`

The upload routes (router 6) use `ctx.client` and `ctx.store` only and
never `ctx.read_client`, as the project rules require. That becomes
visible in one file where today it has to be taken on trust.

## Where the helpers go

- `crsw_web/context.py`: the `Context` dataclass, `build_context(...)`
  (today's lines 115 to 193: clients, caches, catalogue, platform,
  passage index, store, templates) and the `context` and `current_user`
  dependencies.
- `crsw_web/templating.py`: `emphasise`, `explore_link` and the template
  globals.
- `crsw_web/views.py`: what the pages show of the vocabulary and places
  (`vocab_views`, `place_views`, `group_places`, `form_terms`,
  `subject_view`, `place_view`, `subject_groups`, `public_row` and the
  two `_public` helpers).
- `crsw_web/search.py`: `search_all`, moved whole.
- The small guards stay beside the routes that use them: `need_read`,
  `need_search`, `need_passages`, `per_from`, `search_args`,
  `load_record_or_404`, `load_or_404`, `must_be_open`, `labels_for`,
  `storage_error`, `pending_rows`, `destination`. Those used by two
  routers (`need_read`, `storage_error`, `load_or_404`, `destination`)
  go in `context.py`.
- `app.py` keeps `create_app`, `client_rules`, `explorer_day`, the
  constants and the module docstring.

## Order of commits

One kind of change per commit, each followed by both test runners, the
two route lists and the comparison below.

1. Add `context.py` and have `create_app` build the context and take its
   local names from it. Nothing else moves.
2. Templating helpers out.
3. Views out.
4. `search_all` out, whole.
5. to 10. One router per commit, in the order above.
11. Remove the imports `app.py` no longer uses, except those the tests
    reach through it.

Splitting the long functions comes after, each as its own commit and
only with your say: `search_all` (87 on a rough count), `put_file` (23),
`search_why` and `download` (16 each), `create_deposit` (15),
`datasets_page`, `dataset_page` and `finalise` (14 each).

## What must not change, and how each is checked

- Status, headers and body of every response: `compare_web.py`, below.
- The OpenAPI description and the registered route list, switches off
  and on: `docs/refactor/baseline/collect.py`, compared with the saved
  files.
- Both test runners: 1001 passed and 1 skipped; 1002 run, 416 skipped.
- No test file is edited. Three tests reach into `crsw_web.app` by name
  and constrain the plan:
  - `tests/test_web_catalogue.py` imports `explorer_day` from it, so
    that function stays there.
  - `tests/test_web_form.py` patches `crsw_web.app.authority.tree` and
    `tests/test_web_quota.py` patches `crsw_web.app.quota.usage`. Both
    patch the shared module, so they keep working wherever the caller
    lives, provided `app.py` still imports `authority` and `quota`. It
    will, with a comment saying why, as `promoter/__main__.py` does.
- Privacy: no log line and no stored value is added or changed.

## The comparison: `docs/refactor/compare_web.py`

    .venv/Scripts/python docs/refactor/compare_web.py [BASE] [--show N]

It unpacks the whole `crsw_web` package from another commit (`main`
unless told otherwise) under another name, builds that app and this
checkout's app side by side over identically seeded mock stores, sends
both the same scripted requests, and compares status, the headers that
matter and the whole body. It exits 1 if anything differs.

What it covers today:

- 998 requests, 81 distinct method-and-path pairs, every one of the 22
  routes plus the static files.
- 13 configurations: read role off and on; amber open to all with the
  explorer link; search on with passages, without, with the embedding
  call failing, with rerank and explain failing, with reranker and chat
  switched off; proxy sign-in, trusted and untrusted; the header
  diagnostic on; small limits (body, quota, open deposits, members);
  another user.
- Statuses seen: 200, 201, 204, 206, 403, 404, 405, 409, 413, 416, 422,
  429 and 503. More than half the requests are error cases: refused
  forms (one reason each), reserved and refused file names, a program
  under another name, a body over the cap, a full quota, a deposit
  already finalised, unknown ids, bad ranges, another user's deposit, an
  untrusted peer, a platform that fails.

Result on the branch as it stands, where `crsw_web/` is the same as on
`main`: **0 of 998 differ.** As a test of the test, one word of one
error message was changed and the run reported 6 differences; the
change was then undone.

What it replaces before comparing, because these differ by design: the
bucket name (each app has its own), deposit ids, newly minted UUIDs and
timestamps. It does not compare a static file's `ETag`, which is made
from the file's modified time. Both apps are given the same release
string.

What it does not cover:

- Real Ceph, the real platform and the real proxy. The store is mocked.
- The other commit's `crsw_deposit` and `promoter`: both apps use this
  checkout's, since one process cannot hold two. Stage C does not touch
  them, and the Stage A and B comparisons cover them.
- Log lines. The tests that guard the access-log format are in the
  normal test run.
- Timing, and behaviour under many requests at once.
- A run takes about six minutes.
