"""Helpers for administrator-managed Lua uploads and launches."""
import os
import re
from pathlib import Path
from logic import lua_runtime

MAX_SCRIPT_BYTES=256*1024
NAME=re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,95}\.lua$')


def store_script(name, data):
    if not isinstance(name,str) or not NAME.fullmatch(name) or '..' in name:
        raise ValueError('Имя должно содержать латинские буквы, цифры, точки, _ или - и заканчиваться на .lua.')
    if not isinstance(data,bytes) or not 0<len(data)<=MAX_SCRIPT_BYTES:
        raise ValueError('Нужен текстовый Lua-файл размером до 256 КБ.')
    try:text=data.decode('utf-8-sig')
    except UnicodeDecodeError:raise ValueError('Файл должен быть в UTF-8.')
    if not text.strip() or '\x00' in text:raise ValueError('Файл не содержит корректный текст Lua.')
    root=Path(lua_runtime.SCRIPT_DIR)
    if root.is_symlink():raise ValueError('Каталог скриптов не должен быть символической ссылкой.')
    root.mkdir(parents=True,exist_ok=True)
    path=root/name
    try:
        with path.open('x',encoding='utf-8',newline='') as out:out.write(text)
    except FileExistsError:raise ValueError('Файл с таким именем уже существует. Переименуйте файл и отправьте снова.')
    return name


def prepare_runtime(bot, name, mode='bot'):
    if mode not in ('bot','addon'):raise ValueError('Неизвестный режим.')
    path=lua_runtime.resolve_script(name)
    current=getattr(bot,'lua_runtime',None)
    if current and current.running:raise ValueError('Сначала остановите запущенный Lua-скрипт.')
    if mode=='bot' and (getattr(getattr(bot,'session',None),'connected',False) or
                        (getattr(bot,'thread',None) is not None and bot.thread.is_alive())):
        raise ValueError('Для режима бота сначала остановите аккаунт.')
    runtime=lua_runtime.LuaRuntime(bot,path,mode=mode,armed=True)
    bot.lua_spawn_control=mode=='bot';bot.lua_ui_control=runtime.own_ui;bot.lua_runtime=runtime
    try:runtime.start()
    except Exception:
        runtime.stop();bot.lua_runtime=None;bot.lua_spawn_control=False;bot.lua_ui_control=False
        raise
    return runtime
