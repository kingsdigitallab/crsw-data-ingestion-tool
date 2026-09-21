# Web deposit service: security review

Prepared for eResearch, 18 September 2026. Branch `feature/web-deposit`. Everything described here has been exercised on the developer laptop over the KCL VPN against `crsw/staging/_test/`, with the exception of the KCL reverse proxy's identity header, which can only be observed once the VM is behind the proxy.

## 1. Components and trust boundaries

```
 researcher's browser
        │  HTTPS, KCL sign-in, allowed groups, optional KCL-network restriction, WAF
        ▼
 KCL reverse proxy  (managed; terminates TLS; forwards user identity in a header)
        │  HTTP, only from the proxy's load-balancer ranges (OpenStack security group)
        ▼
 web VM ── nginx sidecar ── deposit-web (FastAPI)          holds: STAGING-ONLY key
        │  S3 over HTTPS, path style                         can write only under staging/
        ▼
 Ceph RGW  crsw bucket  ── staging/<user>/<deposit-id>/<final-prefix>/…
        ▲
        │  S3 over HTTPS
 internal VM ── promoter (one-shot, timer-driven)          holds: REAL key
                                                             copies staging → dataset prefix
```

Trust decreases left to right at each arrow. The design rule is that each layer assumes the one in front of it may have failed.

Until the internal VM is provisioned, the promoter is run by hand from an administrator's laptop on the KCL VPN, with the real key held only there; the web VM is deployed alone and deposits wait in `staging/` between runs.

## 2. What each credential can do

Measured with the probes in `tests/` and by hand on 18 September (see `phase-2-transcript.md` and the Phase 3 report):

| Credential | Write | Read | Delete | List |
|---|---|---|---|---|
| Staging key (web VM) | only under `staging/` | only under `staging/` | delete markers under `staging/` only | **whole bucket** (see gap 1) |
| Promoter key (internal VM) | dataset prefixes and `staging/` | both | delete markers; version deletion to be confirmed | whole bucket |
| Researcher | none: no researcher holds a durable storage credential for the web route | | | |

The bucket is versioned. Neither key needs to delete object versions; everything the service and promoter remove is a delete marker. Lifecycle policy must therefore expire noncurrent versions under `staging/` as well as current objects.

## 3. Identity

Sign-in is the KCL proxy's job ("Require authentication", "Allowed groups"). The service does not implement OIDC. In `CRSW_AUTH_MODE=proxy` it:

- reads the user from the header the proxy sets (`CRSW_PROXY_USER_HEADER`; name discovered on first deploy with the one-time `/auth/headers` diagnostic, then the flag is unset);
- optionally normalises it with a regex so the record's `depositor` field is the KCL username, matching CLI deposits;
- optionally re-checks group membership if the proxy forwards groups (`CRSW_PROXY_GROUPS_HEADER`), although the proxy's allowed-groups setting is the primary gate;
- **only believes the header when the connection comes from the proxy**: nginx `allow`/`deny` on `CRSW_TRUSTED_PROXY_CIDRS`, and the app checks the peer again using `X-Sidecar-Peer`, a header the sidecar always overwrites with the address that connected to it (the app port is reachable only from the sidecar). `X-Forwarded-For` is not used for this: the KCL proxy extends it and its leftmost entry is the browser. A request from anywhere else is refused before the header is read (403), and a request from the proxy without the header is 401.

Every mutating request is logged with the username and deposit id; bodies are never logged.

## 4. Threats and mitigations

| Threat | Mitigation | Enforced by |
|---|---|---|
| Someone reaches the VM directly and forges the identity header | Security group admits only the proxy ranges; nginx denies other sources; app re-checks the peer | OpenStack, nginx, app |
| Web VM compromised; attacker holds the staging key | Key writes only under `staging/`; cannot read, alter or delete deposited data; promoter re-derives every check from bytes and labels and never trusts the control object | Ceph policy, promoter |
| Malicious or careless depositor: bad paths, reserved names, noise, red data | Refused at the API (`crsw_deposit` rules); refused again by the promoter | app, promoter |
| Oversized or endless upload | `client_max_body_size` in nginx; `Content-Length` and running-total cap in the app; per-user staging quota; open-deposit cap; member-count cap | nginx, app |
| Flooding create/finalise | nginx `limit_req` 10/min with burst 5 (per source; behind the proxy that is per load balancer, so coarse); quota is the per-user limit | nginx, app |
| Upload buffered to disk on the VM | nginx `proxy_request_buffering off`; app streams in 8 MiB parts; container root filesystem read-only, no volumes or tmpfs; measured: 1 GB upload peaked at 96 MiB RSS | nginx, app, compose |
| Deposit promoted while still uploading | Promoter only sees deposits whose control object says complete, which finalise writes last after verifying every object; after that, further PUTs are refused | app, promoter |
| Two promoter runs overlap | `flock -n` in the systemd unit | host |
| Object swapped at the same size after finalise | Promoter re-hashes every object and compares to the manifest | promoter |
| Deposit to a strand the user may not use | `PROMOTER_AUTHORISED` map (currently `*`); to be driven by group claims once the header shape is known | promoter |
| Existing dataset at the destination | Merged by the same rules as a CLI re-deposit (UUID and `created` kept, manifest unioned, depositor appended), with a warning in the log; a foreign or unparsable record blocks promotion | promoter |
| Abandoned half-finished deposits filling staging | Ignored by the promoter; cleared by the staging lifecycle rule (ask 2); quota limits the damage per user | Ceph lifecycle |
| Secrets in images or the repository | Keys only via env files at run time; `.dockerignore`; `scripts/check-no-secrets.sh` on every commit | build, CI |
| Cross-site scripting or framing of the form | CSP `default-src 'self'`, `X-Frame-Options DENY`, nosniff, same-origin referrer; no inline executable script | nginx |
| Error pages leaking internals | Storage errors are translated into fixed messages; no stack traces in responses (tested); proxy "Capture (hide) errors" as well | app, proxy |

## 5. Known gaps and what we are asking for

1. **The staging key can list the whole bucket.** Object names are metadata. Please scope the policy's `ListBucket` to the `staging/` prefix.
2. **Lifecycle rule on `staging/`**: expire current objects after N days *and* noncurrent versions, or staging will grow forever under versioning.
3. **Confirm the security group** admits only the proxy's three load-balancer ranges on the service port, and nothing else on the web VM.
4. **Identity header**: please configure the proxy to forward the authenticated username, and groups if possible, in a request header, and tell us its name. Observed on 21 September 2026 with authentication on: the proxy forwards only `x-forwarded-*`, `x-real-ip` and `via`; nothing identifies the user. Without it the service cannot record who deposited.
5. **Promoter key**: confirm it need not delete object versions (delete markers are enough), or grant it if the lifecycle approach is not acceptable.
6. **Docker or Podman** on eResearch OpenStack images; the compose file is engine-agnostic.
7. **Exposure beyond KCL** for the UoN pilot is a proxy setting ("Restrict to KCL network" off); we ask for your view before it is changed.
8. **Group check**: with allowed group `er_prj_kdl_slavery` the proxy refuses a member of that group (403 before the VM is reached); with `er_kdl_bastion_users` the same person passes. Which directory does the proxy evaluate groups against, and what is the storage project group called there?

## 6. How to verify each claim

```
# tests, including tampered-object, forged-header and quota cases
python -m unittest                          # stdlib only
.venv/Scripts/python -m pytest -q           # with moto

# no secrets tracked
sh scripts/check-no-secrets.sh

# staging key scope (needs .env)            -> write outside staging denied
.venv/Scripts/python -m pytest tests/test_integration_cluster.py   # CRSW_INTEGRATION=1

# proxy mode locally: 401 without header, 200 with, 403 from an untrusted peer
CRSW_TRUSTED_PROXY_CIDRS=172.16.0.0/12 docker compose -f deploy/compose.yaml up -d --build
curl -i localhost:8080/whoami
curl -i -H 'X-Remote-User: k1078591' localhost:8080/whoami

# rate limit: after the burst, 429s
for i in $(seq 20); do curl -s -o /dev/null -w '%{http_code} ' -X POST -d '{}' -H 'Content-Type: application/json' localhost:8080/deposits; done

# promoter dry run against staging (needs .env.promoter)
.venv/Scripts/python -m promoter run --dry-run
```
