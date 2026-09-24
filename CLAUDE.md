# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

One repository, two deposit routes into CRSW's shared object storage (Ceph, S3-compatible, hosted by KCL eResearch), sharing one conventions package:

- `crsw_deposit/` — the conventions: key construction, dataset record, manifest, object labels, vocabulary (`vocab.py` fetch chain, `authority.py` the term authority file's rules, `vocab_cli.py` the `crsw-vocab` command), noise rules. **Stdlib-only, Python 3.8+.** Both routes import it; nothing convention-shaped may live anywhere else.
- `deposit.py` + `transfer.py` — the command-line tool (rclone-based). Built and working. Researchers run it from a checkout with no install.
- `crsw_web/` — the browser-based deposit service (FastAPI, boto3, Python 3.12) for researchers who cannot use the KCL VPN. Proof of concept in progress; see `docs/web-deposit/plan.md` for phases and checkpoints.
- `promoter/` — moves checked deposits from `staging/` to their dataset prefix, and (`recategorise`) rewrites records in place when categories change. Runs on the internal VM only, with the real key, from the same image (`docker compose --profile internal run --rm promoter`, or `python -m promoter run` locally with `.env.promoter`). Never in the web container.

Specs: `docs/specs/DEPOSIT_TOOL_SPEC.md` is the original brief; `_R2`…`_R9` each **supersede it where they speak** (r8 provenance, r9 categories after deposit; both still marked "proposal" pending agreement). The vocabulary itself lives in the public repo `kingsdigitallab/crsw-vocabulary`; `docs/vocabulary-repo/` is the starter kit that seeded it. `docs/web-deposit/` holds the web service plan, proposal and Phase 0 orientation note. Read the relevant ones before changing behaviour.

## Commands

```
python -m unittest                      # all tests, stdlib only, from repo root
python -m unittest tests.test_keys      # one module
python -m unittest tests.test_keys.TestBuildKey.test_happy_path
python -m pytest                        # same tests plus web tests; needs pip install -e ".[dev]"
pip install -e ".[web,dev]"             # service + test deps into a venv (never required for the CLI)
python deposit.py FILE_OR_FOLDER [...] --strand rs2 --project csac --dataset x --state 2_final --sensitivity green --domain quant --dry-run
docker compose -f deploy/compose.yaml up --build   # web service + nginx sidecar, reads .env
python -m promoter run --dry-run                   # check staged deposits; drop --dry-run to promote
python -m promoter recategorise --dry-run [--changes FILE] [--dataset ID]   # r9: bring records up to the vocabulary
python -m crsw_deposit.vocab_cli --file vocab.json validate|tree|add|rename|merge|split|retire|move   # crsw-vocab
```

`--dry-run` with full flags is the support reproduction path and the CLI parity check; keep it working and keep its output stable.

## Hard constraints

- **`crsw_deposit/` is stdlib-only, Python 3.8 floor, PyInstaller-compatible.** No `dict | dict`, no runtime `list[str]`, no `str.removeprefix`, no dynamic imports. `vocab.json` is package data; `bundled_vocab_path()` keeps its `sys._MEIPASS` fallback.
- **`crsw_web/` and `promoter/` may use Python 3.12, FastAPI, boto3.** They run on a centrally managed VM. They must never re-implement anything in `crsw_deposit/`.
- **Cross-platform.** `pathlib` for local paths, but object keys always use `/`. Never let `os.sep` leak into a key or a manifest path.
- **No secrets in the repo.** CLI credentials live in rclone's config. Service credentials come from `.env` (gitignored) or a mounted file at run time; never in images, compose files or source. `scripts/check-no-secrets.sh` must pass.
- **Every convention has one implementation.** If the CLI and the service disagree about a key, label, record field or byte, the fix goes in `crsw_deposit/`, not in either caller.

## Storage conventions (deliberate decisions, do not "fix" them)

- Key layout `{strand}/{project}/{sensitivity}/{state}/{dataset}/{member}` (r6 §1). `member` may contain `/` (r7 §1). `keys.build_key` is the only place keys are built and validates every part.
- The dataset record is `{prefix}/dataset.<dataset>.json`: description **and** `files` manifest in one document (r5 dropped per-file sidecars). Schema 0.6 in `dataset.schema.json` is the CI contract (0.5 is read and upgraded by `record.upgrade_record`, never written); runtime validation is hand-rolled in `record.validate_record`. Dublin Core spellings: `subject`, `license`, `temporal: {start, end}`.
- **Members first, record last** (r5 Q4). The record's presence marks a complete deposit. Deposits are additive: re-runs skip unchanged members (`record.unchanged_paths`), rewrite the record, never remove a manifest member.
- **Four object labels on every upload** (`x-amz-meta-` dataset-uuid / checksum-sha256 / sensitivity / depositor), including the record itself. Names come from `crsw_deposit.labels`; each transport adds the `x-amz-meta-` prefix its own way.
- **Record bytes are LF-only** so the record's checksum label is platform-independent (r7 §4). `deposit_logic.record_bytes` is the one serialiser.
- **Verification compares sizes, never SHA-256 against ETag** (multipart ETags are hash-of-hashes).
- `sensitivity=red` is refused outright (TRE, not shared storage). Unknown subject terms are refused. Filenames are preserved exactly; corrections are offered, never applied silently. `dataset.*.json` is reserved at every path segment.
- Abstract guidance is 50 words, warn-then-confirm, deliberately soft for the testing phase; the schema carries no length constraint.
- Domains come from the vocabulary; `keys.DOMAINS` is only a stale-cache fallback. Vocabulary chain: fetch → cache → bundled; a fetched file must pass `authority.validate_authority` before it is trusted.
- **Categories change only through the promoter** (r9). The vocabulary is a term authority file (every term ever approved, status, successors, optional `broader` parent, append-only `changes`); `authority.map_subjects` replays its changes over a subject list (rename/merge substitute, split gives every successor as `split_review`, retire drops). A record's history of such changes is its own `category_history`; there is no central register. Nothing in this repo writes to the vocabulary repo.
- OS noise (`.DS_Store`, `Thumbs.db`, `._*`, `.git/`, …) is excluded by default and always counted, never silently. `crsw_deposit.noise` holds the rules for both routes.
- New project or dataset creation triggers the restricted-access guard (r6 §8); "yes" aborts because the restriction must exist before the first deposit.

### CLI-specific

- `rclone copyto`, never `copy`; always `--ignore-times` (a skipped upload means stale labels). Confirmed against rclone v1.74.1.
- Remote/bucket: flag → `config.json` → default. `--remote`/`--bucket` that differ from saved values get one offer to persist (`deposit.offer_to_save_settings`), never under `--dry-run` or on a non-tty. `save_config` is the only writer.
- Preflight failures each need a specific, actionable message (spec §8): VPN-off names the KCL VPN, missing rclone gives the download URL. If the output mentions sockets, TLS or S3 error codes, it is not finished.
- All colour goes through `deposit.style()`; `NO_COLOR`/`FORCE_COLOR`/non-tty honoured.
- `deposit.py` is the only file allowed to talk to the user.

## Web deposit service: fixed decisions (do not relitigate)

- **Streaming only.** Browser → service → Ceph via S3 multipart upload. Never buffer to disk or fully into memory. No volumes or tmpfs. nginx sets `proxy_request_buffering off` and `client_max_body_size` to the agreed cap.
- **Staging-only key.** The service's Ceph credential writes only under `staging/`. It never gets a key that can touch the strand prefixes. (Local dev uses a personal key as a stand-in until eResearch provision one.)
- **Staging keys** are `staging/<user>/<deposit-id>/<final-prefix>/<member>`, so promotion is a prefix strip and the record is byte-identical before and after.
- **Separate promoter** inside the KCL network with the real key. Automated: every complete deposit whose checks all pass is promoted; anything amiss is reported and left in staging (decision of 18 Sept 2026, replacing the plan's "human approval by default"). `--dry-run` reviews without moving. Every action is a JSON line in the log. Checks: record parses and validates, manifest matches objects (sizes), SHA-256 re-hashed, all four labels, size limits, member paths, depositor authorised for the strand (`PROMOTER_AUTHORISED`), destination record sane. Members first, record last, verify, then delete markers in staging (control object last).
- **Existing destination record: merge with a warning.** Same rules as a CLI re-deposit via `deposit_logic.assemble_record(meta, existing, …)`: keep `dataset_uuid` and `created`, union the manifest, append the depositor, widen coverage. Copied objects are re-labelled with the kept UUID. A destination record that cannot be parsed or whose identifier does not match its location blocks promotion.
- **Auth is a seam, and sign-in is not ours.** The KCL reverse proxy in front of the VM does KCL sign-in, allowed-groups, optional KCL-network restriction, WAF and TLS. `crsw_web.auth` is the only place identity is resolved: `placeholder` mode for local dev, `proxy` mode in production reads the user from the proxy's header (`CRSW_PROXY_USER_HEADER`, discovered on first deploy via the one-time `/auth/headers` diagnostic) and only trusts it from `CRSW_TRUSTED_PROXY_CIDRS` (nginx allow/deny plus an app-side peer check). Do not add OIDC to the app. The depositor field holds the KCL username so both routes agree.
- **Limits live in the app and nginx.** Per-user staging quota (`CRSW_USER_QUOTA_BYTES`, computed by listing `staging/<user>/`), open-deposit cap, member cap, body cap; nginx rate-limits create/finalise and caps concurrent uploads per source. Behind the proxy "source" is a load balancer, so the quota is the real per-user limit.
- **Ceph specifics everywhere.** Endpoint `rgw.ceph.er.kcl.ac.uk`, path-style addressing on every client.
- **Containerised from Phase 1.** One compose file: `deposit-web` + nginx sidecar, promoter added in Phase 4. Deploy from the image, not a checkout. Docker by default; Podman if eResearch prefer.
- **Config by environment.** Same image for KCL-only and pilot phases. See `.env.example`.
- **Local end-to-end first.** Phase 2 and 3 checkpoints run on the developer's laptop over the VPN against `crsw/staging/_test/`. VM deployment waits for OpenStack access.

### API shape (Phase 2)

1. `POST /deposits` — metadata in, validated with `crsw_deposit`, prefix built, deposit id returned.
2. `PUT /deposits/{id}/files/{member}` — **raw request body, not multipart form**. Streamed to staging with the four labels. Returns size and checksum as stored.
3. `POST /deposits/{id}/finalise` — writes the record last via `deposit_logic.assemble_record` and `record_bytes`.

### Phases and checkpoints

`docs/web-deposit/plan.md` defines Phases 0–6. **Stop at each checkpoint and wait for review.** Phase 0 is done (`docs/web-deposit/phase-0-orientation.md`). Where the plan's wording is stale against the tool (it predates r5: no `.meta.json` sidecar, no bare `raw/` prefixes, rclone not required), follow the tool and say so.

### Out of scope for the PoC

Presigned URLs, resumable uploads, catalogue integration, anything touching `interim/`, existing-record defaults in the form (the staging key cannot read destinations), and data egress.

### Deferred: data egress

The same VPN problem blocks Nottingham researchers from downloading. Not in the PoC, but keep `crsw_web.auth` and credential handling structured so a separate read-scoped key and a per-prefix authorisation check can be added without touching the upload handlers. Record in the Phase 6 decision record.
