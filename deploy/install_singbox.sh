#!/usr/bin/env bash
# Установка sing-box для моста VLESS -> SOCKS5.
#
# Зачем: подписка отдаёт узлы vless/vmess/trojan/ss, а бот умеет только
# SOCKS5. Без sing-box такие узлы в пуле висят жёлтыми и применить их
# нельзя. Мост поднимает по одному sing-box на узел и отдаёт боту
# socks5://127.0.0.1:<порт>.
#
# Запуск:  bash deploy/install_singbox.sh
# Проверка: /opt/blackrussia/venv/bin/python -c "from logic.vpn_bridge import bridge_available; print(bridge_available())"

set -euo pipefail

ARCH="$(uname -m)"
case "$ARCH" in
    x86_64|amd64) SB_ARCH="amd64" ;;
    aarch64|arm64) SB_ARCH="arm64" ;;
    armv7l|armv7) SB_ARCH="armv7" ;;
    *) echo "[-] Неизвестная архитектура: $ARCH"; exit 1 ;;
esac

VERSION="${SING_BOX_VERSION:-1.11.15}"
TARBALL="sing-box-${VERSION}-linux-${SB_ARCH}.tar.gz"
URL="https://github.com/SagerNet/sing-box/releases/download/v${VERSION}/${TARBALL}"

echo "[*] Архитектура: ${SB_ARCH}, версия: ${VERSION}"

if command -v sing-box >/dev/null 2>&1; then
    echo "[+] sing-box уже стоит: $(sing-box version | head -1)"
    exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[*] Качаю $URL"
if ! curl -fsSL --retry 3 -o "$TMP/$TARBALL" "$URL"; then
    echo "[-] Не скачал. Проверь сеть или задай SING_BOX_VERSION."
    exit 1
fi

echo "[*] Распаковываю"
tar -xzf "$TMP/$TARBALL" -C "$TMP"

BIN="$(find "$TMP" -type f -name sing-box -perm -u+x | head -1)"
if [ -z "$BIN" ]; then
    echo "[-] В архиве нет бинарника sing-box"
    exit 1
fi

install -m 0755 "$BIN" /usr/local/bin/sing-box
echo "[+] Установлен: $(/usr/local/bin/sing-box version | head -1)"

# Мост запускает процессы сам, отдельный сервис не нужен.
echo
echo "Готово. Дальше:"
echo "  1. systemctl restart br-bot"
echo "  2. в Telegram: 🌐 Прокси и VPN -> Проверить сейчас"
echo "  3. узлы станут зелёными и будут раздаваться кнопками"
