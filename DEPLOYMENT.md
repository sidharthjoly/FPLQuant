# Deploying the backend (Oracle Cloud "Always Free")

The frontend deploys itself (GitHub Pages, see `.github/workflows/pages.yml`),
live at https://fplquant.sidharthjoly.com/ (a custom subdomain CNAMEd to
`sidharthjoly.github.io`; the plain `sidharthjoly.github.io/FPLQuant/` URL
redirects there).

The backend (FastAPI + Redis, via `docker-compose.yml`) is **live** on an
Oracle Cloud "Always Free" VM, fronted by Caddy for HTTPS:

```
Browser (GitHub Pages, HTTPS)
        │
        ▼
https://fplquant.duckdns.org   ── DuckDNS: free, permanent subdomain
        │
        ▼
Caddy (:80/:443 on the VM)     ── automatic Let's Encrypt cert, auto-renewing
        │
        ▼
FastAPI (localhost:8000, in Docker)
```

DigitalOcean's GitHub Student Pack offer expired (2026-07-31) before it was
redeemed, so this uses Oracle's Always Free tier instead — and DuckDNS +
Caddy rather than a purchased domain, since it's genuinely free with no
renewal hassle (unlike some free-DNS providers that require confirming a
link every 30 days) and Let's Encrypt certs are free and auto-renewing
indefinitely. No dependency on any time-limited credit or trial.

This doc is both a record of the current setup and a runbook for recreating
it (e.g. if the VM is ever lost and needs rebuilding from scratch).

## 1. Create the VM

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com) (needs a card for
   identity verification; the Always Free resources below don't charge it).
2. Create a compute instance:
   - **Shape:** `VM.Standard.A1.Flex` (Ampere/ARM) is the most generous
     Always Free shape, but ARM capacity can be unavailable in some regions
     — `VM.Standard.E2.1.Micro` (AMD, x86_64, 1/8 OCPU, 1GB RAM) is the
     reliable fallback and is what's actually running today. The Dockerfile
     builds fine on either architecture.
   - **Image:** Ubuntu (latest LTS).
   - **Networking:** create a new VCN + public subnet (the instance-creation
     wizard's default), with **"Automatically assign public IPv4 address"
     enabled** — it can silently end up unchecked, worth double-checking
     before creating. If the instance ends up with no public IP anyway, add
     an **ephemeral** public IP afterward via the VNIC's IP Addresses page.
   - **SSH keys:** paste an existing public key (`cat ~/.ssh/id_ed25519.pub`)
     rather than leaving "No SSH keys" selected — easy to miss and there's
     no good way to add one after the fact.
3. **Open the firewall — in two separate places, both required:**
   - **Cloud-level:** the subnet's Security List *and* any Network Security
     Group attached to the VNIC (Oracle's "Connect public subnet to
     internet" quick action creates one, e.g. `ig-quick-action-NSG`) each
     need ingress rules for TCP 22, 80, and 443 from `0.0.0.0/0`. Both
     layers enforce independently — traffic needs to clear both.
   - **OS-level:** Ubuntu images on OCI also ship with their own `iptables`
     rules that block anything not explicitly allowed, *independently of
     the cloud console rules above* — this trips up nearly everyone on OCI
     specifically. Check with `sudo iptables -L INPUT -n --line-numbers`;
     if there's a catch-all `REJECT` rule, insert ACCEPT rules for 80/443
     *before* it (`sudo iptables -I INPUT <n> -p tcp --dport 80 -j ACCEPT`,
     same for 443), then `sudo netfilter-persistent save` to persist it
     across reboots.

## 2. First-time server setup

SSH in and install Docker:

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
# log out and back in for the group change to apply
```

Clone the repo and start the stack:

```bash
git clone https://github.com/SidharthJoly/FPLQuant.git
cd FPLQuant
cp .env.example .env
```

Edit `.env` (install an editor first if needed — `sudo apt install -y nano`
— minimized Ubuntu images often don't have one; or just use `sed`) and
uncomment `FPLQUANT_CORS_ALLOWED_ORIGINS`, which already defaults to the
Pages origin:

```bash
sed -i 's/^# *FPLQUANT_CORS_ALLOWED_ORIGINS=/FPLQUANT_CORS_ALLOWED_ORIGINS=/' .env
```

Then:

```bash
docker compose up -d --build
docker compose exec api uv run alembic upgrade head
docker compose exec api uv run fplquant-ingest
curl http://localhost:8000/health   # {"status":"ok"}
```

## 3. HTTPS via DuckDNS + Caddy

1. At [duckdns.org](https://www.duckdns.org), sign in and add a subdomain
   (e.g. `fplquant` → `fplquant.duckdns.org`). **The IP field defaults to
   whatever machine is currently viewing the page — override it to the
   VM's public IP.** Confirm it resolves: `nslookup fplquant.duckdns.org`.
2. Install Caddy on the VM:
   ```bash
   sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
   curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
   sudo apt update && sudo apt install -y caddy
   ```
3. Configure it:
   ```bash
   sudo tee /etc/caddy/Caddyfile > /dev/null <<'EOF'
   fplquant.duckdns.org {
       reverse_proxy localhost:8000
   }
   EOF
   sudo systemctl reload caddy
   ```
   Caddy requests and auto-renews a Let's Encrypt cert for the hostname on
   first request — no separate certbot step. Verify:
   ```bash
   curl https://fplquant.duckdns.org/health   # {"status":"ok"}
   ```

If the VM's IP ever changes (instance recreated, etc.), update the IP on
the DuckDNS subdomain's page — the hostname itself doesn't need to change,
so `frontend/config.js` and any bookmarks stay valid.

**Optional hardening, once Caddy is confirmed working:** port 8000 was
opened directly for testing before Caddy was in place. It can be closed
again in the Security List/NSG/iptables (reversing the ingress rules added
for it) since all real traffic now goes through Caddy on 443 — nothing
outside the VM needs to reach 8000 directly anymore.

## 4. Point the frontend at it

`frontend/config.js`:

```js
export const API_BASE = "https://fplquant.duckdns.org";
```

Commit and push — `.github/workflows/pages.yml` redeploys the Pages site
automatically on any push touching `frontend/`.

## 5. Docker image (GitHub Container Registry)

`.github/workflows/docker-publish.yml` builds the image from the `Dockerfile`
and pushes it to `ghcr.io/sidharthjoly/fplquant` (tags: `latest` and the
short commit SHA) automatically on every push to `main` that touches the
Dockerfile, `src/`, or dependency lockfiles — this runs on GitHub's own CI
runners, not the low-spec VM. It shows up under the repo's "Packages"
sidebar and the GitHub profile's Packages tab.

The package is created **private** by default on its first push. To make it
visible on the profile and pullable without auth from the VM, one manual
step is needed once: package page → Package settings → Change visibility →
Public.

## 6. Redeploying after changes (CD)

`.github/workflows/deploy.yml` SSHes into the VM, pulls the latest git
source (for anything not baked into the image, e.g. `docker-compose.yml`
itself) and the latest published `ghcr.io/sidharthjoly/fplquant:latest`
image, then restarts the stack — manually triggered (`workflow_dispatch`),
not on every push, so a deploy is always a deliberate action. Needs these
repo secrets set first (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `DEPLOY_HOST` | `fplquant.duckdns.org` (or the VM's IP) |
| `DEPLOY_USER` | `ubuntu` |
| `DEPLOY_SSH_KEY` | the private key matching the public key added to the instance |

Since the image is built by GitHub's CI runners rather than rebuilt on the
VM's 1/8-OCPU instance, deploys are faster and don't spike CPU during the
container rebuild. Trigger order matters: push to `main` (which publishes
the image) before running "Deploy backend", or the deploy will just re-pull
whatever was last published.

Then: Actions tab → "Deploy backend" → Run workflow.

## Scheduled data ingest on the server

The GitHub Actions ingest workflows (`.github/workflows/ingest*.yml`) run in
CI and upload the resulting SQLite DB as a build artifact — that's a
separate, CI-only DB snapshot (useful for local dev/testing), and it doesn't
touch the deployed server. **The live server keeps itself fresh via cron**,
running the same CLI commands directly against the running containers:

`scripts/cron_ingest.sh` and `scripts/cron_ingest_injuries.sh` wrap
`docker compose exec -T api uv run fplquant-ingest[-injuries]` (`-T` disables
TTY allocation, needed since cron has no terminal). Set up on the VM:

```bash
cd ~/FPLQuant && git pull   # pick up the scripts if they weren't there at clone time
```

The minimized Ubuntu image doesn't ship `cron` (same story as `nano`
earlier — install it first):

```bash
sudo apt update && sudo apt install -y cron
sudo systemctl enable --now cron
```

`crontab -e` needs an interactive editor, which this image also lacks by
default — set the crontab non-interactively instead:

```bash
crontab -l 2>/dev/null > /tmp/mycron || true
cat >> /tmp/mycron <<'EOF'
0 3 * * *   /home/ubuntu/FPLQuant/scripts/cron_ingest.sh >> /home/ubuntu/ingest.log 2>&1
0 4 * * 0   /home/ubuntu/FPLQuant/scripts/cron_ingest_injuries.sh >> /home/ubuntu/ingest_injuries.log 2>&1
EOF
crontab /tmp/mycron
crontab -l   # confirm both lines are there
```

(Adjust the path if the repo isn't cloned to `/home/ubuntu/FPLQuant`. The
times match the CI ingest workflows' own cadence — daily FPL data, weekly
injury scrape since that's rate-limited scraping over the full player pool.)

If the injury table is empty, the cron is the first thing to check —
`crontab -l` and `systemctl is-active cron` — because a cron that never fires
is indistinguishable from one that fires and finds nothing to do. The
`Injury ingest (server)` workflow (Actions → Run workflow) runs the scrape on
the VM on demand over the same SSH path as the deploy, prints the resulting
row counts, and fails if the table is still empty afterwards. Note the
`ingest_injuries.yml` workflow does *not* do this — it runs on a CI runner
against a throwaway database and uploads an artifact, which never reaches the
live site.

Verify before waiting for the schedule — run a script directly and check
its exit code:

```bash
~/FPLQuant/scripts/cron_ingest.sh; echo "exit code: $?"
```

Confirmed working in production: `systemctl is-active cron` → `active`,
manual run → fetched all 587 players, exit code `0`.

## Oracle Always Free: the idle-reclaim gotcha

Oracle reclaims Always Free compute instances that sit idle (CPU, network,
and — for A1/ARM shapes only — memory all under 20% utilization) for a full
7 days straight. For a low-traffic personal project this is a real risk,
not theoretical.

**Mitigation: `.github/workflows/keepalive.yml`**, a GitHub Actions
scheduled workflow that pings `/health` every 15 minutes. Kept in-repo
rather than a third-party uptime monitor (e.g. UptimeRobot) so there's no
extra account to manage — public repos get unlimited free Actions minutes,
so cost isn't a concern. Failures are non-fatal by design (`|| echo ...`) —
it's a keep-alive ping, not an uptime monitor; a brief outage during a
redeploy shouldn't spam notification emails.
