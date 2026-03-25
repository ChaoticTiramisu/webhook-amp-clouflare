# AMP -> Cloudflare hostname auto-sync

This service keeps Cloudflare hostname records (`A`/`AAAA`/`CNAME`) synced from AMP instances.

Yes, it can prompt for your Cloudflare API token interactively.

Mode:
- Webhook-driven: AMP calls this service when instance state/name changes.
- Periodic fallback sync: optional safety reconcile every N seconds.

Rules it enforces:
- Only AMP instance names ending in `*.cobyas.xyz` are managed.
- If an AMP instance name changes away from `*.cobyas.xyz`, its managed DNS record is removed.
- Records are tagged with comment `amp-sync:<instance_id>` so only managed records are touched.

## What this solves

If you name an AMP instance as a full domain, for example `survival.cobyas.xyz`, this script creates/updates:
- `survival.cobyas.xyz` as `A` / `AAAA` / `CNAME` depending on instance target

When that instance is renamed to something not ending in `.cobyas.xyz`, the managed DNS record is deleted automatically.

The same removal also happens on webhook events where an instance is deleted or renamed away from that domain.

## 1) Files

- `amp_cf_srv_sync.py`: sync daemon
- `install.sh`: one-command interactive installer (Git clone + setup + start)
- `.env.example`: configuration template
- `systemd/amp-cf-srv-sync.service`: systemd unit example

## 2) One-command interactive install (from GitHub)

Run this in your Debian LXC:

```bash
REPO_URL=https://github.com/<your-user>/<your-repo>.git bash <(curl -fsSL https://raw.githubusercontent.com/<your-user>/<your-repo>/main/install.sh)
```

The wizard will:
- Ask for AMP settings
- Ask for Cloudflare API token and zone ID
- Write `.env`
- Install Python dependencies
- Start via systemd (or foreground mode)

If this repo is private, use `git clone` manually first and run:

```bash
chmod +x install.sh
./install.sh
```

## 3) Cloudflare token permissions

Create an API token with at least:
- Zone:DNS:Edit
- Zone:Zone:Read
- Scope: only your `cobyas.xyz` zone

## 4) Install in Debian LXC (manual)

```bash
sudo mkdir -p /opt/amp-cf-srv-sync
sudo cp amp_cf_srv_sync.py requirements.txt .env.example /opt/amp-cf-srv-sync/
cd /opt/amp-cf-srv-sync
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env
nano .env
```

Set real values in `.env`:
- `AMP_BASE_URL`: your AMP ADS URL (example `http://127.0.0.1:8080`)
- `AMP_API_TOKEN`: AMP API token
- `CLOUDFLARE_API_TOKEN`: Cloudflare API token
- `CLOUDFLARE_ZONE_ID`: zone id for `cobyas.xyz`
- `DEFAULT_TARGET`: target hostname/IP if AMP does not expose one
- `DNS_TTL`: DNS record TTL
- `DNS_PROXIED`: usually `false` for game traffic
- `WEBHOOK_PORT` and `WEBHOOK_PATH`: where AMP will send webhooks
- `WEBHOOK_TOKEN`: optional shared secret to protect webhook endpoint

If your AMP endpoint differs, update:
- `AMP_INSTANCE_LIST_ENDPOINT`

## 5) Configure AMP webhook

In AMP, create a webhook that sends `POST` requests to:

```text
http://<your-lxc-ip>:8787/amp-webhook
```

If you set `WEBHOOK_TOKEN`, send it in one of these headers:
- `X-Webhook-Token: <token>`
- `Authorization: Bearer <token>`

Recommended AMP events:
- Instance created
- Instance renamed/updated
- Instance deleted

Note: exact AMP event names may vary by version/module. This service performs a full reconcile on each webhook, so any event tied to instance changes is sufficient.

## 6) Run manually first

```bash
cd /opt/amp-cf-srv-sync
./.venv/bin/python amp_cf_srv_sync.py
```

Watch logs for:
- Received webhook event
- Created/Updated/Deleted DNS records

Stop with `Ctrl+C` after validation.

## 7) Install as systemd service

```bash
sudo cp systemd/amp-cf-srv-sync.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now amp-cf-srv-sync
sudo systemctl status amp-cf-srv-sync
```

Check logs:

```bash
journalctl -u amp-cf-srv-sync -f
```

## Notes

- The script is webhook-driven and can also run periodic fallback sync via `PERIODIC_SYNC_SECONDS`.
- It only edits records that have comments beginning with `amp-sync:`.
- It creates `A`/`AAAA` for IP targets and `CNAME` for hostname targets.
- If AMP response shape differs, the script may need key-path tweaks for name/port/target fields.
