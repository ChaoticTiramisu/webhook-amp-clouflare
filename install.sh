#!/usr/bin/env bash
set -euo pipefail

PLACEHOLDER_REPO_URL="https://github.com/ChaoticTiramisu/webhook-amp-clouflare.git"
DEFAULT_REPO_URL="${REPO_URL:-$PLACEHOLDER_REPO_URL}"
DEFAULT_INSTALL_DIR="/opt/amp-cf-srv-sync"

prompt_action() {
  local value
  while true; do
    if ! read -r -p "Choose action: [I]nstall, [U]pdate, [R]emove? [I]: " value < /dev/tty; then
      echo "Input canceled or unavailable." >&2
      exit 1
    fi
    value="$(echo "$value" | tr '[:upper:]' '[:lower:]')"

    if [[ -z "$value" || "$value" == "i" || "$value" == "install" ]]; then
      echo "install"
      return
    fi

    if [[ "$value" == "u" || "$value" == "update" ]]; then
      echo "update"
      return
    fi

    if [[ "$value" == "r" || "$value" == "remove" || "$value" == "uninstall" || "$value" == "n" || "$value" == "no" ]]; then
      echo "remove"
      return
    fi

    echo "Please choose I, U, or R."
  done
}

prompt_default() {
  local prompt="$1"
  local default="$2"
  local value
  if ! read -r -p "$prompt [$default]: " value < /dev/tty; then
    value=""
  fi
  if [[ -z "$value" ]]; then
    echo "$default"
  else
    echo "$value"
  fi
}

prompt_required() {
  local prompt="$1"
  local value
  while true; do
    if ! read -r -p "$prompt: " value < /dev/tty; then
      echo "Input canceled or unavailable." >&2
      exit 1
    fi
    if [[ -n "$value" ]]; then
      echo "$value"
      return
    fi
    echo "Value is required." >&2
  done
}

prompt_yes_no() {
  local prompt="$1"
  local default="$2"
  local value
  local normalized_default

  normalized_default="$(echo "$default" | tr '[:upper:]' '[:lower:]')"

  while true; do
    if ! read -r -p "$prompt [$default]: " value < /dev/tty; then
      echo "Input canceled or unavailable." >&2
      exit 1
    fi
    value="$(echo "$value" | tr '[:upper:]' '[:lower:]')"

    if [[ -z "$value" ]]; then
      value="$normalized_default"
    fi

    if [[ "$value" == "y" || "$value" == "yes" ]]; then
      echo "yes"
      return
    fi

    if [[ "$value" == "n" || "$value" == "no" ]]; then
      echo "no"
      return
    fi

    echo "Please answer y or n."
  done
}

ensure_command() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd"
    exit 1
  fi
}

merge_env_from_template() {
  local template_file="$1"
  local env_file="$2"

  if [[ ! -f "$template_file" ]]; then
    echo "Warning: Template not found at $template_file. Keeping existing .env unchanged."
    return
  fi

  if [[ ! -f "$env_file" ]]; then
    cp "$template_file" "$env_file"
    echo "Created $env_file from template."
    return
  fi

  local merged_file
  merged_file="$(mktemp)"

  awk '
    NR == FNR {
      line = $0
      sub(/^[[:space:]]+/, "", line)
      if (line ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
        key = line
        sub(/=.*/, "", key)
        val = substr(line, index(line, "=") + 1)
        old[key] = val
        old_order[++old_count] = key
      }
      next
    }

    {
      line = $0
      trimmed = line
      sub(/^[[:space:]]+/, "", trimmed)

      if (trimmed ~ /^[A-Za-z_][A-Za-z0-9_]*=/) {
        key = trimmed
        sub(/=.*/, "", key)
        seen[key] = 1
        if (key in old) {
          print key "=" old[key]
          used_old[key] = 1
        } else {
          print line
        }
      } else {
        print line
      }
    }

    END {
      wrote_legacy_header = 0
      for (i = 1; i <= old_count; i++) {
        key = old_order[i]
        if (!(key in seen) && (key in old)) {
          if (!wrote_legacy_header) {
            print ""
            print "# Preserved legacy settings"
            wrote_legacy_header = 1
          }
          print key "=" old[key]
        }
      }
    }
  ' "$env_file" "$template_file" > "$merged_file"

  mv "$merged_file" "$env_file"
  echo "Migrated $env_file using $template_file (values preserved)."
}

sync_repo() {
  local install_dir="$1"
  local repo_url="$2"

  if [[ -d "$install_dir/.git" ]]; then
    echo "Repository exists, pulling latest changes..."
    git -C "$install_dir" pull --ff-only
  else
    echo "Cloning repository..."
    rm -rf "$install_dir"
    git clone "$repo_url" "$install_dir"
  fi
}

setup_venv() {
  local install_dir="$1"
  cd "$install_dir"
  echo "Creating/updating virtual environment..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt
}

write_env_interactive() {
  local install_dir="$1"

  echo
  echo "Now configure AMP and Cloudflare settings"

  local amp_base_url
  amp_base_url="$(prompt_default "AMP base URL" "http://127.0.0.1:8080")"

  local amp_username
  amp_username="$(prompt_required "AMP username")"

  local amp_password
  amp_password="$(prompt_required "AMP password")"

  local periodic_sync_seconds
  periodic_sync_seconds="$(prompt_default "Periodic sync seconds (0 disables)" "10")"

  local cloudflare_api_token
  cloudflare_api_token="$(prompt_required "Cloudflare API token")"

  local cloudflare_zone_id
  cloudflare_zone_id="$(prompt_required "Cloudflare zone ID (for cobyas.xyz)")"

  local allowed_domain
  allowed_domain="$(prompt_default "Allowed domain" "cobyas.xyz")"

  local dns_ttl
  dns_ttl="$(prompt_default "DNS TTL" "60")"

  local dns_proxied
  dns_proxied="$(prompt_default "DNS proxied (true/false)" "false")"

  local default_target
  default_target="$(prompt_default "Default target host/IP (optional)" "")"

  local public_ip_source_record
  public_ip_source_record="$(prompt_default "Public IP source record (optional, e.g. home.cobyas.xyz)" "")"

  local prefer_public_ip_source
  prefer_public_ip_source="$(prompt_default "Prefer public IP source record (true/false)" "true")"

  local upnp_enabled
  upnp_enabled="$(prompt_default "Enable UPnP auto-port-forwarding (true/false)" "false")"

  local upnp_protocols
  upnp_protocols="$(prompt_default "UPnP protocols (tcp, udp, or tcp,udp)" "tcp")"

  local upnp_internal_client
  upnp_internal_client="$(prompt_default "UPnP internal client IP (optional)" "")"

  local upnp_lease_seconds
  upnp_lease_seconds="$(prompt_default "UPnP lease seconds (0 = permanent)" "0")"

  local ignore_instance_names
  ignore_instance_names="$(prompt_default "Ignore instance names CSV (optional)" "")"

  cat > "$install_dir/.env" <<EOF
AMP_BASE_URL=$amp_base_url
AMP_USERNAME=$amp_username
AMP_PASSWORD=$amp_password
PERIODIC_SYNC_SECONDS=$periodic_sync_seconds
CLOUDFLARE_API_TOKEN=$cloudflare_api_token
CLOUDFLARE_ZONE_ID=$cloudflare_zone_id
ALLOWED_DOMAIN=$allowed_domain
DNS_TTL=$dns_ttl
DNS_PROXIED=$dns_proxied
DEFAULT_TARGET=$default_target
PUBLIC_IP_SOURCE_RECORD=$public_ip_source_record
PREFER_PUBLIC_IP_SOURCE=$prefer_public_ip_source
UPNP_ENABLED=$upnp_enabled
UPNP_PROTOCOLS=$upnp_protocols
UPNP_INTERNAL_CLIENT=$upnp_internal_client
UPNP_DESCRIPTION_PREFIX=amp-sync-upnp:
UPNP_LEASE_SECONDS=$upnp_lease_seconds
IGNORE_INSTANCE_NAMES=$ignore_instance_names
EOF

  echo
  echo "Wrote configuration to $install_dir/.env"
}

restart_service_if_present() {
  local systemctl_cmd="systemctl"
  if command -v sudo >/dev/null 2>&1; then
    systemctl_cmd="sudo systemctl"
  fi

  if systemctl list-unit-files 2>/dev/null | grep -q '^amp-cf-srv-sync\.service'; then
    echo "Restarting systemd service..."
    $systemctl_cmd daemon-reload
    $systemctl_cmd restart amp-cf-srv-sync || true
    $systemctl_cmd status --no-pager amp-cf-srv-sync || true
  fi
}

install_or_update() {
  local mode="$1"

  ensure_command git
  ensure_command python3

  local install_dir
  install_dir="$(prompt_default "Install directory" "$DEFAULT_INSTALL_DIR")"

  if [[ -z "$install_dir" ]]; then
    install_dir="$DEFAULT_INSTALL_DIR"
  fi

  local repo_url="$DEFAULT_REPO_URL"

  sync_repo "$install_dir" "$repo_url"
  setup_venv "$install_dir"

  if [[ "$mode" == "update" ]]; then
    merge_env_from_template "$install_dir/.env.example" "$install_dir/.env"
    restart_service_if_present
    echo
    echo "✓ Update complete"
    return
  fi

  write_env_interactive "$install_dir"

  local systemd_choice
  systemd_choice="$(prompt_yes_no "Install as systemd service" "Y")"

  if [[ "$systemd_choice" == "yes" ]]; then
    ensure_command sudo
    local run_user
    run_user="$(prompt_default "Service user" "root")"

    sudo tee /etc/systemd/system/amp-cf-srv-sync.service >/dev/null <<EOF
[Unit]
Description=AMP to Cloudflare DNS sync
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$install_dir
ExecStart=$install_dir/.venv/bin/python $install_dir/amp_cf_srv_sync.py
Restart=always
RestartSec=5
User=$run_user

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable --now amp-cf-srv-sync
    echo "Systemd service started: amp-cf-srv-sync"
    echo "View logs with: journalctl -u amp-cf-srv-sync -f"
  else
    echo
    echo "Starting service in foreground..."
    exec "$install_dir/.venv/bin/python" "$install_dir/amp_cf_srv_sync.py"
  fi
}

uninstall() {
  echo "== AMP -> Cloudflare DNS uninstaller =="
  
  local install_dir
  install_dir="$(prompt_default "Installation directory to remove" "$DEFAULT_INSTALL_DIR")"

  if [[ -z "$install_dir" ]]; then
    install_dir="$DEFAULT_INSTALL_DIR"
  fi

  if [[ ! -d "$install_dir" ]]; then
    echo "Error: Directory not found: $install_dir"
    exit 1
  fi

  local confirm
  confirm="$(prompt_yes_no "Remove $install_dir and all its contents?" "N")"
  
  if [[ "$confirm" != "yes" ]]; then
    echo "Uninstall cancelled."
    exit 0
  fi

  # Stop and disable systemd service if it exists
  if systemctl is-active --quiet amp-cf-srv-sync 2>/dev/null; then
    echo "Stopping systemd service..."
    sudo systemctl stop amp-cf-srv-sync
  fi

  if systemctl is-enabled amp-cf-srv-sync 2>/dev/null; then
    echo "Disabling systemd service..."
    sudo systemctl disable amp-cf-srv-sync
  fi

  if [[ -f /etc/systemd/system/amp-cf-srv-sync.service ]]; then
    echo "Removing systemd service file..."
    sudo rm /etc/systemd/system/amp-cf-srv-sync.service
    sudo systemctl daemon-reload
  fi

  # Remove installation directory
  echo "Removing installation directory: $install_dir"
  sudo rm -rf "$install_dir"

  echo "✓ Uninstall complete"
}

main() {
  echo "== AMP -> Cloudflare DNS installer =="

  local action
  action="$(prompt_action)"

  if [[ "$action" == "remove" ]]; then
    uninstall
    exit 0
  fi

  install_or_update "$action"
}

main "$@"
