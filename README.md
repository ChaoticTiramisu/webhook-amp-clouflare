# AMP -> Cloudflare DNS sync

This project keeps Cloudflare DNS records in sync with AMP instances using AMPAPI_Python (`cc-ampapi`).

It runs as a periodic sync service (default every 10 seconds) and does not require webhook configuration.

If you already run a Cloudflare DDNS updater in another LXC, this service can read that DDNS record and use it as the target for game DNS records.

Optional: it can also create and clean UPnP port forwards on your Debian host's gateway for instances under your allowed domain.

## Behavior

- Only instances with names ending in the configured domain are managed.
	- Example: if `ALLOWED_DOMAIN=cobyas.xyz`, instance name `survival.cobyas.xyz` is managed.
- Records are tagged with comments like `amp-sync:<instance_id>`.
- Only records tagged with `amp-sync:` are created, updated, or deleted by this tool.
- Record type is inferred from target:
	- IPv4 -> `A`
	- IPv6 -> `AAAA`
	- hostname -> `CNAME`

## Files

- `amp_cf_srv_sync.py`: main sync service
- `install.sh`: interactive installer/uninstaller
- `.env.example`: configuration template
- `systemd/amp-cf-srv-sync.service`: systemd example unit

## Quick Install (Debian/LXC)

Run:

```bash
bash <(curl -fsSL "https://raw.githubusercontent.com/ChaoticTiramisu/webhook-amp-clouflare/main/install.sh?$(date +%s)")
```

The installer will:

- Clone/update the repo
- Create `.venv`
- Install dependencies
- On install: prompt for AMP and Cloudflare settings
- On update: reuse existing `.env` without prompting
- Write `.env`
- Optionally install/start systemd service

Installer actions:

- `Install`: interactive setup (writes `.env`)
- `Update`: non-interactive code/dependency update, then migrates `.env` against `.env.example`
	- Existing values are preserved
	- New config keys are added with template defaults
	- Legacy keys not present in new template are preserved at the bottom
- `Remove`: uninstall service and delete install directory

## Required Cloudflare Permissions

Create a token with:

- `Zone:DNS:Edit`
- `Zone:Zone:Read`

Scope it to your target zone.

## Configuration

Required values in `.env`:

- `AMP_BASE_URL`
- `AMP_USERNAME`
- `AMP_PASSWORD`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ZONE_ID`

Common optional values:

- `PERIODIC_SYNC_SECONDS` (default `10`, set `0` to disable periodic loop)
- `ALLOWED_DOMAIN` (default `cobyas.xyz`)
- `DNS_TTL` (default `60`)
- `DNS_PROXIED` (default `false`)
- `DEFAULT_TARGET` (used when AMP does not provide a target)
- `PUBLIC_IP_SOURCE_RECORD` (Cloudflare record to read public target from, for example `home.cobyas.xyz`)
- `PREFER_PUBLIC_IP_SOURCE` (default `true`; if `false`, only used when AMP target is private/loopback/missing)
- `UPNP_ENABLED` (default `false`; enable auto port forwarding)
- `UPNP_PROTOCOLS` (`tcp`, `udp`, or `tcp,udp`)
- `UPNP_INTERNAL_CLIENT` (optional fixed LAN IP; otherwise gateway LAN address auto-detected)
- `UPNP_DESCRIPTION_PREFIX` (default `amp-sync-upnp:`)
- `UPNP_LEASE_SECONDS` (default `0`, permanent if supported)
- `IGNORE_INSTANCE_NAMES` (comma-separated names to skip)

## UPnP Notes

- UPnP is only applied to instances that match `*.ALLOWED_DOMAIN`.
- If an instance is renamed away from that domain or removed, managed UPnP mappings are deleted.
- Mappings are tracked by description prefix so unmanaged router mappings are not touched.
- Requires router UPnP enabled and the Python package `miniupnpc`.

## Manual Run

```bash
cd /opt/amp-cf-srv-sync
./.venv/bin/python amp_cf_srv_sync.py
```

## systemd

If installed as a service:

```bash
systemctl status amp-cf-srv-sync
systemctl is-active amp-cf-srv-sync
journalctl -u amp-cf-srv-sync -f
```

## Uninstall

Run installer and choose uninstall:

```bash
bash install.sh
```

Then choose `Install (Y) or Uninstall (N)?` -> `N`.
