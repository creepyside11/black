"""Per-runtime waypoint controller; no server success is inferred from sending movement."""
from __future__ import annotations
import json
import math
import os
import re
import time
import tempfile

ROUTE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts', 'routes')
NAME = re.compile(r'^[A-Za-z0-9_-]{1,80}$')
MAX_POINTS = 10000


def load_route(name, root=None):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('имя маршрута: только латинские буквы, цифры, _ и -')
    root = os.path.realpath(root or ROUTE_DIR)
    path = os.path.realpath(os.path.join(root, name + '.json'))
    if not path.startswith(root + os.sep):
        raise ValueError('маршрут вне каталога scripts/routes')
    if os.path.getsize(path) > 2_000_000:
        raise ValueError('файл маршрута слишком большой')
    with open(path, encoding='utf-8') as source:
        data = json.load(source)
    raw = data.get('points') if isinstance(data, dict) else data
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_POINTS:
        raise ValueError('нужен список points от 1 до 10000 точек')
    points = []
    for i, p in enumerate(raw, 1):
        if isinstance(p, (list, tuple)) and len(p) == 3:
            p = dict(zip(('x','y','z'), p))
        if not isinstance(p, dict):
            raise ValueError(f'точка {i}: нужны x, y, z')
        try:
            xyz = [float(p[k]) for k in ('x','y','z')]
            radius = float(p.get('radius', 2.0))
            wait = float(p.get('wait', 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError(f'точка {i}: неверные координаты, radius или wait') from None
        if not all(math.isfinite(x) for x in xyz+[radius,wait]) or not 0.1 <= radius <= 100 or not 0 <= wait <= 3600:
            raise ValueError(f'точка {i}: координаты должны быть конечными, radius 0.1..100, wait 0..3600')
        points.append(dict(x=xyz[0], y=xyz[1], z=xyz[2], radius=radius, wait=wait))
    return points


def save_route(name, points, root=None):
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise ValueError('некорректное имя маршрута')
    root = os.path.realpath(root or ROUTE_DIR)
    os.makedirs(root, exist_ok=True)
    target = os.path.join(root, name + '.json')
    if os.path.islink(target):
        raise ValueError('ссылка вместо файла маршрута запрещена')
    fd, temp = tempfile.mkstemp(prefix='_route_', suffix='.json', dir=root)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump({'points': points}, f, ensure_ascii=False, indent=2, allow_nan=False)
        tempname = os.path.basename(temp)[:-5]
        count = len(load_route(tempname, root))
        os.replace(temp, target)
        return count
    finally:
        if os.path.exists(temp): os.unlink(temp)


class RouteController:
    def __init__(self, bot, emit, log, *, armed, clock=time.monotonic, root=None):
        self.bot, self.emit, self.log, self.armed = bot, emit, log, armed
        self.clock, self.root = clock, root
        self.state = 'idle'
        self.name = ''
        self.points = []
        self.index = 0
        self.movement_id = None
        self.api = None
        self.mode = 'walk'
        self.deadline = 0
        self.resume_at = 0
        self.last_position = None
        self.last_progress = 0
        self.timeout = 120
        self.stuck_timeout = 30
        self.point_pending = False

    def report(self):
        return dict(state=self.state, name=self.name, point=min(self.index+1, len(self.points)) if self.points else 0,
                    total=len(self.points), mode=self.mode, movement_id=self.movement_id)

    def _log(self, message):
        self.log('[МАРШРУТ] ' + message)

    def _connected(self):
        return bool(self.api and getattr(getattr(self.api, 'session', None), 'connected', False))

    def start(self, name, mode='walk', timeout=120, stuck_timeout=30):
        if self.state in ('moving','waiting','paused'):
            raise ValueError('маршрут уже запущен; сначала остановите его')
        if mode not in ('walk','drive'):
            raise ValueError('режим маршрута: walk или drive')
        timeout, stuck_timeout = float(timeout), float(stuck_timeout)
        if not all(math.isfinite(x) for x in (timeout,stuck_timeout)) or not 1 <= timeout <= 3600 or not 1 <= stuck_timeout <= 3600:
            raise ValueError('timeout и stuck_timeout должны быть в диапазоне 1..3600 секунд')
        points = load_route(name, self.root)
        self.name, self.points, self.mode, self.index = name, points, mode, 0
        self.timeout, self.stuck_timeout = timeout, stuck_timeout
        self.api = getattr(self.bot, 'api', None)
        if not self.armed:
            self.state = 'dry'
            self._log(f'проверен файл {name}: {len(points)} точек; в --dry движение не запускается')
            self.emit('on_route_dry', self.report())
            return
        if not self._connected():
            self.state = 'error'
            raise ValueError('нет подключения к серверу')
        self._log(f'начат {name}: {len(points)} точек, режим {mode}')
        self._next()

    def _next(self):
        if not self._connected() or getattr(self.bot, 'api', None) is not self.api:
            self._fail('соединение потеряно или сессия изменилась')
            return
        if self.index >= len(self.points):
            self.state, self.movement_id = 'finished', None
            self._log(f'{self.name}: все точки пройдены контроллером')
            self.emit('on_route_end', self.report())
            return
        p = self.points[self.index]
        self.state = 'moving'
        now = self.clock()
        self.deadline, self.last_progress = now + self.timeout, now
        self.last_position = self._pos()
        try:
            self.movement_id = self.api.MoveToCoord(p['x'], p['y'], p['z'], mode='walk' if self.mode == 'walk' else 'vehicle', completion_distance=p['radius'])
            if self.movement_id is None or self.movement_id is False:
                raise ValueError('контроллер не начал движение')
        except Exception as exc:
            self._fail('не удалось запустить движение: ' + str(exc))
            return
        self._log(f"точка {self.index+1}/{len(self.points)}: ({p['x']:.2f}, {p['y']:.2f}, {p['z']:.2f}), радиус {p['radius']:.1f}")
        self.emit('on_route_start_point', self.report())

    def _pos(self):
        try:
            p = self.api.getbotposition()
            xyz = tuple(float(v) for v in p[:3])
            return xyz if all(math.isfinite(v) for v in xyz) else None
        except (TypeError, ValueError, AttributeError, IndexError):
            return None

    def _cancel(self):
        mid, self.movement_id = self.movement_id, None
        if mid is not None and self.api is not None:
            try: self.api.StopMoveToCoord(movement_id=mid)
            except Exception as exc: self._log('ошибка остановки движения: ' + str(exc))

    def _fail(self, reason):
        self._cancel()
        self.state = 'error'
        self._log(f'ошибка на точке {self.index+1}/{len(self.points)}: {reason}')
        self.emit('on_route_error', dict(self.report(), reason=reason))

    def on_move_end(self, movement, status):
        if self.state != 'moving' or not isinstance(movement, dict) or movement.get('movement_id') != self.movement_id:
            return
        self.movement_id = None
        if status != 'arrived':
            self._fail('движение завершено: ' + str(status))
            return
        p = self.points[self.index]
        self._log(f'точка {self.index+1}/{len(self.points)} достигнута по данным контроллера')
        if self.emit('on_route_point', dict(self.report(), x=p['x'], y=p['y'], z=p['z'])) is False:
            self._fail('очередь событий переполнена'); return
        self.index += 1
        self.state = 'waiting'
        self.point_pending = True
        self.resume_at = self.clock() + p['wait']

    def tick(self):
        if self.state not in ('moving','waiting'): return
        if not self._connected() or getattr(self.bot, 'api', None) is not self.api:
            self._fail('соединение потеряно или сессия изменилась'); return
        now = self.clock()
        if self.state == 'waiting':
            if not self.point_pending and now >= self.resume_at: self._next()
            return
        if now >= self.deadline:
            self._fail('превышено время движения к точке'); return
        p = self._pos()
        if p is not None:
            if self.last_position is None or math.dist(p, self.last_position) >= 0.5:
                self.last_position, self.last_progress = p, now
            elif now - self.last_progress >= self.stuck_timeout:
                self._fail('позиция не меняется: возможное застревание')

    def pause(self):
        if self.state not in ('moving','waiting'): raise ValueError('маршрут не выполняется')
        self._cancel()
        self.state = 'paused'
        self._log('пауза')

    def resume(self):
        if self.state != 'paused': raise ValueError('маршрут не на паузе')
        self._log('продолжение')
        self._next()

    def stop(self):
        if self.state in ('moving','waiting','paused'):
            self._cancel()
            self.state = 'stopped'
            self._log('остановлен')
