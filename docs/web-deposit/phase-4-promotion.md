# Phase 4 checkpoint: promotion

Date: 18 September 2026. Promoter run from the developer laptop over the KCL VPN with the dedicated promoter key in `.env.promoter`, against `staging/_test/`.

## Where the promoter runs

Same image as the web service, its own container, on the **internal** VM only, with the real key mounted at run time. It is a one-shot command, not a long-running service: a host timer runs `docker compose --profile internal run --rm promoter` every five minutes (`deploy/promoter.service`, `deploy/promoter.timer`, with a lock so runs never overlap). `.env.promoter` must never exist on the web VM.

Decision of 18 September: promotion is automated. Every complete deposit whose checks all pass is promoted without a prompt; anything amiss is left in staging and reported in the log. This replaces the plan document's "human approval by default". `--dry-run` reviews without moving.

Addition of 24 September, before the first run on the internal VM: the log in the VM's journal is not a durable audit trail (size-capped, lost with the VM), so each run now also writes its lines as one object in the bucket under `audit/promoter/`, and `promoter audit` / `promoter datasets` read the bucket back (runbook section 6). A read-only administrator page in the web service was considered and deferred: it needs the web VM's staging-only key widened by eResearch to read `audit/` and the dataset prefixes, which is the same read-scoped key the deferred data-egress path needs, plus an administrator group on the proxy. Raise both with eResearch together when the pilot needs non-technical staff to see activity. The read path itself (browse, search, download, the upstream-dataset picker) is planned in `finding-and-reuse.md` (25 September); the same key and the same ask cover it.

## When a deposit fails the checks

Against an honest web service, almost never: it already refused bad paths and noise, verified sizes and wrote the record last. The checks exist for when the service's bookkeeping cannot be trusted:

- The web VM was compromised and a plausible but fake deposit was written to staging with the staging key. The promoter re-derives everything from bytes and labels rather than believing the control object. This is the case that justifies the step.
- An object was swapped or corrupted at the same size. Only the SHA-256 re-hash catches it.
- Staging was edited by hand, or a lifecycle rule expired a version at the wrong moment, leaving manifest and objects out of step.
- The depositor is not entitled to the target strand (`PROMOTER_AUTHORISED`). Until SSO group claims exist this is the only place that policy lives.
- The destination already holds a record that does not belong there (moved or hand-edited). Building on it would corrupt a dataset.
- An object exceeds the policy size limit eResearch set (`PROMOTER_MAX_OBJECT_BYTES`, `PROMOTER_MAX_DEPOSIT_BYTES`).
- Web service and promoter disagree about labels or record shape (version drift). The monorepo makes this unlikely; the check costs nothing.

## Dry run over staging

```
start            {"dry_run": true, "vocabulary": "bundled", "staging": "staging/_test", "bucket": "crsw"}
checked          {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "ok": true, "problems": [], "warnings": [], "files": 2, "bytes": 9437192}
would_promote    {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "dataset_uuid": "68f52788-cd31-4b18-a2d0-7f41aff5218c"}
checked          {"deposit": "f09ced8b09de", "prefix": "rs1/test-project/green/0_raw/test-dataset", "ok": true, "problems": [], "warnings": [], "files": 1, "bytes": 41}
would_promote    {"deposit": "f09ced8b09de", "prefix": "rs1/test-project/green/0_raw/test-dataset", "dataset_uuid": "be0d9808-fcce-4490-9dec-578f7d08529a"}
finish           {"seen": 2, "promoted": 0, "refused_or_failed": 0, "dry_run": true}
```

Two deposits, both passing: the Phase 2 curl deposit and a form deposit made by the user.

## Promotion of dd85a7172677

```
start            {"dry_run": false, "vocabulary": "bundled", "staging": "staging/_test", "bucket": "crsw"}
checked          {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "ok": true, "problems": [], "warnings": [], "files": 2, "bytes": 9437192}
copied           {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "source": "staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/one.csv", "destination": "rs2/csac/green/0_raw/web-poc/one.csv", "bytes": 8}
copied           {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "source": "staging/_test/k1078591/dd85a7172677/rs2/csac/green/0_raw/web-poc/sub/blob.bin", "destination": "rs2/csac/green/0_raw/web-poc/sub/blob.bin", "bytes": 9437184}
record_written   {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "destination": "rs2/csac/green/0_raw/web-poc/dataset.web-poc.json", "files": 2, "added": 2, "updated": 0, "merged_with_existing": false}
promoted         {"deposit": "dd85a7172677", "prefix": "rs2/csac/green/0_raw/web-poc", "dataset_uuid": "68f52788-cd31-4b18-a2d0-7f41aff5218c", "files": 2, "record": "rs2/csac/green/0_raw/web-poc/dataset.web-poc.json"}
finish           {"seen": 1, "promoted": 1, "refused_or_failed": 0, "dry_run": false}
```

Independent confirmation with rclone:

```
$ rclone lsjson --metadata --recursive --files-only k1078591:crsw/rs2/csac/green/0_raw/web-poc
dataset.web-poc.json          1421  uuid=68f52788.. sha=e5e8dc79.. sens=green dep=k1078591
one.csv                          8  uuid=68f52788.. sha=492d5ea4.. sens=green dep=k1078591
sub/blob.bin               9437184  uuid=68f52788.. sha=b9118fa9.. sens=green dep=k1078591

$ rclone lsf -R --files-only k1078591:crsw/staging/_test/
k1078591/f09ced8b09de/_deposit.json
k1078591/f09ced8b09de/rs1/test-project/green/0_raw/test-dataset/dataset.test-dataset.json
k1078591/f09ced8b09de/rs1/test-project/green/0_raw/test-dataset/v2.txt
```

The promoted deposit is gone from staging (delete markers; versions remain per bucket policy). The user's form deposit is untouched.

## Defect found and fixed by this run

The promoted record's `created` was the promotion time, not the time the researcher finalised. Fixed in the shared package: `deposit_logic.assemble_record` now takes a `created` hint, used only when no destination record exists; the promoter passes the staged record's `created`. `modified` is the promotion time. The record already promoted at `rs2/csac/green/0_raw/web-poc` carries the wrong `created` (13:14 rather than 10:42 UTC); it is test data and was left as is.

Addition of 25 September (r8 §4): before copying anything, the promoter
looks up each "derived from" reference to another dataset in the store
and fills in the parent's UUID and version, rewriting the staged record
first so what is moved is what was checked (`record_rewritten` with
`where: staging`, then `resolved_reference` lines). A parent that is not
there is a warning; the reference is left as typed and the deposit still
promotes. Dry runs report what would be filled in and write nothing.

## Test counts at this commit

429 pass under pytest (moto), 430 under stdlib unittest; CLI `--dry-run` output unchanged from the pre-refactor baseline; secrets check passes.
