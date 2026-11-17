"""Local account-scoped Lua logs with bounded disk use."""
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import threading

LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs', 'lua')
_SECRET = re.compile(r'(?i)(password|passwd|пароль|token|api[_-]?key|ключ)\s*[:=]\s*[^\s,;]+')
_lock = threading.Lock()
_loggers = {}

def sink(bot):
    account = int(bot.bot_id)
    with _lock:
        logger = _loggers.get(account)
        if logger is None:
            os.makedirs(LOG_DIR, exist_ok=True)
            logger = logging.getLogger('black.lua.account.' + str(account))
            logger.setLevel(logging.INFO)
            logger.propagate = False
            handler = RotatingFileHandler(os.path.join(LOG_DIR, f'account_{account}.log'), maxBytes=1024*1024, backupCount=3, encoding='utf-8')
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S'))
            logger.addHandler(handler)
            _loggers[account] = logger
    def write(message):
        text = str(message).replace('\r', ' ').replace('\n', ' ')
        clean = _SECRET.sub(lambda m: m.group(1) + '=[СКРЫТО]', text)[:4000]
        formatted = bot.add_log(clean)
        lua_logs = getattr(bot, 'lua_logs', None)
        if lua_logs is not None:
            lua_logs.append(formatted if isinstance(formatted, str) else clean)
        logger.info(clean)
    return write


def read_log(account, *, limit=4*1024*1024):
    """Read a bounded tail, oldest rotation first, without following symlinks."""
    import stat
    account=int(account)
    if account < 0: raise ValueError('invalid account')
    chunks=[]
    for suffix in ('.3','.2','.1',''):
        path=os.path.join(LOG_DIR,f'account_{account}.log{suffix}')
        flags=os.O_RDONLY | getattr(os,'O_NOFOLLOW',0)
        try:
            fd=os.open(path,flags)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):continue
                with os.fdopen(fd,'rb',closefd=False) as source:
                    source.seek(0,os.SEEK_END);size=source.tell()
                    source.seek(max(0,size-limit));chunks.append(source.read(limit))
            finally:os.close(fd)
        except OSError:continue
    result=b''.join(chunks)[-limit:]
    if result and len(result)>=limit and b'\n' in result: result=result.split(b'\n',1)[1]
    return result.decode('utf-8','replace')
