"""Optional managed sing-box process for the user's existing _sb/config.json."""
from __future__ import annotations
import atexit
import os
import subprocess
import tempfile
import threading
import time
from logic.vpn_bridge import find_binary

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

class SingBoxService:
    def __init__(self, config=None, log=print):
        self.config = os.path.abspath(config or os.environ.get('BR_SINGBOX_CONFIG') or os.path.join(ROOT, '_sb', 'config.json'))
        self.log = log
        self.proc = None
        self.lock = threading.RLock()
        self.last_error = ''
        atexit.register(self.stop)

    def status(self):
        with self.lock:
            running = self.proc is not None and self.proc.poll() is None
            return {'running':running, 'pid':self.proc.pid if running else None,
                    'configured':os.path.isfile(self.config), 'binary':find_binary(),
                    'error':self.last_error}

    def start(self, *, quiet_missing=False):
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.log('[sing-box] Уже запущен программой')
                return True
            if not os.path.isfile(self.config):
                self.last_error = 'нет конфигурации _sb/config.json'
                if not quiet_missing: self.log('[sing-box] ' + self.last_error + '; создайте или укажите BR_SINGBOX_CONFIG')
                return False
            binary = find_binary()
            if not binary:
                self.last_error = 'не найден исполняемый файл; в Termux: pkg install sing-box'
                self.log('[sing-box] ' + self.last_error)
                return False
            try:
                check = subprocess.run([binary, 'check', '-c', self.config], stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, timeout=12)
                if check.returncode:
                    self.last_error = 'конфигурация отклонена sing-box; выполните sing-box check -c путь_к_конфигу'
                    self.log('[sing-box] ' + self.last_error)
                    return False
                self.proc = subprocess.Popen([binary, 'run', '-c', self.config], stdout=subprocess.DEVNULL,
                                             stderr=subprocess.DEVNULL)
                time.sleep(.4)
                if self.proc.poll() is not None:
                    self.last_error = f'процесс сразу завершился (код {self.proc.returncode}); проверьте конфиг и занятые порты'
                    self.log('[sing-box] ' + self.last_error)
                    self.proc = None
                    return False
            except (OSError, subprocess.SubprocessError) as exc:
                self.last_error = 'ошибка запуска: ' + type(exc).__name__
                self.log('[sing-box] ' + self.last_error)
                return False
            self.last_error = ''
            self.log('[sing-box] Процесс запущен; работоспособность внешнего узла нужно проверить отдельно')
            return True

    def stop(self):
        with self.lock:
            proc, self.proc = self.proc, None
            if proc and proc.poll() is None:
                proc.terminate()
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait(timeout=3)
