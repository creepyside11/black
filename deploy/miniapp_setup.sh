#!/usr/bin/env bash
# RakCRMP Mini App — установка на VPS одной командой.
#
#   sudo bash deploy/miniapp_setup.sh [каталог проекта]
#
# Что делает:
#   1. узнаёт публичный IP и строит домен <ip-с-дефисами>.sslip.io
#      (sslip.io — публичный DNS, любой IP превращается в имя; домен покупать
#      не нужно, а без имени Let's Encrypt сертификат не выдаст);
#   2. ставит Caddy и настраивает автоматический HTTPS;
#   3. сажает панель на 127.0.0.1 — наружу её отдаёт только Caddy;
#   4. прописывает адрес Mini App в .env;
#   5. ставит кнопку «Панель» в меню бота.
#
# Скрипт можно запускать повторно: он ничего не ломает и не дублирует.

set -euo pipefail

ROOT="${1:-/opt/blackrussia}"
ENV_FILE="$ROOT/.env"
PANEL_UNIT="/etc/systemd/system/rakcrmp-panel.service"
PANEL_PORT="${BR_WEB_PORT:-8080}"

say() { printf '\033[1m[mini]\033[0m %s\n' "$*"; }
die() { printf '\033[31m[mini] %s\033[0m\n' "$*" >&2; exit 1; }

[ -d "$ROOT" ] || die "нет каталога проекта: $ROOT"
[ -f "$ROOT/web/server.py" ] || die "в $ROOT нет web/server.py"

# --- 1. публичный IP и домен ------------------------------------------------

IP="$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || true)"
[ -n "$IP" ] || IP="$(curl -fsS --max-time 10 https://ifconfig.me 2>/dev/null || true)"
[ -n "$IP" ] || die "не смог узнать публичный IP"
case "$IP" in *[!0-9.]*) die "странный IP: $IP";; esac

DOMAIN="${IP//./-}.sslip.io"
URL="https://$DOMAIN/"
say "IP: $IP"
say "домен: $DOMAIN"

# --- 2. Caddy ---------------------------------------------------------------

if ! command -v caddy >/dev/null 2>&1; then
  say "ставлю Caddy"
  case "$(uname -m)" in
    x86_64|amd64) ARCH=amd64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) die "неизвестная архитектура: $(uname -m)" ;;
  esac
  VER="$(curl -fsSL --max-time 15 https://api.github.com/repos/caddyserver/caddy/releases/latest \
        | sed -n 's/.*"tag_name": *"\([^"]*\)".*/\1/p' | head -1)"
  [ -n "$VER" ] || VER="v2.11.7"
  TMP="$(mktemp -d)"
  curl -fsSL --max-time 120 \
    "https://github.com/caddyserver/caddy/releases/download/${VER}/caddy_${VER#v}_linux_${ARCH}.tar.gz" \
    -o "$TMP/caddy.tar.gz" || die "Caddy не скачался"
  tar xzf "$TMP/caddy.tar.gz" -C "$TMP" caddy
  install -m755 "$TMP/caddy" /usr/local/bin/caddy
  rm -rf "$TMP"
fi
say "Caddy: $(caddy version | head -1)"

mkdir -p /etc/caddy
cat > /etc/caddy/Caddyfile <<EOF
{
	admin off
}

$DOMAIN {
	encode zstd gzip
	reverse_proxy 127.0.0.1:$PANEL_PORT
}
EOF

cat > /etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy (TLS для Mini App)
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
User=root
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

# --- 3. панель только на localhost -----------------------------------------

if [ -f "$PANEL_UNIT" ]; then
  sed -i "s|^Environment=BR_WEB_HOST=.*|Environment=BR_WEB_HOST=127.0.0.1|" "$PANEL_UNIT"
  sed -i "s|^Environment=BR_WEB_PORT=.*|Environment=BR_WEB_PORT=$PANEL_PORT|" "$PANEL_UNIT"
  # Аргумент командной строки сильнее переменной окружения: пока в ExecStart
  # стоит --host 0.0.0.0, панель торчит наружу открытым текстом. Убираем его.
  sed -i "s|^ExecStart=.*|ExecStart=/usr/bin/python3 -m web.server|" "$PANEL_UNIT"
  systemctl daemon-reload
  systemctl restart rakcrmp-panel
  say "панель перезапущена на 127.0.0.1:$PANEL_PORT"
else
  say "юнита rakcrmp-panel нет — подними панель сам на 127.0.0.1:$PANEL_PORT"
fi

# --- 4. адрес Mini App в .env ----------------------------------------------

touch "$ENV_FILE"
if grep -q '^BR_MINIAPP_URL=' "$ENV_FILE"; then
  sed -i "s|^BR_MINIAPP_URL=.*|BR_MINIAPP_URL=$URL|" "$ENV_FILE"
else
  printf '\nBR_MINIAPP_URL=%s\n' "$URL" >> "$ENV_FILE"
fi
say "BR_MINIAPP_URL=$URL"

# --- 5. кнопка в боте -------------------------------------------------------

TOKEN="$(sed -n 's/^TG_BOT_TOKEN=//p' "$ENV_FILE" | head -1 | tr -d '"'"'"'\r')"
if [ -n "$TOKEN" ]; then
  RESP="$(curl -fsS --max-time 20 -X POST \
    "https://api.telegram.org/bot${TOKEN}/setChatMenuButton" \
    -H 'Content-Type: application/json' \
    -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"Панель\",\"web_app\":{\"url\":\"$URL\"}}}" 2>/dev/null || true)"
  case "$RESP" in
    *'"ok":true'*) say "кнопка «Панель» в боте обновлена" ;;
    *) say "кнопку поставить не вышло: $RESP" ;;
  esac
else
  say "TG_BOT_TOKEN не найден — кнопку не ставила"
fi

# --- итог -------------------------------------------------------------------

say "жду сертификат (до 60 с)…"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 5 -o /dev/null "$URL" 2>/dev/null; then
    say "готово: $URL"
    exit 0
  fi
  sleep 2
done

say "сертификат ещё не встал. Проверь: journalctl -u caddy -n 40"
say "Порты 80 и 443 должны быть открыты наружу."
exit 1
