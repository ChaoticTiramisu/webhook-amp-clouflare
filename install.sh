#!/usr/bin/env bash
set -euo pipefail

PLACEHOLDER_REPO_URL="https://github.com/ChaoticTiramisu/webhook-amp-clouflare.git"
DEFAULT_REPO_URL="${REPO_URL:-$PLACEHOLDER_REPO_URL}"
DEFAULT_INSTALL_DIR="/opt/amp-cf-srv-sync"

prompt_default() {
  local prompt="$1"
  local default="$2"
  local value
  read -r -p "$prompt [$default]: " value
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
    read -r -p "$prompt: " value
    if [[ -n "$value" ]]; then
      echo "$value"
      return
    fi
    echo "Value is required."
  done
}

prompt_secret_required() {
  local prompt="$1"
  local value
  while true; do
    read -r -s -p "$prompt: " value
    echo
    if [[ -n "$value" ]]; then
      echo "$value"
      return
    fi
    echo "Value is required."
  done
}

prompt_yes_no() {
  local prompt="$1"
  local default="$2"
  local value
  local normalized_default

  normalized_default="$(echo "$default" | tr '[:upper:]' '[:lower:]')"

  while true; do
    read -r -p "$prompt [$default]: " value
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

main() {
  echo "== AMP -> Cloudflare DNS interactive installer =="
  ensure_command git
  ensure_command python3

  local install_dir
  install_dir="$(prompt_default "Install directory" "$DEFAULT_INSTALL_DIR")"

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
  amp_password="$(prompt_secret_required "AMP password")"

  local periodic_sync_seconds
  periodic_sync_seconds="$(prompt_default "Periodic fallback sync seconds (0 disables)" "300")"

  local webhook_host
  webhook_host="$(prompt_default "Webhook listen host" "0.0.0.0")"

  local webhook_port
  webhook_port="$(prompt_default "Webhook listen port" "8787")"

  local webhook_path
  webhook_path="$(prompt_default "Webhook path" "/amp-webhook")"

  local webhook_token
  read -r -s -p "Webhook token (optional, press enter to skip): " webhook_token
  echo

  local cloudflare_api_token
  cloudflare_api_token="$(prompt_secret_required "Cloudflare API token")"

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

  local ignore_instance_names
  ignore_instance_names="$(prompt_default "Ignore instance names CSV (optional)" "")"

  cat > .env <<EOF
AMP_BASE_URL=$amp_base_url
AMP_USERNAME=$amp_username
AMP_PASSWORD=$amp_password
PERIODIC_SYNC_SECONDS=$periodic_sync_seconds
WEBHOOK_HOST=$webhook_host
WEBHOOK_PORT=$webhook_port
WEBHOOK_PATH=$webhook_path
WEBHOOK_TOKEN=$webhook_token
CLOUDFLARE_API_TOKEN=$cloudflare_api_token
CLOUDFLARE_ZONE_ID=$cloudflare_zone_id
ALLOWED_DOMAIN=$allowed_domain
DNS_TTL=$dns_ttl
DNS_PROXIED=$dns_proxied
DEFAULT_TARGET=$default_target
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
    echo "AMP webhook target URL: http://<LXC-IP>:$webhook_port$webhook_path"
    if [[ -n "$webhook_token" ]]; then
      echo "Send header: X-Webhook-Token: <your-token>"
    fi
    echo "Starting service in foreground..."
    exec ./.venv/bin/python amp_cf_srv_sync.py
  fi

  echo
  echo "AMP webhook target URL: http://<LXC-IP>:$webhook_port$webhook_path"
  if [[ -n "$webhook_token" ]]; then
    echo "Send header: X-Webhook-Token: <your-token>"
  fi
}

main "$@"
