"""Harvest task: server textdraw events, farm menus, checkpoints and one-hour limit."""
import math
import re
import threading
import time

from logic.bot_tasks import BaseTask
from logic.farm import (FarmRunner, FARM_WORK, S_TO_WORK, S_MINIGAME, S_TO_DROPOFF,
                        S_RETRY, S_IDLE)
from logic import immortal

_APPLE = b'gui:fruitpickerapple'
_PROGRESS = re.compile(rb'(\d+(?:[.,]\d+)?)_\s*/_\s*(\d+(?:[.,]\d+)?)')


class FarmTask(BaseTask):
    DURATION = 3600.0

    def __init__(self, bot_instance):
        super().__init__(bot_instance)
        self.name = 'Сборщик урожая'
        self.runner = FarmRunner(self.bot, travel=self._travel, log=self.bot.add_log,
                                 on_minigame=self._minigame)
        self._lock = threading.RLock()
        self._work_started = None
        self._checkpoint = None
        self._checkpoint_seq = 0
        self._last_checkpoint_seq = 0
        self._last_apple_at = 0.0
        self._last_move_at = 0.0
        self._apple_count = 0
        self._target_kg = 5.0
        self._timer = None

    def _travel(self, bot, target):
        movement_id = self.api.MoveToCoord(*target, mode='walk', completion_distance=2.0,
                                            stop_on_dialog=True)
        started = time.monotonic()
        try:
            while self.running and self.session.connected:
                if math.dist(self.api.getbotposition(), target) <= 3.0:
                    return
                if time.monotonic() - started > 600:
                    raise RuntimeError('не удалось добраться до точки за 10 минут')
                time.sleep(0.5)
            raise RuntimeError('задача остановлена или соединение разорвано')
        finally:
            self.api.StopMoveToCoord(movement_id, reason='farm_end')

    def stop(self):
        if not self.running:
            return
        self.running = False
        if self._timer is not None:
            self._timer.cancel()
        try:
            self.api.StopMoveToCoord(reason='farm_stopped')
        except Exception:
            pass
        self.runner.stop()
        immortal.stop(self.bot, log=self.bot.add_log)
        self.bot.add_log('[ФЕРМА] Задача остановлена')

    def _time_up(self):
        self.bot.add_log('[ФЕРМА] Час работы завершён')
        self.bot.default_task_name = 'stop'
        manager = getattr(self.bot, 'manager', None)
        if manager is not None:
            try:
                manager.save_accounts()
            except Exception as exc:
                self.bot.add_log(f'[ФЕРМА] Не удалось сохранить остановку: {exc}')
        self.stop()

    def on_dialog(self, dialog_id, style, title, info, button1='', button2=''):
        if self.running:
            self.runner.on_dialog(dialog_id, style, title, info, button1, button2)

    def on_receive_json(self, interface_id, data):
        if self.running and interface_id == 10 and isinstance(data, dict):
            items = data.get('m') or []
            self.runner.on_dialog_menu(items if isinstance(items, list) else [items],
                                       closed=data.get('c') == 1, payload=data)

    def on_checkpoint(self, x, y, z, radius):
        if not self.running or self.runner.state not in (S_TO_WORK, S_MINIGAME, S_TO_DROPOFF):
            return
        try:
            point = (float(x), float(y), float(z))
            if math.dist(point, FARM_WORK) > 350:
                return
            with self._lock:
                self._checkpoint = point
                self._checkpoint_seq += 1
        except (ValueError, TypeError):
            return

    def on_receive_rpc(self, rpc_id, payload):
        if not self.running or self.runner.state not in (S_TO_WORK, S_MINIGAME):
            return
        if not isinstance(payload, (bytes, bytearray)):
            return
        payload = bytes(payload)
        # 349 sends all reserved names in advance; only 418 shows the live apple.
        if rpc_id == 418 and len(payload) >= 24 and payload.endswith(b'\x14\x00' + _APPLE):
            apple_id = int.from_bytes(payload[:2], 'little')
            if not 0x800 <= apple_id <= 0x80c:
                return
            try:
                self.api.SendRPC(437, apple_id.to_bytes(2, 'little'))
                with self._lock:
                    self._last_apple_at = time.monotonic()
                    self._apple_count += 1
                self.bot.add_log(f'[ФЕРМА] Яблоко {apple_id:#x}: отправлен сбор')
            except Exception as exc:
                self.bot.add_log(f'[ФЕРМА] Ошибка сбора яблока: {exc}')
        elif rpc_id == 349 and len(payload) >= 4:
            text_len = int.from_bytes(payload[2:4], 'little')
            if text_len != len(payload) - 4:
                return
            match = _PROGRESS.search(payload[4:])
            if match:
                try:
                    kg = float(match.group(1).replace(b',', b'.'))
                    target = float(match.group(2).replace(b',', b'.'))
                    if 0 <= kg <= target <= 100 and target > 0:
                        with self._lock:
                            self.runner.kg = kg
                            self._target_kg = target
                except ValueError:
                    pass

    def _minigame(self, runner):
        with self._lock:
            kg, target = runner.kg, self._target_kg
            last_apple, checkpoint = self._last_apple_at, self._checkpoint
            seq = self._checkpoint_seq
        if kg >= target and last_apple and time.monotonic() - last_apple > 1.0:
            return True
        # When the current tree is finished the server points to the next one.
        if (checkpoint is not None and seq > self._last_checkpoint_seq
                and (not last_apple or time.monotonic() - last_apple > 1.5)
                and time.monotonic() - self._last_move_at > 2):
            self._last_checkpoint_seq = seq
            self._last_move_at = time.monotonic()
            self.bot.add_log('[ФЕРМА] Иду к отметке следующего дерева')
            self._travel(self.bot, checkpoint)
            runner.minigame_started_at = time.time()
        return False

    def _run_loop(self):
        try:
            immortal.enable(self.bot, log=self.bot.add_log)
            self.runner.start()
            while self.running and self.session.connected:
                if self.runner.state in (S_TO_WORK, S_MINIGAME, S_TO_DROPOFF) and self._work_started is None:
                    self._work_started = time.monotonic()
                    self._timer = threading.Timer(self.DURATION, self._time_up)
                    self._timer.daemon = True
                    self._timer.start()
                    self.bot.add_log('[ФЕРМА] Смена началась: остановка через 1 час')
                previous = self.runner.state
                self.runner.step()
                if previous == S_TO_DROPOFF and self.runner.state == S_TO_WORK:
                    with self._lock:
                        self.runner.kg = 0.0
                        self._last_apple_at = 0.0
                        self._last_checkpoint_seq = self._checkpoint_seq
                time.sleep(0.25 if self.runner.state == S_MINIGAME else 0.8)
        except Exception as exc:
            self.bot.add_log(f'[ФЕРМА] Ошибка: {exc}')
        finally:
            self.stop()
