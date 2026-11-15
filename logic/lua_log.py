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
        bot.add_log(clean)
        logger.info(clean)
    return write
