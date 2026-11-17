"""Small, private, per-account/per-script JSON storage for trusted Lua scripts."""
from __future__ import annotations
import hashlib
import json
import os
import tempfile
import threading

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'lua_data'))
MAX_BYTES = 32768
MAX_KEYS = 64
_LOCK = threading.RLock()

class LuaStorageError(ValueError):
    pass


class LuaStorage:
    def __init__(self, bot, script_path):
        nickname = str(getattr(bot, 'nickname', '') or '').strip()
        server = str(getattr(bot, 'server_name', '') or '').strip()
        if not nickname or not server:
            raise LuaStorageError('неизвестны ник и сервер аккаунта')
        project = str(getattr(getattr(getattr(bot, 'manager', None), 'project', None), 'key', '') or '')
        ident = '\0'.join((project, server.casefold(), nickname.casefold(), os.path.abspath(script_path)))
        self.path = os.path.join(ROOT, hashlib.sha256(ident.encode('utf-8')).hexdigest() + '.json')
        self.data = {}
        self.error = None
        self._read()

    def _read(self):
        if os.path.islink(ROOT):
            self.error = 'каталог lua_data является ссылкой'; return
        flags=os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
        try:
            if os.path.islink(self.path): raise LuaStorageError('файл данных является ссылкой')
            fd=os.open(self.path, flags)
        except FileNotFoundError:
            return
        except (OSError, LuaStorageError):
            self.error = 'файл данных недоступен'; return
        try:
            with os.fdopen(fd, 'rb') as f: raw=f.read(MAX_BYTES+1)
            if len(raw)>MAX_BYTES: raise LuaStorageError('файл данных слишком большой')
            value=json.loads(raw.decode('utf-8'))
            self._serialize(value)
            self.data=value
        except (ValueError, UnicodeError, OSError, RecursionError):
            self.error = 'файл данных повреждён или превышает лимит'

    @staticmethod
    def _serialize(value):
        if not isinstance(value, dict) or len(value)>MAX_KEYS:
            raise LuaStorageError('не больше 64 ключей')
        for key in value:
            if not isinstance(key,str) or not 1 <= len(key) <= 64:
                raise LuaStorageError('ключ должен быть строкой длиной 1–64')
        def validate(item, depth=0):
            if depth > 6: raise LuaStorageError('вложенность больше 6')
            if isinstance(item, str) and len(item) > 4000: raise LuaStorageError('строка больше 4000 символов')
            if isinstance(item, dict):
                if len(item) > 64: raise LuaStorageError('объект больше 64 полей')
                for key, child in item.items():
                    if not isinstance(key, str) or len(key) > 128: raise LuaStorageError('некорректный ключ объекта')
                    validate(child, depth+1)
            elif isinstance(item, list):
                if len(item) > 64: raise LuaStorageError('массив больше 64 элементов')
                for child in item: validate(child, depth+1)
        validate(value)
        try:
            raw=json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',',':')).encode('utf-8')
        except (ValueError, TypeError, RecursionError):
            raise LuaStorageError('значение должно быть корректным JSON')
        if len(raw)>MAX_BYTES: raise LuaStorageError('данные больше 32 КиБ')
        return raw

    def snapshot(self):
        return self.data.copy()

    def apply(self, kind, key=None, value=None):
        with _LOCK:
            if self.error: raise LuaStorageError(self.error + '; сохранение заблокировано')
            if kind not in ('store_set','store_delete','store_clear'): raise LuaStorageError('неизвестная операция')
            if kind != 'store_clear' and (not isinstance(key,str) or not 1 <= len(key) <= 64):
                raise LuaStorageError('ключ должен быть строкой длиной 1–64')
            updated=dict(self.data)
            if kind=='store_set': updated[key]=value
            elif kind=='store_delete': updated.pop(key,None)
            else: updated.clear()
            raw=self._serialize(updated)
            if os.path.islink(ROOT): raise LuaStorageError('каталог lua_data является ссылкой')
            os.makedirs(ROOT, mode=0o700, exist_ok=True)
            fd,temp=tempfile.mkstemp(prefix='.lua-', dir=ROOT)
            try:
                with os.fdopen(fd,'wb') as f:
                    f.write(raw);f.flush();os.fsync(f.fileno())
                os.chmod(temp,0o600)
                os.replace(temp,self.path)
            finally:
                if os.path.exists(temp):os.unlink(temp)
            self.data=updated
