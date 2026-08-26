#!/usr/bin/env bash
# Idempotent provisioning of the Context Pager relay on an AWS t3.micro (Ubuntu 22.04/24.04).
# Free tier: ~$0/mo. Zero ML. No Docker — bare venv + systemd + Caddy (Q26).
#
# Run from a clone of this repo so the companion files are found:
#   git clone https://github.com/vatsalyd/context_pager.git
#   cd context_pager && sudo PAGER_DUCKDNS_DOMAIN=pager PAGER_DUCKDNS_TOKEN=xxxx bash deploy/setup_relay.sh
#
# Requirements (env): PAGER_DUCKDNS_DOMAIN (subdomain, no .duckdns.org), PAGER_DUCKDNS_TOKEN,
#   optionally PAGER_S3_BUCKET for S3 backups.
# The package is installed from the repo clone (deploy/../..) when present; otherwise from
# $PAGER_PIP_REF, which defaults to PyPI (`context-pager[relay]`) once published.
#
# Re-running the script is safe: everything is guarded to be idempotent.
set -euo pipefail

DOMAIN="${PAGER_DUCKDNS_DOMAIN:?set PAGER_DUCKDNS_DOMAIN (subdomain, no .duckdns.org)}"
DUCKDNS_TOKEN="${PAGER_DUCKDNS_TOKEN:?set PAGER_DUCKDNS_TOKEN}"
PIP_REF="${PAGER_PIP_REF:-context-pager[relay]}"
S3_BUCKET="${PAGER_S3_BUCKET:-}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

FQDN="$DOMAIN.duckdns.org"
APP_USER="pager"
APP_DIR="/etc/context-pager"
VENV_DIR="/opt/context-pager/venv"
BACKUP_DIR="/var/backups/context-pager"

if [[ "$(id -u)" != "0" ]]; then
    echo "run as root (or with sudo)" >&2
    exit 1
fi

log() { echo "==> $*"; }

# ── base packages ─────────────────────────────────────────────
log "installing base packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sqlite3 git curl ca-certificates gnupg apt-transport-https

# ── Caddy (auto-TLS via HTTP-01) ──────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
    log "installing Caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
fi

log "installing Caddyfile"
if [[ -f "$SCRIPT_DIR/Caddyfile" ]]; then
    sed "s/pager\.duckdns\.org/$FQDN/" "$SCRIPT_DIR/Caddyfile" > /etc/caddy/Caddyfile
else
    cat > /etc/caddy/Caddyfile <<EOF
$FQDN {
    encode gzip
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
EOF
fi
chmod 0644 /etc/caddy/Caddyfile

log "installing landing page"
if [[ -f "$SCRIPT_DIR/landing.html" ]]; then
    install -m 0644 "$SCRIPT_DIR/landing.html" /etc/context-pager/landing.html
fi

systemctl enable --now caddy
systemctl reload caddy

# ── app user + directories ────────────────────────────────────
id "$APP_USER" &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR" "$BACKUP_DIR"

# ── venv + package ────────────────────────────────────────────
if [[ ! -x "$VENV_DIR/bin/pager" ]]; then
    log "creating venv at $VENV_DIR"
    install -d -o "$APP_USER" -g "$APP_USER" /opt/context-pager
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --upgrade pip
    if [[ -f "$REPO_DIR/pyproject.toml" ]]; then
        log "installing relay stack from repo clone $REPO_DIR"
        "$VENV_DIR/bin/pip" install "$REPO_DIR[relay]"
    else
        log "installing $PIP_REF (first run pulls the relay stack; may take a minute)"
        "$VENV_DIR/bin/pip" install "$PIP_REF"
    fi
fi

# ── relay env (only written once so admin tweaks persist) ─────
if [[ ! -f "$APP_DIR/.env" ]]; then
    log "writing $APP_DIR/.env"
    cat > "$APP_DIR/.env" <<EOF
PAGER_RELAY_HOST=0.0.0.0
PAGER_RELAY_PORT=8000
PAGER_MCP_PATH=/mcp
PAGER_BRIDGE_PATH=/bridge
PAGER_SQLITE_DB=$APP_DIR/users.db
PAGER_PUBLIC_URL=https://$FQDN
PAGER_API_KEY_PREFIX=pgr_
PAGER_RATE_LIMIT_CALLS_PER_HOUR=100
PAGER_MAX_BRIDGES_PER_KEY=2
PAGER_SIGNUP_PER_IP_PER_DAY=5
LOG_LEVEL=INFO
EOF
    chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
    chmod 600 "$APP_DIR/.env"
fi

# ── systemd unit ──────────────────────────────────────────────
log "installing systemd unit"
if [[ -f "$SCRIPT_DIR/context-pager-relay.service" ]]; then
    install -m 0644 "$SCRIPT_DIR/context-pager-relay.service" /etc/systemd/system/context-pager-relay.service
else
    cat > /etc/systemd/system/context-pager-relay.service <<EOF
[Unit]
Description=Context Pager Relay (thin MCP router + bridge WSS)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV_DIR/bin/pager serve
Restart=always
RestartSec=5
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths=$APP_DIR $BACKUP_DIR

[Install]
WantedBy=multi-user.target
EOF
fi
systemctl daemon-reload
systemctl enable --now context-pager-relay

# ── nightly backup (7-day retention, optional S3) ─────────────
log "installing backup script"
if [[ -f "$SCRIPT_DIR/backup.sh" ]]; then
    install -m 0755 "$SCRIPT_DIR/backup.sh" /usr/local/bin/context-pager-backup
else
    cat > /usr/local/bin/context-pager-backup <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
DB_DIR="${PAGER_DB_DIR:-/etc/context-pager}"
BACKUP_DIR="${PAGER_BACKUP_DIR:-/var/backups/context-pager}"
RETENTION_DAYS="${PAGER_BACKUP_RETENTION_DAYS:-7}"
S3_BUCKET="${PAGER_S3_BUCKET:-}"
AWS_BIN="${PAGER_AWS_BIN:-aws}"
[[ -f /etc/context-pager/backup.env ]] && . /etc/context-pager/backup.env
mkdir -p "$BACKUP_DIR"
for db in users.db; do
    if [[ ! -f "$DB_DIR/$db" ]]; then
        continue
    fi
    sqlite3 "$DB_DIR/$db" ".backup '$BACKUP_DIR/$db.$(date +%F).bak'"
done
find "$BACKUP_DIR" -name '*.bak' -mtime "+$RETENTION_DAYS" -delete
if [[ -n "$S3_BUCKET" ]]; then
    "$AWS_BIN" s3 sync "$BACKUP_DIR" "s3://$S3_BUCKET/context-pager/" --delete --quiet
fi
EOF
    chmod 0755 /usr/local/bin/context-pager-backup
fi
( crontab -l 2>/dev/null | grep -q context-pager-backup ) || \
    ( crontab -l 2>/dev/null; echo "30 2 * * * /usr/local/bin/context-pager-backup" ) | crontab -

if [[ -n "$S3_BUCKET" ]]; then
    if "$VENV_DIR/bin/pip" install -q awscli 2>/dev/null; then
        AWS_BIN="$VENV_DIR/bin/aws"
    else
        apt-get install -y -qq awscli
        AWS_BIN="aws"
    fi
    {
        echo "export PAGER_S3_BUCKET=$S3_BUCKET"
        echo "export PAGER_AWS_BIN=$AWS_BIN"
    } > /etc/context-pager/backup.env
fi

# ── DuckDNS: keep the A record pointed at this host ───────────
log "installing DuckDNS updater"
install -d -m 0755 /etc/duckdns
printf '%s' "$DUCKDNS_TOKEN" > /etc/duckdns/token
chmod 600 /etc/duckdns/token
cat > /usr/local/bin/context-pager-duckdns <<EOF
#!/usr/bin/env bash
curl -fsS "https://www.duckdns.org/update?domains=$DOMAIN&token=$DUCKDNS_TOKEN&ip=" || true
EOF
chmod 0755 /usr/local/bin/context-pager-duckdns
( crontab -l 2>/dev/null | grep -q context-pager-duckdns ) || \
    ( crontab -l 2>/dev/null; echo "*/5 * * * * /usr/local/bin/context-pager-duckdns" ) | crontab -
/usr/local/bin/context-pager-duckdns

log "waiting for the relay to come up"
for i in $(seq 1 30); do
    curl -fsS -o /dev/null "http://127.0.0.1:8000/v1/signup" -X POST && break
    sleep 2
done

cat <<EOF

Context Pager relay is up.
  Public MCP:   https://$FQDN/mcp
  Bridge WSS:   wss://$FQDN/bridge
  Signup:       curl -X POST https://$FQDN/v1/signup
  Service:      systemctl status context-pager-relay
  Backups:      $BACKUP_DIR (nightly, $(echo "${PAGER_BACKUP_RETENTION_DAYS:-7}")-day retention)
EOF
