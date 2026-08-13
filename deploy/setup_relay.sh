#!/usr/bin/env bash
# Idempotent provisioning of the Context Pager relay on an AWS t3.micro (Ubuntu 22.04/24.04).
# Free tier: ~$0/mo. Zero ML. No Docker — bare venv + systemd + Caddy (Q26).
#
# Requirements (env): PAGER_DUCKDNS_DOMAIN (e.g. "pager"), PAGER_DUCKDNS_TOKEN,
#   and optionally PAGER_S3_BUCKET (e.g. "my-bucket") for backups.
# The package spec defaults to PyPI; point PAGER_PIP_REF at a local repo until published:
#   PAGER_PIP_REF="git+https://github.com/vatsalyd/context_pager.git@main#egg=context-pager[relay]"
#
# Re-running the script is safe: everything is guarded to be idempotent.
set -euo pipefail

DOMAIN="${PAGER_DUCKDNS_DOMAIN:?set PAGER_DUCKDNS_DOMAIN (subdomain, no .duckdns.org)}"
DUCKDNS_TOKEN="${PAGER_DUCKDNS_TOKEN:?set PAGER_DUCKDNS_TOKEN}"
PIP_REF="${PAGER_PIP_REF:-context-pager[relay]}"
S3_BUCKET="${PAGER_S3_BUCKET:-}"

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
apt-get install -y -qq python3-venv python3-pip sqlite3 curl ca-certificates gnupg apt-transport-https

# ── Caddy (auto-TLS via HTTP-01) ──────────────────────────────
if ! command -v caddy >/dev/null 2>&1; then
    log "installing Caddy"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get update -qq
    apt-get install -y -qq caddy
fi

log "installing Caddyfile"
if [[ -f "Caddyfile" ]]; then
    install -m 0644 Caddyfile /etc/caddy/Caddyfile
else
    sed "s/pager\.duckdns\.org/$FQDN/" "$(dirname "$0")/Caddyfile" | install -m 0644 /dev/stdin /etc/caddy/Caddyfile
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
    log "installing $PIP_REF (first run pulls the relay stack; may take a minute)"
    "$VENV_DIR/bin/pip" install "$PIP_REF"
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
if [[ -f "context-pager-relay.service" ]]; then
    install -m 0644 context-pager-relay.service /etc/systemd/system/context-pager-relay.service
else
    install -m 0644 "$(dirname "$0")/context-pager-relay.service" /etc/systemd/system/context-pager-relay.service
fi
systemctl daemon-reload
systemctl enable --now context-pager-relay

# ── nightly backup (7-day retention, optional S3) ─────────────
log "installing backup script"
if [[ -f "backup.sh" ]]; then
    install -m 0755 backup.sh /usr/local/bin/context-pager-backup
else
    install -m 0755 "$(dirname "$0")/backup.sh" /usr/local/bin/context-pager-backup
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
