#!/usr/bin/env bash
set -euo pipefail

PLACEHOLDER_REPO_URL="https://github.com/ChaoticTiramisu/webhook-amp-clouflare.git"
DEFAULT_REPO_URL="${REPO_URL:-$PLACEHOLDER_REPO_URL}"
DEFAULT_INSTALL_DIR="/opt/amp-cf-srv-sync"

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
  action="$(prompt_yes_no "Install (Y) or Uninstall (N)?" "Y")"

  if [[ "$action" != "yes" ]]; then
    uninstall
    exit 0
  fi
  ensure_command git
  ensure_command python3

  local install_dir
  install_dir="$(prompt_default "Install directory" "$DEFAULT_INSTALL_DIR")"

  if [[ -z "$install_dir" ]]; then
    install_dir="$DEFAULT_INSTALL_DIR"
  fi

  local repo_url="$DEFAULT_REPO_URL"

  if [[ -d "$install_dir/.git" ]]; then
    echo "Repository exists, pulling latest changes..."
    git -C "$install_dir" pull --ff-only
  else
    echo "Cloning repository..."
    rm -rf "$install_dir"
    git clone "$repo_url" "$install_dir"
  fi

  cd "$install_dir"

  echo "Creating/updating virtual environment..."
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip >/dev/null
  ./.venv/bin/pip install -r requirements.txt

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

  local ignore_instance_names
  ignore_instance_names="$(prompt_default "Ignore instance names CSV (optional)" "")"

  cat > .env <<EOF
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
IGNORE_INSTANCE_NAMES=$ignore_instance_names
EOF

  echo
  echo "Wrote configuration to $install_dir/.env"

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
    exec ./.venv/bin/python amp_cf_srv_sync.py
  fi
}

main "$@"
