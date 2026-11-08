#!/usr/bin/env bash
# Установка команды 'br' для Termux / Linux / macOS.
#
# Что было не так: раньше скрипт жёстко прописывал путь к python3 из Termux
# (/data/data/com.termux/files/usr/bin/python3). На обычном Linux и на macOS
# такой команды нет — обёртка ставилась и падала с «No such file or
# directory». Теперь запускается переносимый bin/br, а интерпретатор
# подбирается на месте.

set -e

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$REPO_DIR/bin/br"

if [ ! -f "$REPO_DIR/bot.py" ]; then
    echo "[!] В $REPO_DIR нет bot.py — запусти install.sh из корня репозитория." >&2
    exit 1
fi

chmod +x "$LAUNCHER" 2>/dev/null || true

# Куда ставить: Termux -> $PREFIX/bin, Linux -> ~/.local/bin или /usr/local/bin
if [ -n "$PREFIX" ] && [ -d "$PREFIX/bin" ] && [ -w "$PREFIX/bin" ]; then
    TARGET_DIR="$PREFIX/bin"
elif [ -d "$HOME/.local/bin" ] || mkdir -p "$HOME/.local/bin" 2>/dev/null; then
    TARGET_DIR="$HOME/.local/bin"
elif [ -d "/usr/local/bin" ] && [ -w "/usr/local/bin" ]; then
    TARGET_DIR="/usr/local/bin"
else
    echo "[!] Не нашёл каталог в PATH для установки. Добавь вручную:" >&2
    echo "    ln -s \"$LAUNCHER\" ~/.local/bin/br" >&2
    exit 1
fi

TARGET_BIN="$TARGET_DIR/br"

# Если в каталоге уже лежит чужой br (например, от прошлой установки) —
# перезаписываем symlink'ом на актуальный лаунчер.
rm -f "$TARGET_BIN"
ln -s "$LAUNCHER" "$TARGET_BIN" 2>/dev/null || {
    cp "$LAUNCHER" "$TARGET_BIN"
    chmod +x "$TARGET_BIN"
}

echo "[+] Команда 'br' установлена: $TARGET_BIN -> $LAUNCHER"
echo "[+] Проверка: br  (или $TARGET_BIN)"
case ":$PATH:" in
    *":$TARGET_DIR:"*) ;;
    *) echo "[i] Добавь $TARGET_DIR в PATH: export PATH=\"$TARGET_DIR:\$PATH\"" ;;
esac

# Клиент из пакета br/ запускается отдельно: python3 -m br
echo "[i] Одиночный клиент: python3 -m br   (из $REPO_DIR)"