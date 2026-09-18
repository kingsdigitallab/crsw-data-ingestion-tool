# Web deposit service — proof-of-concept plan for Claude Code

Status: proposal, not yet approved. Build is exploratory and stays on the KCL network until eResearch and centre management have signed off.

## Purpose

Give CRSW researchers a browser-based way to deposit data into the Ceph S3 store that applies the same conventions as the existing Python deposit tool (structured keys, dataset-level metadata, manifest written last), without needing rclone, per-user S3 keys or the KCL VPN on the researcher's machine.

## Why

UoN researchers have KCL affiliation but cannot reliably use the KCL VPN. As of September 2026 no UoN researcher has completed a deposit via the rclone-wrapper script. A web service on a KCL-hosted VM can be reached via SSO without the VPN step, and keeps the deposit rules in code rather than in documentation.

## Decisions already taken (treat as fixed)

- Uploads stream from the browser through the service to Ceph. Nothing is written to the VM's disk. This is a day-one requirement, not an optimisation.
- The service holds a Ceph key that can write **only** to `staging/`. It cannot read, overwrite or delete anything under `raw/`, `interim/` or `final/`.
- A separate promoter process, running inside the KCL network with the real key, moves validated deposits from `staging/` to their destination. Promotion is on human approval by default, with an automatic mode available later.
- Deposit conventions (key construction, sidecar `.meta.json`, manifest-last, per-state rules) are shared with the existing deposit tool, not reimplemented. If the current tool is a script rather than a package, refactoring it into one is step 0.
- Authentication is KCL SSO with membership of `er_prj_kdl_slavery`. For the PoC the service sits on the KCL network only, with a simple placeholder login. The SSO integration must be a clean seam, not woven through the code.
- Ceph specifics apply throughout: path-style addressing, endpoint `rgw.ceph.er.kcl.ac.uk`, `force_path_style` / `addressing_style: path` wherever a client is configured.

## Shape of the service

Two-step deposit, deliberately mirroring how a presigned-URL flow would work so that switching the upload leg later is a config change rather than a rewrite:

1. `POST /deposits` — researcher submits dataset metadata (form or JSON). Service validates it, builds the key prefix, writes a placeholder record, returns a deposit id.
2. `PUT /deposits/{id}/files/{filename}` — browser sends each file as a raw request body (not multipart form encoding). Service streams the body straight to `staging/<user>/<deposit-id>/<filename>` using S3 multipart upload under the hood. Response includes size and checksum as stored.
3. `POST /deposits/{id}/finalise` — service writes `.meta.json` and the manifest last, marking the deposit complete and ready for promotion.

Raw-body PUT rather than multipart form parsing keeps streaming simple and avoids buffering. The browser side is a few dozen lines of JavaScript.

## Suggested stack

Python 3.12, FastAPI, boto3 (or aiobotocore if async streaming to S3 proves awkward with boto3). Jinja2 templates for the form; no front-end framework. Uvicorn behind nginx on the VM, with nginx enforcing request size caps and timeouts. Pytest with moto for S3 behaviour, plus a small integration test against a throwaway prefix on the real cluster.

If any of this conflicts with what the existing deposit tool uses, prefer consistency with the existing tool and say so.

## Deployment

Both the web service and the promoter are containerised from the first working skeleton (Phase 1), not retrofitted later. Docker by default; Podman acceptable if eResearch prefer rootless containers on their OpenStack images. Confirm with them before Phase 1.

- One repository, one build. A single compose file defines two services: `deposit-web` (FastAPI behind an nginx sidecar) and `promoter`. Both images are built from the same source and the same version of the shared conventions package.
- No upload storage anywhere. No volume or tmpfs for in-flight files. The nginx sidecar must set `proxy_request_buffering off` and `client_max_body_size` to the agreed cap so bodies pass straight through to the app.
- Secrets mounted, never baked in. The Ceph keys come from a mounted file or environment at run time. They must not appear in the image, the compose file or the repo. Add a check to CI that fails if they do.
- Config by environment. The same image serves the KCL-only phase and the pilot phase; only environment differs (auth mode, allowed origins, size caps, staging prefix).
- Nothing else on the host. Docker/Podman, the compose stack and system updates. Document the host setup as a short runbook so either VM can be rebuilt in under an hour.

## Phases and checkpoints

Stop at each checkpoint and wait for a review before continuing. Do not skip ahead.

**Phase 0 — orientation**
Read the existing deposit tool and the repo `docs/` folder. Produce a short note listing: which functions build keys, validate metadata and write manifests; whether they can be imported as-is; what needs refactoring into a shared package. Propose the package layout.
*Checkpoint: agree package layout and refactor scope.*

**Phase 1 — shared package and skeleton**
Extract conventions into a package both tools import. Existing deposit tool must still work unchanged from the user's point of view. Stand up the FastAPI skeleton with config loading, health endpoint, and a connectivity check that lists the staging prefix using the staging-only key. Deliver it as a compose stack (web service plus nginx sidecar) and deploy it to the VM from the image, not from a checkout.
*Checkpoint: existing tool passes its tests; containerised skeleton runs on the VM.*

**Phase 2 — deposit API**
Implement the three endpoints above with streaming upload. Unit tests with moto. Integration test writing to `staging/_test/` on the real cluster.
*Checkpoint: a deposit made via curl appears in staging with correct keys, sidecar and manifest.*

**Phase 3 — browser form**
Minimal form: metadata fields mirroring the deposit tool's prompts, file picker, progress per file, clear errors. Must work in Edge and Chrome on Windows and Safari on macOS.
*Checkpoint: Neil completes a deposit from a laptop on the KCL network.*

**Phase 4 — promoter**
Standalone script for the internal VM. Lists complete deposits in staging, runs checks (manifest consistent with objects present, checksums, size limits, filename allow-list, depositor authorised for target prefix), reports, and on approval copies to destination and deletes from staging. Dry-run mode first. Log every promotion.
*Checkpoint: dry-run output reviewed; one deposit promoted by hand.*

**Phase 5 — auth seam and hardening**
Replace placeholder login with an OIDC integration point (config only; real SSO registration needs eResearch). Per-user upload quota, rate limiting, nginx size caps, no other services on the VM. Add the promoter as a second service in the compose file for the internal VM.
*Checkpoint: security review with eResearch.*

**Phase 6 — documentation**
Update repo `docs/`: decision record for the web deposit approach, deployment notes for both VMs, researcher-facing how-to. Note what was deferred (presigned URLs, large-file handling beyond streaming).

## Not in scope for the PoC

- Presigned URLs (blocked on RGW reachability from outside KCL; revisit if that changes)
- Resumable uploads
- Catalogue integration
- Anything touching `interim/` — researchers with existing rclone access carry on as before

## Acceptance for the PoC

- A researcher on the KCL network can deposit a dataset of several files totalling a few GB through the browser, with no disk written on the VM
- Resulting keys, sidecar and manifest are byte-for-byte what the existing deposit tool would have produced
- Staging key demonstrably cannot write outside `staging/`
- Promoter refuses a deposit with a missing or inconsistent manifest
- Existing deposit tool unaffected

## Things to avoid

- Buffering uploads to disk or into memory
- Duplicating convention logic between the two tools
- Giving the web service anything other than the staging-only key
- Baking SSO assumptions into request handlers
- Putting credentials in images, compose files or the repository
- Silent promotion; every move from staging is logged and, for now, approved

## Open questions for eResearch

- Whether Docker or Podman is preferred on eResearch OpenStack images
- Provisioning a staging-only write policy for the service key
- Lifecycle rule to expire staging objects after N days
- Maximum object size and multipart part-size guidance on the cluster
- SSO app registration for the service, and group-membership claims for `er_prj_kdl_slavery`
- Whether the VM can later be exposed beyond the KCL network, and what that requires
