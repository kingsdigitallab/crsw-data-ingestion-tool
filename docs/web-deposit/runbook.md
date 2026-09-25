# Runbook: web deposit service and promoter

Two VMs on eResearch OpenStack. Either can be rebuilt from a bare image in under an hour with this page. Nothing else runs on either host.

| | Web VM | Internal VM |
|---|---|---|
| Runs | `deposit-web` + nginx sidecar | `promoter`, once every five minutes |
| Reachable from | the KCL reverse proxy only | nothing inbound |
| Key on the host | `.env` with the **staging-only** key | `.env.promoter` with the **real** key |
| Must never hold | `.env.promoter` | (the web service) |
| Minimum instance | 1 vCPU, 2 GB RAM, 10 GB disk | 1 vCPU, 1 GB RAM, 10 GB disk |

The web VM is small because uploads stream through it: one worker peaked at 96 MiB RSS while streaming a 1 GB file (68 MiB idle), and nothing in flight touches disk. Container logs are the only thing that grows; compose rotates them (5 × 10 MB per container).

## 0. Before either VM: the KCL reverse proxy

In the proxy's management interface, Proxy mode, target the web VM by name and the service port (`CRSW_HTTP_PORT`, default 8080). Security section:

- Enable web application firewall: on.
- Restrict to KCL network: on for the KCL-only phase; off for the UoN pilot after the eResearch review.
- Capture (hide) errors: on.
- Require authentication: on. Allowed groups: `er_prj_kdl_slavery`.
- The proxy must forward the authenticated username (and groups, if it can) in a request header. As observed on 21 September 2026 it forwards none by default, and `er_prj_kdl_slavery` was refused by its group check while `er_kdl_bastion_users` passed. Both are open asks to eResearch; until they are answered, see "Interim: proxy gate without identity" in section 2.

The proxy is a webfarm: front nginx (TLS, HTTP/2) → Apache with mod_auth_mellon (SAML) → Keycloak at `kc.sso.er.kcl.ac.uk`. Apache's access log already records the signed-in k-number, so forwarding it is a config change on their side (security review, ask 4). Group membership is read from the SAML assertion at sign-in and propagates with a delay: after adding someone to the group, they must sign out of KCL SSO completely and back in, and may need to wait for the next sync. A 403 from the proxy for a newly added member is usually this, not a fault.

Note the proxy's load-balancer address ranges; they go into the security group and `CRSW_TRUSTED_PROXY_CIDRS`.

## 1. Host preparation (both VMs)

Both VMs run the same repository; the role is chosen by the compose command and the env file present. The checkout lives under `/opt`, named for the role, with a symlink in the admin's home so it is easy to find:

| | Web VM | Internal VM |
|---|---|---|
| Checkout | `/opt/crsw-deposit-web` | `/opt/crsw-data-promoter` |

```
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git   # or Podman + podman-compose if eResearch prefer
sudo usermod -aG docker $USER                    # log out and in again
DIR=/opt/crsw-data-promoter                      # or /opt/crsw-deposit-web
sudo mkdir -p $DIR && sudo chown $USER $DIR
git clone https://github.com/kingsdigitallab/crsw-data-ingestion-tool $DIR
mkdir -p ~/repos && ln -s $DIR ~/repos/$(basename $DIR)
cd $DIR && git checkout <tag>
```

Security group: web VM allows TCP `CRSW_HTTP_PORT` from the proxy ranges only, plus SSH from the admin range. Internal VM allows SSH from the admin range only.

Outbound, both VMs need HTTPS (443) to `rgw.ceph.er.kcl.ac.uk` and to `raw.githubusercontent.com` (the vocabulary, fetched on every promoter run and every ten minutes by the web service), plus `github.com`, Docker Hub and PyPI for the clone and the image build. Neither VM needs any other egress today. When natural-language search is built (`finding-and-reuse.md` §5) the internal VM will also need HTTPS to the KCL LLM platform host; bundle that with the read-key ask.

## 2. Web VM

```
cd /opt/crsw-deposit-web
cp .env.example .env            # fill in: staging key, CRSW_AUTH_MODE=proxy, CRSW_TRUSTED_PROXY_CIDRS
chmod 600 .env
export CRSW_HTTP_PORT=8080 CRSW_TRUSTED_PROXY_CIDRS=<ranges>   # also read by the nginx sidecar
docker compose -f deploy/compose.yaml up -d --build
curl -s localhost:8080/health
```

`/health` is the only path nginx serves to a source outside `CRSW_TRUSTED_PROXY_CIDRS`; every other path answers 403 from the VM itself, which is the allow-list working. The same CIDR value must be in `.env` (the app's peer check) and exported in the shell (the nginx sidecar), and the export is needed again for every later `docker compose` command.

First deploy only, to learn the identity header:

1. In `.env` set `CRSW_DEBUG_HEADERS=1` and, temporarily, `CRSW_AUTH_MODE=placeholder`; restart.
2. Through the proxy, signed in, open `https://<proxy-name>/auth/headers`. Note the header carrying your username (and any groups header). `peer` must be one of the proxy addresses; it is the address that connected to the sidecar, not the leftmost `x-forwarded-for` entry, which is the browser. If no header names you, the proxy is not forwarding identity: stop here and use the interim below.
3. Set `CRSW_PROXY_USER_HEADER` (and `CRSW_PROXY_GROUPS_HEADER`, `CRSW_PROXY_USERNAME_PATTERN` if the value is `k1234567@kcl.ac.uk`-shaped), set `CRSW_AUTH_MODE=proxy`, set `CRSW_DEBUG_HEADERS=0`; restart.
4. `https://<proxy-name>/whoami` must show your k-number.

### Interim: proxy gate without identity

While the proxy forwards no identity header, the proxy can still gate access (authentication on, with a group its directory evaluates) but the app cannot know who signed in. For the administrator's own end-to-end test only, run the app in `CRSW_AUTH_MODE=placeholder` with `CRSW_DEV_USER=<your k-number>` so test deposits carry the right depositor. This is never acceptable for the pilot: every deposit would be attributed to that one user.

Logs: `docker compose -f deploy/compose.yaml logs -f`. The app logs one line per create, upload, removal and finalise with the username and deposit id. Whenever proxy settings are saved, the load balancer fires a burst of scanner-shaped requests (`/i.php`, `/.hg`, a dozen browser identities, all 404 within a second): that is the proxy's web application firewall probing the backend, not an attack. In the webfarm's own logs every request appears twice, once from the front nginx and once from Apache; duplicate lines there are normal.

## 3. Internal VM

```
cd /opt/crsw-data-promoter
cp .env.promoter.example .env.promoter     # fill in the promoter key; set PROMOTER_AUTHORISED when known
chmod 600 .env.promoter
docker compose -f deploy/compose.yaml --profile internal build promoter
docker compose -f deploy/compose.yaml --profile internal run --rm promoter python -m promoter run --dry-run --env-file /dev/null
sudo cp deploy/promoter.service /etc/systemd/system/crsw-promoter.service   # WorkingDirectory=/opt/crsw-data-promoter
sudo cp deploy/promoter.timer   /etc/systemd/system/crsw-promoter.timer
sudo mkdir -p /var/log/journal && sudo systemctl restart systemd-journald   # journal survives reboots
sudo systemctl daemon-reload && sudo systemctl enable --now crsw-promoter.timer
journalctl -u crsw-promoter.service -f        # one JSON line per action
```

The unit's `WorkingDirectory` must be the checkout; edit the copied unit if it is anywhere other than `/opt/crsw-data-promoter`. The timer runs every five minutes with a lock, so runs never overlap. Exit code 1 means a deposit was refused and left in staging; read the `checked` line for the reasons. Exit code 2 on a real run means the run's audit object could not be written (section 6): the promotion happened, the journal has the lines, and the bucket needs looking at.

### Interim: promoter from a laptop

Until the internal VM exists, the web VM can run alone. Finalised deposits wait in `staging/`; nothing expires and nothing breaks, and each user's quota is freed when their deposits are promoted. Promote from an admin laptop on the VPN, from a checkout at the same tag as the web VM, with `.env.promoter` in the repo root. Use the checkout's own virtual environment: the system Python lacks boto3.

```
.venv/Scripts/pip install -e ".[web,dev]"           # once, and after each pull
.venv/Scripts/python -m promoter run --dry-run      # review
.venv/Scripts/python -m promoter run                # promote, log to promoter.log
```

`.env.promoter` still never goes on the web VM. When the internal VM arrives, install the timer there as above; the web VM is untouched.

## 4. Routine operations

- **Upgrade**: `git fetch && git checkout <new tag>`, then `docker compose -f deploy/compose.yaml up -d --build` on the web VM and `--profile internal build promoter` on the internal VM. Both must run the same tag.
- **Roll back**: check out the previous tag and rebuild; images are reproducible from the tag.
- **Disk**: after an upgrade run `docker system prune -f` to drop the old image layers. Logs are rotated by compose; `docker system df` shows what Docker holds.
- **Vocabulary change merged**: the web service re-fetches the vocabulary every `CRSW_VOCAB_REFRESH_SECONDS` (default 600), so a merged change is in the form within ten minutes; `docker compose -f deploy/compose.yaml restart deposit-web` makes it immediate. `GET /vocabulary` shows the version, source and load time the service is using.
- **Rotate a key**: edit the env file, restart the service (web) or nothing (promoter picks it up next run). Old key revoked by eResearch.
- **Refused deposit**: `journalctl` shows the problems. Fix at source (usually ask the researcher to re-deposit) or, for a policy refusal, adjust `PROMOTER_AUTHORISED`. A deposit is never edited in place.
- **Clear staging**: nothing to do; the lifecycle rule expires abandoned deposits and their noncurrent versions.
- **Check the service key's scope** after any policy change: `CRSW_INTEGRATION=1 .venv/Scripts/python -m pytest tests/test_integration_cluster.py` from a laptop on the VPN.

## 5. Categories after deposit (recategorise)

Subject terms and domains can change after a dataset is in place (r9,
`docs/specs/DEPOSIT_TOOL_SPEC_R9.md`). The promoter is the only thing
that rewrites a record, and it only ever rewrites the record: files,
labels, keys and the UUID never change. Every rewrite is a new object
version in the bucket and a `record_rewritten` line in the log.

The vocabulary is fetched live from the public repo
[kingsdigitallab/crsw-vocabulary](https://github.com/kingsdigitallab/crsw-vocabulary)
on every promoter run (the `start` log line names the source and
version). Changes to it go through that repo's issue forms and review.

Two sources of change:

- **The vocabulary's own changes** (a term renamed, merged, split or
  retired in the vocabulary repository). Applied to every record that
  carries an affected term, and written into the record's
  `category_history` by the actor `vocabulary`. A split gives the
  dataset every successor term and marks the entry `split_review`; a
  steward then removes the wrong ones with a change file.
- **A change file**: a steward's decisions about particular datasets.

Always dry-run first, on the internal VM (or from a laptop with the
promoter key, see the interim note above):

```
python -m promoter recategorise --dry-run
python -m promoter recategorise --dry-run --changes changes.json --by k1078591
```

On the internal VM the same commands run in the promoter container. The container is read-only and has no volumes, so a change file is mounted in for the run:

```
cd /opt/crsw-data-promoter
docker compose -f deploy/compose.yaml --profile internal run --rm promoter \n  python -m promoter recategorise --dry-run --env-file /dev/null
docker compose -f deploy/compose.yaml --profile internal run --rm \n  -v "$PWD/changes.json:/app/changes.json:ro" promoter \n  python -m promoter recategorise --dry-run --changes /app/changes.json --by k1078591 --env-file /dev/null
```

The dry-run prints one `recategorise_planned` line per record that
would change (subjects and domain before and after, and each change),
`recategorise_refused` for anything it will not do, and a `finish` line
with `would_rewrite`. Run it again without `--dry-run` to write. A second
run then finds nothing to do. `--dataset <identifier or uuid>` limits a
run to one dataset. The promoter also maps stale terms on a staged
deposit at promotion time (a `stale_terms_mapped` log line); a staged
deposit whose term was split is refused with the successors named, and
the depositor re-finalises with the right one.

Change file format, one entry per dataset, any of `add`, `remove`,
`set_domain`:

```json
[
  {"dataset": "rs2/csac/green/2_final/csac-clean",
   "add": ["prevalence"], "remove": ["survey-online"],
   "reason": "steward review after the survey split, issue #31"},
  {"dataset": "8f14e45f-ceea-467f-a34e-9db1c153f0a1",
   "set_domain": "geo", "reason": "reassigned to the geospatial steward"}
]
```

A change naming an unknown term, an unknown dataset, a term the record
does not have, or leaving a dataset with no subject at all is refused
and nothing is written for that dataset. Exit codes: 0 all rewritten or
nothing to do; 1 something refused or failed; 2 configuration error or
an unusable change file.

**Deferred**: a form in the web service for stewards to file a change
request into `staging/` for the promoter to pick up (r9 §1.4 B). Not
built; the change file is the route until a steward asks.

## 6. Audit: what has happened, and what is in place

Every promoter run (`run` and `recategorise`, dry runs included) ends by
writing its log lines as one object in the bucket:
`audit/promoter/YYYY/MM/DD/<run-id>.jsonl`, the run id being the UTC
time the run started. The bucket is versioned and the promoter never
rewrites an audit object, so this is the durable trail; the VM's
journal is a convenience copy that dies with the VM. `PROMOTER_AUDIT_PREFIX`
moves it; blank disables it (not for production).

Read it back from anywhere the promoter key is (the internal VM's
container, or a laptop checkout):

```
cd /opt/crsw-data-promoter
P="docker compose -f deploy/compose.yaml --profile internal run --rm promoter python -m promoter"
$P audit --env-file /dev/null                          # the newest ten runs, one line per action
$P audit --since 2026-09-01 --action promoted --env-file /dev/null
$P audit --dataset rs1/test/green/0_raw/test --env-file /dev/null   # or a dataset name or uuid
$P audit --user k1078591 --json --env-file /dev/null   # raw JSON lines for piping
$P datasets --env-file /dev/null                       # every record in place: depositor, created, modified, files, bytes
$P datasets --strand rs2 --json --env-file /dev/null
```

On the VM every promoter command runs inside the container like this:
the host has Docker and nothing else, no Python with boto3, and the key
is only ever mounted into the container from `.env.promoter`. A bare
`python -m promoter ...` on the VM will not work. From a laptop checkout
with the venv and a local `.env.promoter`, drop the `docker compose ...`
prefix and the `--env-file` flag:
`.venv/Scripts/python -m promoter audit --since 2026-09-01`.

Questions and where the answer is:

- *What happened to deposit X, and when?* `audit --dataset <name>`: its
  `checked` line (problems, warnings, size), then `promoted` with the
  dataset UUID, or `promotion_failed` with the error. The `start` line
  of that run names the vocabulary version used.
- *Who deposited what, this month?* `audit --since <date> --action
  promoted`.
- *Which records were rewritten when the vocabulary changed?*
  `audit --action record_rewritten`; the record's own
  `category_history` says what changed and why.
- *What is in the store now?* `datasets`. It reads the bucket, so it is
  always current; there is no register to keep in step.
- *Who created a deposit, or uploaded a file, before promotion?* The
  web VM's container log (`docker compose logs deposit-web`), rotated at
  50 MB. The promoter's `checked` line carries the depositor and the
  deposit id, which is enough for most questions.

Not built, recorded for the pilot: a read-only administrator page in the
web service showing the same two views. It needs the web VM's key
widened by eResearch to read `audit/` and the dataset prefixes (the same
read-scoped key the deferred data-egress path needs), and an
administrator group on the proxy. Until then the trail is read from the
command line as above.
