#!/data/data/com.termux/files/usr/bin/bash
# Автоустановка и регистрация команды 'br' в Termux / Linux

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_PATH="/data/data/com.termux/files/usr/bin/br"

if [ -d "/data/data/com.termux/files/usr/bin" ]; then
    TARGET_BIN="$BIN_PATH"
elif [ -d "$HOME/.local/bin" ]; then
    TARGET_BIN="$HOME/.local/bin/br"
    mkdir -p "$HOME/.local/bin"
else
    TARGET_BIN="/usr/local/bin/br"
fi

cat << SCRIPT > "$TARGET_BIN"
#!/bin/bash
cd "$REPO_DIR" && exec /data/data/com.termux/files/usr/bin/python3 bot.py "\$@"
SCRIPT

chmod +x "$TARGET_BIN"
echo "[+] Команда 'br' успешно установлена в $TARGET_BIN"
echo "[+] Теперь можно просто написать 'br' в любом месте терминала!"
