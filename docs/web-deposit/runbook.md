# Runbook: web deposit service and promoter

Two VMs on eResearch OpenStack. Either can be rebuilt from a bare image in under an hour with this page. Nothing else runs on either host.

| | Web VM | Internal VM |
|---|---|---|
| Runs | `deposit-web` + nginx sidecar | `promoter`, once every five minutes |
| Reachable from | the KCL reverse proxy only | nothing inbound |
| Key on the host | `.env` with the **staging-only** key | `.env.promoter` with the **real** key |
| Must never hold | `.env.promoter` | (the web service) |

## 0. Before either VM: the KCL reverse proxy

In the proxy's management interface, Proxy mode, target the web VM by name and the service port (`CRSW_HTTP_PORT`, default 8080). Security section:

- Enable web application firewall: on.
- Restrict to KCL network: on for the KCL-only phase; off for the UoN pilot after the eResearch review.
- Capture (hide) errors: on.
- Require authentication: on. Allowed groups: `er_prj_kdl_slavery`.

Note the proxy's load-balancer address ranges; they go into the security group and `CRSW_TRUSTED_PROXY_CIDRS`.

## 1. Host preparation (both VMs)

```
sudo apt-get update && sudo apt-get install -y docker.io docker-compose-v2 git   # or Podman + podman-compose if eResearch prefer
sudo mkdir -p /opt/crsw-deposit && sudo chown $USER /opt/crsw-deposit
git clone https://github.com/kingsdigitallab/crsw-data-ingestion-tool /opt/crsw-deposit
cd /opt/crsw-deposit && git checkout <tag>
```

Security group: web VM allows TCP `CRSW_HTTP_PORT` from the proxy ranges only, plus SSH from the admin range. Internal VM allows SSH from the admin range only.

## 2. Web VM

```
cd /opt/crsw-deposit
cp .env.example .env            # fill in: staging key, CRSW_AUTH_MODE=proxy, CRSW_TRUSTED_PROXY_CIDRS
chmod 600 .env
export CRSW_HTTP_PORT=8080 CRSW_TRUSTED_PROXY_CIDRS=<ranges>   # also read by the nginx sidecar
docker compose -f deploy/compose.yaml up -d --build
curl -s localhost:8080/health
```

First deploy only, to learn the identity header:

1. In `.env` set `CRSW_DEBUG_HEADERS=1` and, temporarily, `CRSW_AUTH_MODE=placeholder`; restart.
2. Through the proxy, signed in, open `https://<proxy-name>/auth/headers`. Note the header carrying your username (and any groups header).
3. Set `CRSW_PROXY_USER_HEADER` (and `CRSW_PROXY_GROUPS_HEADER`, `CRSW_PROXY_USERNAME_PATTERN` if the value is `k1234567@kcl.ac.uk`-shaped), set `CRSW_AUTH_MODE=proxy`, set `CRSW_DEBUG_HEADERS=0`; restart.
4. `https://<proxy-name>/whoami` must show your k-number.

Logs: `docker compose -f deploy/compose.yaml logs -f`. The app logs one line per create, upload, removal and finalise with the username and deposit id.

## 3. Internal VM

```
cd /opt/crsw-deposit
cp .env.promoter.example .env.promoter     # fill in the promoter key; set PROMOTER_AUTHORISED when known
chmod 600 .env.promoter
docker compose -f deploy/compose.yaml --profile internal build promoter
docker compose -f deploy/compose.yaml --profile internal run --rm promoter python -m promoter run --dry-run --env-file /dev/null
sudo cp deploy/promoter.service /etc/systemd/system/crsw-promoter.service
sudo cp deploy/promoter.timer   /etc/systemd/system/crsw-promoter.timer
sudo systemctl daemon-reload && sudo systemctl enable --now crsw-promoter.timer
journalctl -u crsw-promoter.service -f        # one JSON line per action
```

The timer runs every five minutes with a lock, so runs never overlap. Exit code 1 means a deposit was refused and left in staging; read the `checked` line for the reasons.

## 4. Routine operations

- **Upgrade**: `git fetch && git checkout <new tag>`, then `docker compose -f deploy/compose.yaml up -d --build` on the web VM and `--profile internal build promoter` on the internal VM. Both must run the same tag.
- **Roll back**: check out the previous tag and rebuild; images are reproducible from the tag.
- **Rotate a key**: edit the env file, restart the service (web) or nothing (promoter picks it up next run). Old key revoked by eResearch.
- **Refused deposit**: `journalctl` shows the problems. Fix at source (usually ask the researcher to re-deposit) or, for a policy refusal, adjust `PROMOTER_AUTHORISED`. A deposit is never edited in place.
- **Clear staging**: nothing to do; the lifecycle rule expires abandoned deposits and their noncurrent versions.
- **Check the service key's scope** after any policy change: `CRSW_INTEGRATION=1 .venv/Scripts/python -m pytest tests/test_integration_cluster.py` from a laptop on the VPN.
