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
- The proxy must forward the authenticated username in a request header. It forwarded none by default (observed 21 September 2026); since 16 October 2026 it sends `x-webauth-user` (the k-number), `x-webauth-name` and `x-webauth-mail`, and the service runs in proxy mode on them (section 2). No groups header is sent, so `CRSW_AMBER_ACCESS=groups` is still not available. The group check (`er_prj_kdl_slavery` refused while `er_kdl_bastion_users` passed) remains an open ask.

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

Outbound, both VMs need HTTPS (443) to `rgw.ceph.er.kcl.ac.uk` and to `raw.githubusercontent.com` (the vocabulary, fetched on every promoter run and every ten minutes by the web service), plus `github.com`, Docker Hub and PyPI for the clone and the image build. With the KCL LLM platform configured (section 7, "asking in plain words"; `finding-and-reuse.md` §5) both VMs also need HTTPS to `ai.create.kcl.ac.uk`: the internal VM to embed records after a promotion, the web VM to read a question. Neither VM needs any other egress. If the platform host is blocked, leave the `*_LLM_*` variables blank and everything else runs as before; the ask is one line to eResearch.

## 2. Web VM

```
cd /opt/crsw-deposit-web
cp .env.example .env            # fill in: staging key, CRSW_AUTH_MODE=proxy, CRSW_TRUSTED_PROXY_CIDRS
chmod 600 .env
export CRSW_HTTP_PORT=8080 CRSW_TRUSTED_PROXY_CIDRS=<ranges>   # also read by the nginx sidecar
docker compose -f deploy/compose.yaml up -d --build
curl -s localhost:8080/health
```

The read role (section 7) stays off on the VM until eResearch issue the read-scoped key: leave `CRSW_READ_S3_ACCESS_KEY` and `CRSW_READ_S3_SECRET_KEY` blank. **Never put a personal key on the web VM**; a personal key can write anywhere, and the staging-only design depends on the VM holding nothing that can.

The same goes for the LLM platform key: `CRSW_LLM_BASE_URL` and `CRSW_LLM_API_KEY` on the VM should be a key issued to the project, not a person's, so it can be rotated when people change. Blank keeps "ask in your own words" off; the find page still works.

`/health` is the only path nginx serves to a source outside `CRSW_TRUSTED_PROXY_CIDRS`; every other path answers 403 from the VM itself, which is the allow-list working. The same CIDR value must be in `.env` (the app's peer check) and exported in the shell (the nginx sidecar), and the export is needed again for every later `docker compose` command.

First deploy only, or after a change on the proxy's side, to learn the identity header:

1. In `.env` set `CRSW_DEBUG_HEADERS=1` and, temporarily, `CRSW_AUTH_MODE=placeholder`; restart.
2. Through the proxy, signed in, open `https://<proxy-name>/auth/headers`. Note the header carrying your username (and any groups header). `peer` must be one of the proxy addresses; it is the address that connected to the sidecar, not the leftmost `x-forwarded-for` entry, which is the browser. If no header names you, the proxy is not forwarding identity: stop here, the app cannot know who signed in, and placeholder mode on the VM attributes every deposit to `CRSW_DEV_USER` (acceptable for the administrator's own test only, never for the pilot).
3. Set `CRSW_PROXY_USER_HEADER` (and `CRSW_PROXY_NAME_HEADER` for the display name; `CRSW_PROXY_GROUPS_HEADER`, `CRSW_PROXY_USERNAME_PATTERN` only if the proxy sends groups or a `k1234567@kcl.ac.uk`-shaped value), set `CRSW_AUTH_MODE=proxy`, set `CRSW_DEBUG_HEADERS=0`; restart.
4. `https://<proxy-name>/whoami` must show your k-number.

*Result, 16 October 2026:* the diagnostic showed `x-webauth-user: k1078591`, `x-webauth-name`, `x-webauth-mail`, no groups header, `peer` `10.202.65.118` (inside `CRSW_TRUSTED_PROXY_CIDRS`, which lists the proxy's three addresses `10.202.65.116`, `.117`, `.118`). Header names are case-insensitive. The switch from the interim placeholder mode was:

```
cd /opt/crsw-deposit-web
grep -n '^CRSW_\(AUTH_MODE\|PROXY_USER_HEADER\|PROXY_NAME_HEADER\|TRUSTED_PROXY_CIDRS\|DEBUG_HEADERS\)=' .env .env.local 2>/dev/null
# in .env (or .env.local, loaded after .env): CRSW_AUTH_MODE=proxy,
# CRSW_PROXY_USER_HEADER=x-webauth-user, CRSW_PROXY_NAME_HEADER=x-webauth-name
# (v0.12.0 or later), CRSW_PROXY_USERNAME_PATTERN= (blank), CRSW_DEBUG_HEADERS=0
export CRSW_HTTP_PORT=8080 CRSW_TRUSTED_PROXY_CIDRS=$(grep '^CRSW_TRUSTED_PROXY_CIDRS=' .env | cut -d= -f2-)
docker compose -f deploy/compose.yaml up -d      # recreates deposit-web with the new env
curl -s localhost:8080/health                     # "auth_mode": "proxy"
curl -si localhost:8080/whoami | head -1          # 403 from the VM itself: not the proxy, as designed
```

Through the proxy, `/whoami` shows your k-number (and `name` once the name header is set), `/auth/headers` is 404 again, and the form says "Depositing as *Your Name* (k1234567)". The proof that identity is real is a colleague opening `/whoami` and seeing their own k-number; their test deposit lands under `staging/<their k-number>/` and, promoted, carries it as depositor (`python -m promoter datasets` or `promoter audit --since <date>` on the internal VM). With real depositors `PROMOTER_AUTHORISED` can become a per-user strand map whenever the Centre wants one (`.env.promoter`, section 3); `*` stays right while the proxy's allowed group is the gate and every member may deposit to every strand.

The username is lower-cased and must be a plain key segment (letters, digits, `.`, `_`, `-`, up to 64 characters), since it becomes `staging/<user>/`; anything else is refused with 403 "not a usable username". The display name is shown on pages only and never written anywhere; `x-webauth-mail` is not read.

Logs: `docker compose -f deploy/compose.yaml logs -f`. The app logs one line per create, upload, removal and finalise with the username and deposit id. Whenever proxy settings are saved, the load balancer fires a burst of scanner-shaped requests (`/i.php`, `/.hg`, a dozen browser identities, all 404 within a second, or 401 in proxy mode since they carry no identity header): that is the proxy's web application firewall probing the backend, not an attack. In the webfarm's own logs every request appears twice, once from the front nginx and once from Apache; duplicate lines there are normal.

## 3. Internal VM

```
cd /opt/crsw-data-promoter || echo "WRONG MACHINE"   # the internal VM only; see below
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

The unit's `WorkingDirectory` must be the checkout; edit the copied unit if it is anywhere other than `/opt/crsw-data-promoter`. The timer fires within a minute of `enable --now`, then every five minutes, with a lock so runs never overlap, and it survives reboots; `systemctl list-timers --all --no-pager | grep crsw` shows the next and last firing. The first run is the long one: `embed_model_resolved`, `embed_model_changed`, `embeddings_written` with every dataset, then a `passages_written` line per dataset (about ten seconds for six datasets); later runs are quiet, a `start` and a `finish` line each. `journalctl -f` follows the log until Ctrl-C.

Pasting this block on the web VM by mistake (seen 15 October 2026) installs the units there too, where the service fails at once with `status=200/CHDIR` every five minutes and never runs the promoter: harmless, since the checkout and the key are absent, but noisy. Undo it there with `sudo systemctl disable --now crsw-promoter.timer`, `sudo rm /etc/systemd/system/crsw-promoter.{timer,service}`, `sudo systemctl daemon-reload`, `sudo systemctl reset-failed crsw-promoter.service`; the list-timers line above should then print nothing. Exit code 1 means a deposit was refused and left in staging; read the `checked` line for the reasons. Exit code 2 on a real run means the run's audit object could not be written (section 6): the promotion happened, the journal has the lines, and the bucket needs looking at.

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
- **Scope note or other spelling edited** (`describe`, or the "Describe a term" form; no new vocabulary version): nothing to do. The form and the find pages show it after the next refresh; the next promoter run, quiet or not, re-embeds only the datasets carrying that term and logs `embed_text_changed`.
- **Rotate a key**: edit the env file, restart the service (web) or nothing (promoter picks it up next run). Old key revoked by eResearch.
- **ARC change the embedding model**: nothing to do. `arc:embedvl` is an alias; the vectors are keyed to the backend it resolves to, and every promoter run (a quiet one too) asks. The run that first sees a new backend logs `embed_model_changed` with the old and new values, re-embeds every dataset (`embeddings_written` with `embedded` ≈ all rows) and rebuilds every passages file in place, so it is a long run; until it finishes, the find page says "Ranking by meaning is off until the index is rebuilt for the current embedding model" and the search page "These passages were made with a different embedding model", and word search keeps working. Nothing is re-read from researchers' files through the web, only from the store. The first run after upgrading to this version does the same once, because the rows written before carried the bare alias (and, from v0.11, no `text_sha`). The other embedding alias on the platform is `arc:embed` (text only, smaller); switching to it is the same event. If ARC ever offer versioned aliases, pin to one and this check becomes belt and braces.
- **"Ranking by meaning is off" stays after an upgrade**: nothing needs kicking off; the timer's quiet runs rewrite the embeddings by themselves, but only when the internal VM has `PROMOTER_LLM_BASE_URL`, `PROMOTER_LLM_API_KEY` and `PROMOTER_LLM_EMBED_MODEL` set *and* can reach the platform, and the notice clears only when the web VM resolves the same backend (both ask `GET /models` with their own key). On the internal VM: `grep -o '^PROMOTER_LLM_[A-Z_]*' .env.promoter` (names only, never the values; `BASE_URL` and `API_KEY` must both be there, `EMBED_MODEL` has a default), `systemctl list-timers --all | grep crsw` (the timer must show a next run), `journalctl -u crsw-promoter.service -n 60 --no-pager`, and `$P audit --action embed_model_resolved`, `--action embed_model_unresolved`, `--action embeddings_failed`. No `embed_*` line at all on a quiet run means the LLM settings are blank there, or the file is already current. `embed_model_unresolved` means `GET /models` failed from that VM (the route to `ai.create.kcl.ac.uk`, the key, or the URL); nothing is written until it resolves, and the datasets left without a row are embedded then (`embed_rows_missing`). `embeddings_failed` names the HTTP error. On the web VM, `grep -o '^CRSW_LLM_[A-Z_]*' .env` (`BASE_URL` and `API_KEY` both needed) and `docker compose -f deploy/compose.yaml logs deposit-web | grep -i "models\|fingerprint"`: the web logs nothing when the model resolves, so an empty grep with both settings present is good; a line naming a bare `arc:embedvl` means the web cannot reach the platform, and the notice stays however current the file is. From a laptop on the VPN, `.venv/Scripts/python -m promoter index --embed` with a key in `.env.promoter` writes the file by hand (section 6). Seen 15 October 2026: both VMs were configured and could reach the platform, but the timer and service units had never been installed on the internal VM (`Unit crsw-promoter.timer could not be found`), so the promoter had only ever run by hand, as dry runs, which never write embeddings; installing the units (section 3) fixed it on the first firing.
- **Reading the promoter's logs later**: there are two copies and neither is a single text file. The VM's journal is systemd's binary log under `/var/log/journal/`, read only through `journalctl` and trimmed by size over time: `journalctl -u crsw-promoter.service --since "3 hours ago" --no-pager` for everything the timer logged, `journalctl -u crsw-promoter.service --no-pager | grep -v '"action": "start"\|"action": "finish"\|"action": "audit_written"\|systemd\[1\]'` to drop the quiet runs and systemd's own lines, `systemctl list-timers --all --no-pager | grep crsw` to see that the timer is still scheduled. The durable copy is the audit trail in the bucket (section 6), one small file per run under `audit/promoter/YYYY/MM/DD/`, which `$P audit --since 2026-10-15 --env-file /dev/null` (or `--action embeddings_written`, `--dataset`, `--user`) stitches together from any machine holding the promoter key; prefer it when the question is what changed rather than whether the timer ticked. For a plain file to keep or send: `journalctl -u crsw-promoter.service --no-pager > promoter-journal.txt`, or `$P audit --since 2026-10-01 --json --env-file /dev/null > promoter-audit.jsonl`.
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
time the run started (so a day's runs are a folder of small files, not
one file; `promoter audit` reads them back together). The bucket is versioned and the promoter never
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
$P index --env-file /dev/null                          # rebuild the index by hand (see below)
$P index --embed --env-file /dev/null                  # ... and the embeddings behind meaning-based search
$P passages [--dataset ID | --strand rsN] --env-file /dev/null   # passages of every text-bearing file (PROMOTER_PASSAGES=1)
```

**The index.** After any real run that promoted or rewrote a record, the
promoter rewrites `index/datasets.jsonl` and `index/datasets.parquet`
(`PROMOTER_INDEX_PREFIX`; blank disables): one row per dataset record in
place with its identifier, UUID, path parts, domain, abstract, subjects,
coverage, version, depositor(s), dates, file count and bytes, schema and
vocabulary versions, the "derived from" references (and the parent
identifiers as a list, for "what was derived from X"), the provenance
tools, and an `origin` (the first tool named, else `deposit`). The
records stay the truth; the index is a cache that `$P index` rebuilds
in full at any time. Idle runs do not touch it, so the versioned bucket
does not fill with copies. It is what the planned search page, the
upstream-dataset picker and cdisaw-parquet read. From a laptop with the
promoter key, DuckDB reads it straight from the bucket:

```
CREATE SECRET ceph (TYPE S3, KEY_ID '...', SECRET '...', ENDPOINT 'rgw.ceph.er.kcl.ac.uk', URL_STYLE 'path');
SELECT identifier, dataset, depositor, files, bytes FROM read_parquet('s3://crsw/index/datasets.parquet');
```

**The embeddings.** With `PROMOTER_LLM_BASE_URL`, `PROMOTER_LLM_API_KEY`
and `PROMOTER_LLM_EMBED_MODEL` set, the same runs also refresh
`index/embeddings.parquet`: one vector per dataset, made by the KCL LLM
platform from the record's title, abstract, subject terms and source
note, never from a file. Only records of the sensitivities in
`PROMOTER_LLM_SENSITIVITIES` (green by default) are sent; the others get
a row with no vector. Subject terms go in as their label and scope note
from the vocabulary of the day ("Forced labour: work exacted under the
menace of a penalty"), never as the bare slug. The work is incremental
(a row is kept when its identifier, modified stamp, model, vector length
and the text it was made from, `text_sha`, are unchanged), so a scope
note or label edited in the vocabulary re-embeds the rows carrying that
term on the next run, a quiet one included (`embed_text_changed`, with
the count); the vectors are cut to `PROMOTER_LLM_EMBED_DIMS` numbers and
stored at half precision, and a failure is logged as `embeddings_failed`
without changing the run's exit code. `$P index --embed` rebuilds by hand. On the
laptop, before the internal VM can reach the platform, that command with
the key in `.env.promoter` does the same job. "Model" here is not the
alias in the configuration but what the platform says is behind it
(`alias|backend|quantisation|registered`, from `GET /models`), logged on
every embedding step as `embed_model_resolved`; see "ARC change the
embedding model" in section 4. When `GET /models` fails or names no
backend, the run logs `embed_model_unresolved` and writes nothing
(vectors keyed to the bare alias would only be replaced wholesale by the
next run that resolves); a dataset promoted meanwhile has no row in the
file, and the next quiet run that resolves embeds it, logging
`embed_rows_missing` with the count. By hand, `$P index --embed` prints
the same line and exits 1.

**Passages (searching inside documents; `finding-and-reuse.md` §7).** With
`PROMOTER_PASSAGES=1` as well, a promotion also reads each promoted
dataset's text-bearing files (plain text, Markdown, the text layer of
PDFs, Word; never tables or images), splits them into passages of about
350 words, embeds those and writes `index/passages/<identifier>.parquet`
(one row per passage: file, page, position, the text, the vector). Only
datasets of the sensitivities in `PROMOTER_LLM_SENSITIVITIES` are read,
never one under `PROMOTER_PASSAGES_EXCLUDE`, a file over
`PROMOTER_PASSAGES_MAX_FILE_BYTES` is skipped and the walk stops at
`PROMOTER_PASSAGES_MAX_PER_DATASET` (from v0.9.2 a file the cap cut
short is marked and read to the end once the cap is raised, reusing the
vectors it already had; a file cut short by an earlier version is not
marked, so delete its dataset's passages file and run `$P passages
--dataset` to finish it); the log line per dataset
(`passages_written`) counts each of those, and `files_no_text` is the
number of PDFs with no text layer, which is the size of any future OCR
job. Incremental by each file's checksum, so a re-deposit re-reads only
what changed. `passages_failed` is logged without changing the exit
code. A dataset that is excluded, or whose sensitivity is taken off the
list, after its passages were built has its passages file deleted the
next time it is promoted or `$P passages` runs (`passages_removed`), so
the VM's copy drops it on its next sync. `$P passages` also removes any
passages file whose dataset record is no longer in place. Narrowing
`PROMOTER_LLM_SENSITIVITIES` takes effect on the next `$P passages` run. `$P passages` rebuilds for every
record in place, or one dataset or strand; the first run over a backlog of long documents is the one
slow job, and it resumes where it stopped because each dataset's file is
written as it finishes.

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
- *What was dataset X derived from, and did the promoter find it?*
  `audit --action resolved_reference --dataset <name>`: the parent's
  identifier, UUID and version as filled in from the store (r8 §4);
  `audit --action reference_unresolved` lists references whose parent
  was not in the store, left as typed and promoted with a warning.
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

## 7. Finding and downloading (the read role)

Planned in `finding-and-reuse.md`; built in stages from 25 September. The
service gains a second, read-only S3 client over `CRSW_READ_S3_ACCESS_KEY`
/ `CRSW_READ_S3_SECRET_KEY` and reads the promoter's index
(`index/datasets.jsonl`, section 6). With both keys blank every route below
is 404 and the form shows no "Find data" link, so the deployed service is
unchanged until the key exists.

| Route | What |
|---|---|
| `/datasets` | search box and filters (strand, state, sensitivity, subject) over the index |
| `/datasets.json?q=…` | the same rows as JSON; feeds the form's "Find a dataset in the store" picker under the derived-from box and the project and dataset name pickers |
| `/deposits/lookup?strand&project&sensitivity&state&dataset` | what a deposit would land on: the existing dataset (summary, the record's values for prefill, its file list), a same-named dataset elsewhere in the project, near-matches, and deposits to it still waiting in staging. The form calls it as the name is typed; `POST /deposits` to an existing or waiting dataset then needs `confirm_existing`, and a version lower than the record's is refused |
| `/datasets/<identifier>` | the record's description, derived-from links both ways, provenance, files |
| `/datasets/<identifier>/record` | the record as stored |
| `/datasets/<identifier>/files/<path>` | one file, streamed from the store through the service |

Downloads pass through the VM one 8 MiB chunk at a time and never touch
its disk; a `Range` request is passed to the store, so a dropped
download resumes. Only files the dataset record names are served, so
the route cannot fetch an arbitrary key, and every download is one line
in the container log (user, dataset, file, bytes, range). That log is
the only download trail until eResearch central logging exists. The
sidecar allows four concurrent downloads per proxy address and never
spools a slow client's response to disk.

The read client is wrapped so it can only get, head and list; a bug cannot
write or delete with the read key however broad it is. This is what makes
a developer's personal key safe to use **on a laptop** while the scoped key
is awaited. Amber data is listed and described for everyone signed in;
whether its files are served is `CRSW_AMBER_ACCESS` (`off` until the
Centre decides; `groups` uses the proxy's groups header and
`CRSW_AMBER_GROUP_TEMPLATE`; `all` for the KCL-only phase).

To ask eResearch for, in one go: a read-scoped key for the web VM over
`index/` and the four strand prefixes (never `staging/`); the groups header
the proxy can forward; whether the proxy buffers or time-limits large
responses; HTTPS from both VMs to `ai.create.kcl.ac.uk`.

**Asking in plain words.** With `CRSW_LLM_BASE_URL` and `CRSW_LLM_API_KEY`
set (and the read role on), the find page gains "Ask in your own words".
A question goes through three steps, each of which can fail or be switched
off on its own without breaking the page:

| Step | Model | Switch | What is sent |
|---|---|---|---|
| filter | `CRSW_LLM_CHAT_MODEL` (`arc:lite`) | blank the name | the question, the vocabulary lists |
| meaning | `CRSW_LLM_EMBED_MODEL` (`arc:embed`) | blank the name | the question, prefixed with `CRSW_LLM_QUERY_INSTRUCTION` (blank = no prefix); needs `index/embeddings.parquet` |
| rerank | `CRSW_LLM_RERANK_MODEL` (`arc:rerankvl`) | blank the name | the question and the title plus abstract of up to twenty candidates |

Only abstracts of the sensitivities in `CRSW_LLM_SENSITIVITIES` (green by
default) are sent to the reranker; `CRSW_LLM_EMBED_DIMS` must match the
promoter's. To change it, set both, restart the web service (its passages
copy empties itself and refills at the new size) and run `$P passages` and
`$P index --embed`; until the promoter has rewritten them, the search page
reports the files still at the old size. The page shows "Understood as: strand rs2, subject
armed-conflict, from 1990" and fills the search form with those values so
the researcher can adjust and press Search; a dataset named in the question
always comes first, and a filter that matches nothing relaxes to the closest
datasets with a notice. The log line per question records the user, the
question's length, the steps that ran and the result count, never the
question. Everything above is off when the two variables are blank, and the
find page is then exactly as before.

**What a page costs.** On a warm cache a find page makes no network
call: the index and the embeddings file are held in memory per worker
and re-read every `CRSW_INDEX_REFRESH_SECONDS` (default 60) with
`If-None-Match`, so an unchanged file is a 304 and nothing is parsed; the
vocabulary is re-fetched every `CRSW_VOCAB_REFRESH_SECONDS` on a
background thread, so no request waits on GitHub; the record page and
the record route fetch the record once; the sidecar compresses pages and
JSON and keeps its connections to the app open. The image runs two
uvicorn workers, each with its own caches, so every refresh happens
twice. Known limit: the two workers share one DuckDB file for passages,
and only one can hold it, so the second worker keeps its copy in memory
and rebuilds it from the bucket at each start (a GET per passages file);
search still works in both. If a page is slow for a researcher and
`curl -s -o /dev/null -w '%{time_total}' http://127.0.0.1:8080/datasets`
on the VM is quick, the time is in the KCL proxy.

**How passage search grows.** A search on the live corpus takes about
four seconds (15 October 2026, six datasets, about a hundred passages),
and nearly all of it is the two platform calls: embedding the question
and reranking the top `SEARCH_TOP` (20) passages. Neither grows with the
corpus. The question is embedded with `CRSW_LLM_QUERY_INSTRUCTION` in
front of it and the passages were embedded without one (the asymmetry
Qwen3-Embedding is trained on; it lowers the wrong answers more than the
right one), so turning the instruction on, off or rewording it is a
restart of `deposit-web`, never a re-embed. What grows is the DuckDB scan that scores every passage
(`array_cosine_similarity` over `FLOAT[1024]`) and the word checks: linear
in the number of passages, microseconds today, tens of milliseconds at
10,000 passages (about 200 datasets at 50 each), a few hundred
milliseconds at 100,000, several seconds at a million. Memory grows first
and it grows **on the web VM**, in the `deposit-web` container: each of
the two uvicorn workers holds the passages table (about 4 KB per passage
for the vector plus the text; one worker in the DuckDB file on the
`passages` volume, the other in memory) and the dataset embeddings (one
vector per dataset, negligible). Reckon 0.5 GB per worker, 1 GB for the
container, at 100,000 passages; the internal VM holds nothing between
runs, and the promoter only streams the files it writes. Watch it with
`docker stats --no-stream` on the web VM and the passages count on the
search page; resize the VM (section 1 gives the minimum) when the
container passes half the RAM, and move to the next rung when the scan
alone, not the platform calls, passes half a second or the table passes
about 100,000 rows. The rungs, cheapest first: cut `PROMOTER_LLM_EMBED_DIMS`
and `CRSW_LLM_EMBED_DIMS` together (1024 today, from a model that is
already truncated: 512 halves memory and scan time, the next promoter
run re-embeds everything because the length changed, and the loss of
ranking quality should be small but has not been measured here, so try
it on a laptop against a few known questions first); an approximate
nearest-neighbour index (HNSW) beside the table, rebuilt at each sync,
through a small in-process library rather than DuckDB's vector-search
extension, since the service deliberately loads no DuckDB extensions; a
dedicated vector store, which is a bigger operational step than this
corpus should ever need.

The subject dropdown on the find page lists the whole vocabulary of the
moment, grouped by facet and indented under broader terms, a term no
dataset carries yet included; the count line names only the narrower
terms the results actually carry ("3 datasets with subject
armed-conflict (and 1 narrower term)"), and `subject_filter.matched` in
`/datasets.json` still lists every term the filter searched.

**Searching inside documents.** With `CRSW_PASSAGES=1` as well (and the
promoter writing passages, section 6), the service keeps a copy of
`index/passages/` in one DuckDB file on the `passages` volume
(`CRSW_PASSAGES_PATH`, default `/data/passages.duckdb`) and adds
"Search inside content" to the navigation: `/search?q=…` embeds the
words, finds the closest passages, keeps only those whose file the
download rule would serve this person, reranks the top twenty and shows
dataset, file, page and passage. The search is hybrid: passages that
contain the words as typed come first (green mark on the sentence),
then passages containing every telling word, then the rest by meaning,
where the sentences of the passages shown go to the embedding model in
one more call and each passage's closest sentence is marked (yellow).
Each result says which of the three it is. A passage under six words is
shown only on a words match, since a vector cannot tell "test data"
from anything. The question's own words are bold wherever they appear.
An "Order" switch puts everything by meaning alone instead, in order of
similarity with no reranker. A "Why this?"
link on each result asks the chat model, on the click and not before,
for one sentence on what connects the passage to the question; the page
calls it "the model's reading", since a vector match has no reasoning
to show, and it is offered only for passages of the sensitivities in
`CRSW_LLM_SENSITIVITIES`. Results found by meaning alone are folded under
"N more passages, close in meaning only" whenever a words match exists, or
when they fall more than 0.05 below the best of them: on a corpus of
similar documents every passage is nearly equally close to any question,
and the fold keeps those near-ties out of the way without dropping them.
The copy refreshes on the index cycle, fetching
only files whose ETag changed; delete the volume (or the file) and it is
rebuilt from the bucket on the next request. Sizes: about 3 KB per
passage on the volume; a million passages is roughly 3 GB, so ask for a
volume sized to the document count. The log line per search records the
user, the length of the words, the steps that ran and the hit count,
never the words. With `CRSW_AMBER_ACCESS=off` no amber passage is even
queried; `CRSW_LLM_SENSITIVITIES` says whose passages may be sent to the
reranker. Everything is off with `CRSW_PASSAGES` blank or `0`.
