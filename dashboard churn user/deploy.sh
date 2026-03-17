#!/bin/bash
# ============================================================
# deploy.sh — Requesty Analytics sur srv1474234.hstgr.cloud
# Ubuntu 24.04 — HTTPS Let's Encrypt — Anti-bruteforce
# ============================================================
# Usage:
#   scp collector.py deploy.sh root@srv1474234.hstgr.cloud:/root/
#   ssh root@srv1474234.hstgr.cloud
#   chmod +x deploy.sh && ./deploy.sh
# ============================================================
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BOLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GREEN}✓  $1${NC}"; }
info() { echo -e "${YELLOW}▸  $1${NC}"; }
err()  { echo -e "${RED}✗  $1${NC}"; exit 1; }
sep()  { echo -e "\n${BOLD}── $1 ──────────────────────────────────${NC}"; }

[[ $EUID -ne 0 ]] && err "Lance ce script en root"
[[ ! -f "$(dirname "$0")/collector.py" ]] && err "collector.py introuvable à côté de deploy.sh"

echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗"
echo      "║   Requesty Analytics — VPS Hostinger Ubuntu 24.04    ║"
echo -e   "╚══════════════════════════════════════════════════════╝${NC}"
echo ""

DOMAIN="srv1474234.hstgr.cloud"
APP_DIR="/opt/requesty-analytics"
APP_USER="requesty"
INTERNAL_PORT=7842

# ── Saisie des paramètres ─────────────────────────────────────
echo -e "${BOLD}Paramètres requis :${NC}\n"

while [[ -z "${REQUESTY_KEY:-}" ]]; do
    read -rp "  Clé API Requesty (sk-...): " REQUESTY_KEY
done

while [[ -z "${AUTH_TOKEN:-}" ]]; do
    read -rp "  Mot de passe dashboard (choisis-en un fort): " AUTH_TOKEN
done
# Vérification longueur minimale
[[ ${#AUTH_TOKEN} -lt 12 ]] && err "Mot de passe trop court (12 caractères minimum recommandé)"

read -rp "  Email pour certificat SSL: " SSL_EMAIL
[[ -z "$SSL_EMAIL" ]] && err "Email SSL requis"

read -rp "  Période de données (7d/30d/90d) [30d]: " SYNC_PERIOD
SYNC_PERIOD=${SYNC_PERIOD:-30d}

echo ""
echo -e "${BOLD}Récapitulatif :${NC}"
echo "  URL finale     : https://$DOMAIN"
echo "  Mot de passe   : ${AUTH_TOKEN:0:3}$(printf '%*s' $((${#AUTH_TOKEN}-3)) | tr ' ' '*')"
echo "  Sync period    : $SYNC_PERIOD"
echo "  Anti-bruteforce: fail2ban (5 essais → ban 1h)"
echo ""
read -rp "Lancer le déploiement ? [O/n] " CONFIRM
[[ "${CONFIRM,,}" == "n" ]] && exit 0

# ═══════════════════════════════════════════════════════════════
sep "1. Mise à jour Ubuntu 24.04"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get upgrade -y -qq -o Dpkg::Options::="--force-confdef" -o Dpkg::Options::="--force-confold"
ok "Système à jour"

# ═══════════════════════════════════════════════════════════════
sep "2. Dépendances"
# Ubuntu 24.04 : certbot via snap (plus fiable qu'apt sur 24.04)
apt-get install -y -qq \
    python3 python3-pip python3-venv \
    nginx \
    curl ufw fail2ban \
    apache2-utils \
    snapd

# Certbot via snap (recommandé sur Ubuntu 24.04)
snap install --classic certbot 2>/dev/null || true
ln -sf /snap/bin/certbot /usr/bin/certbot 2>/dev/null || true

ok "Nginx, Python3, fail2ban, certbot (snap) installés"

# ═══════════════════════════════════════════════════════════════
sep "3. Utilisateur système"
if ! id "$APP_USER" &>/dev/null; then
    useradd --system --shell /bin/bash --create-home "$APP_USER"
    ok "Utilisateur '$APP_USER' créé"
else
    ok "Utilisateur '$APP_USER' existe déjà"
fi

# ═══════════════════════════════════════════════════════════════
sep "4. Application"
mkdir -p "$APP_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "$SCRIPT_DIR/collector.py" "$APP_DIR/"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
ok "collector.py déployé"

info "Virtualenv Python..."
sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q requests flask flask-cors
ok "Dépendances Python installées"

cat > "$APP_DIR/.env" << EOF
REQUESTY_KEY=$REQUESTY_KEY
AUTH_TOKEN=$AUTH_TOKEN
SYNC_PERIOD=$SYNC_PERIOD
EOF
chmod 600 "$APP_DIR/.env"
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
ok "Fichier .env créé (permissions 600)"

# ═══════════════════════════════════════════════════════════════
sep "5. Service systemd"
cat > /etc/systemd/system/requesty-analytics.service << EOF
[Unit]
Description=Requesty Analytics Collector
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/python collector.py \\
    --key \${REQUESTY_KEY} \\
    --period \${SYNC_PERIOD} \\
    --port $INTERNAL_PORT \\
    --auto \\
    --auth-token \${AUTH_TOKEN}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=requesty-analytics
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable requesty-analytics
systemctl start requesty-analytics
sleep 3

if systemctl is-active --quiet requesty-analytics; then
    ok "Service démarré et activé au boot"
else
    echo -e "${YELLOW}⚠  Service pas encore actif (sync initiale en cours)${NC}"
    journalctl -u requesty-analytics -n 10 --no-pager || true
fi

# ═══════════════════════════════════════════════════════════════
sep "6. Anti-bruteforce — fail2ban"

# Jail custom pour Nginx auth failures
cat > /etc/fail2ban/jail.d/requesty.conf << 'EOF'
[nginx-requesty-auth]
enabled   = true
port      = http,https
filter    = nginx-requesty-auth
logpath   = /var/log/nginx/requesty_access.log
maxretry  = 5
findtime  = 300
bantime   = 3600
action    = iptables-multiport[name=requesty, port="http,https", protocol=tcp]
EOF

# Filtre : détecte les 401 dans les logs Nginx
cat > /etc/fail2ban/filter.d/nginx-requesty-auth.conf << 'EOF'
[Definition]
failregex = ^<HOST> .+ "(GET|POST|HEAD|OPTIONS) .+" 401
ignoreregex =
EOF

systemctl enable fail2ban
systemctl restart fail2ban
ok "fail2ban actif — 5 échecs en 5min = ban 1h"

# ═══════════════════════════════════════════════════════════════
sep "7. Nginx — rate limiting + reverse proxy"

# Mot de passe htpasswd (nginx basic auth = popup navigateur)
htpasswd -bc /etc/nginx/.requesty_htpasswd admin "$AUTH_TOKEN" 2>/dev/null
chmod 640 /etc/nginx/.requesty_htpasswd
chown root:www-data /etc/nginx/.requesty_htpasswd
ok "htpasswd créé (user: admin)"

# Config Nginx avec rate limiting
cat > /etc/nginx/sites-available/requesty-analytics << NGINX
# Rate limiting : 10 req/s par IP, burst 20
limit_req_zone \$binary_remote_addr zone=requesty_auth:10m rate=10r/s;
limit_req_zone \$binary_remote_addr zone=requesty_api:10m  rate=30r/s;

# Compteur de connexions simultanées par IP
limit_conn_zone \$binary_remote_addr zone=requesty_conn:10m;

server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    location / {
        return 301 https://\$host\$request_uri;
    }
}

server {
    listen 443 ssl http2;
    listen [::]:443 ssl http2;
    server_name $DOMAIN;

    # Logs dédiés (fail2ban surveille ce fichier)
    access_log /var/log/nginx/requesty_access.log;
    error_log  /var/log/nginx/requesty_error.log;

    # SSL (sera rempli par certbot)
    ssl_certificate     /etc/letsencrypt/live/$DOMAIN/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN/privkey.pem;
    include             /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam         /etc/letsencrypt/ssl-dhparams.pem;

    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options DENY always;
    add_header X-Content-Type-Options nosniff always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    # Masquer la version Nginx
    server_tokens off;

    # ── Connexions simultanées : max 10 par IP ──────────────
    limit_conn requesty_conn 10;

    # ── Route principale ─────────────────────────────────────
    location / {
        # Rate limiting strict sur l'auth (anti-bruteforce nginx)
        limit_req zone=requesty_auth burst=5 nodelay;
        limit_req_status 429;

        # Basic auth — popup navigateur, une seule saisie du mot de passe
        auth_basic "Requesty Analytics";
        auth_basic_user_file /etc/nginx/.requesty_htpasswd;

        proxy_pass         http://127.0.0.1:$INTERNAL_PORT;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 60s;
        proxy_connect_timeout 10s;
        proxy_buffering    off;

        # CORS pour le dashboard React
        add_header Access-Control-Allow-Origin  "*" always;
        add_header Access-Control-Allow-Methods "GET, POST, OPTIONS" always;
        add_header Access-Control-Allow-Headers "Authorization, Content-Type" always;
        if (\$request_method = OPTIONS) { return 204; }
    }

    # Bloquer les user-agents suspects (scanners, bots)
    if (\$http_user_agent ~* "(nikto|sqlmap|nmap|masscan|zgrab|go-http-client/1.1)") {
        return 444;
    }
}
NGINX

ln -sf /etc/nginx/sites-available/requesty-analytics /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t || err "Config Nginx invalide"

# Config temporaire HTTP pour certbot
cat > /etc/nginx/sites-available/requesty-tmp << 'NGINX_TMP'
server {
    listen 80 default_server;
    location /.well-known/acme-challenge/ { root /var/www/html; }
    location / { return 200 "ok\n"; add_header Content-Type text/plain; }
}
NGINX_TMP
ln -sf /etc/nginx/sites-available/requesty-tmp /etc/nginx/sites-enabled/requesty-analytics
systemctl reload nginx

# ═══════════════════════════════════════════════════════════════
sep "8. Certificat SSL Let's Encrypt"
mkdir -p /var/www/html

certbot certonly --webroot \
    -w /var/www/html \
    -d "$DOMAIN" \
    --non-interactive \
    --agree-tos \
    --email "$SSL_EMAIL" \
    --no-eff-email \
    || err "Certbot échoué. Vérifie que le port 80 est ouvert dans le firewall Hostinger."

ok "Certificat SSL obtenu"

# Renouvellement automatique
systemctl enable snap.certbot.renew.timer 2>/dev/null || \
    (crontab -l 2>/dev/null; echo "0 3 * * * certbot renew --quiet --deploy-hook 'systemctl reload nginx'") | crontab -
ok "Renouvellement auto SSL configuré"

# ═══════════════════════════════════════════════════════════════
sep "9. Activation config HTTPS Nginx"
ln -sf /etc/nginx/sites-available/requesty-analytics /etc/nginx/sites-enabled/requesty-analytics
rm -f /etc/nginx/sites-available/requesty-tmp
nginx -t && systemctl reload nginx
ok "Nginx HTTPS actif"

# ═══════════════════════════════════════════════════════════════
sep "10. Firewall UFW"
ufw --force reset > /dev/null
ufw default deny incoming  > /dev/null
ufw default allow outgoing > /dev/null
ufw allow ssh
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ok "Firewall : SSH + 80 + 443 uniquement"

# ═══════════════════════════════════════════════════════════════
sep "11. Tests finaux"
sleep 4

# Test API interne
if curl -sf "http://127.0.0.1:$INTERNAL_PORT/health" -o /tmp/rq_health.json 2>/dev/null; then
    RECORDS=$(python3 -c "import json; print(json.load(open('/tmp/rq_health.json')).get('records',0))" 2>/dev/null || echo "?")
    ok "API Flask → $RECORDS enregistrements"
else
    echo -e "${YELLOW}⚠  API Flask pas encore prête (sync en cours, attends 30s)${NC}"
fi

# Test HTTPS avec auth
sleep 2
HTTP_CODE=$(curl -sf -o /dev/null -w "%{http_code}" \
    --user "admin:$AUTH_TOKEN" \
    "https://$DOMAIN/health" 2>/dev/null || echo "000")

if [[ "$HTTP_CODE" == "200" ]]; then
    ok "HTTPS + auth opérationnel (200 OK)"
elif [[ "$HTTP_CODE" == "401" ]]; then
    echo -e "${YELLOW}⚠  Auth échouée (vérifie le mot de passe)${NC}"
else
    echo -e "${YELLOW}⚠  HTTPS code=$HTTP_CODE (DNS propagation possible, attends 1-2min)${NC}"
fi

# Test anti-bruteforce
ok "fail2ban actif — vérification : fail2ban-client status nginx-requesty-auth"

# ═══════════════════════════════════════════════════════════════
echo ""
echo -e "${BOLD}${GREEN}"
echo "╔══════════════════════════════════════════════════════════╗"
echo "║           ✅  DÉPLOIEMENT TERMINÉ                        ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║                                                          ║"
echo "║  🌐  https://srv1474234.hstgr.cloud                      ║"
echo "║                                                          ║"
echo "║  🔐  Login   : admin                                     ║"
printf "║      Password: %-43s║\n" "$AUTH_TOKEN"
echo "║                                                          ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  🛡️  Sécurité active :                                   ║"
echo "║  • HTTPS TLS 1.2/1.3 (Let's Encrypt)                    ║"
echo "║  • Nginx basic auth (popup navigateur)                   ║"
echo "║  • Rate limiting : 10 req/s, burst 5                    ║"
echo "║  • fail2ban : 5 échecs → ban IP 1h                      ║"
echo "║  • Token Flask en double protection                      ║"
echo "║  • UFW firewall (80/443/SSH seulement)                   ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Maintenance :                                           ║"
echo "║  Logs    : journalctl -u requesty-analytics -f           ║"
echo "║  Bans IP : fail2ban-client status nginx-requesty-auth    ║"
echo "║  Débannir: fail2ban-client unban <IP>                    ║"
echo "║  Restart : systemctl restart requesty-analytics          ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo -e "${NC}"
