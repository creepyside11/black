"""Offline Lua playground: simulated events and clocks, never connects an account."""
from __future__ import annotations
import heapq
import itertools
import json
import os
import re
import sys
from logic.lua_engine import LuaEngine

ROOT = os.path.realpath(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'test_lua_scripts'))
MAX_EVENTS = 200


def resolve(name, suffix, root=ROOT):
    if not isinstance(name, str) or not name.endswith(suffix) or os.path.isabs(name):
        raise ValueError('нужно имя файла внутри test_lua_scripts с расширением ' + suffix)
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(os.path.realpath(root) + os.sep) or not os.path.isfile(path):
        raise ValueError('файл не найден в test_lua_scripts')
    return path


def run(name, fixture=None, *, root=ROOT, output=print):
    script = resolve(name, '.lua', root)
    events = []
    if fixture:
        path = resolve(fixture, '.json', root)
        if os.path.getsize(path) > 512*1024: raise ValueError('файл событий слишком большой')
        with open(path, encoding='utf8') as f: spec = json.load(f)
        events = spec.get('events') if isinstance(spec, dict) else spec
        if not isinstance(events, list) or len(events) > MAX_EVENTS: raise ValueError('events должен быть списком до 200 событий')
    engine = LuaEngine()
    clock = 0.0
    serial = itertools.count()
    queue = []
    seen = 0
    snapshot = {'connected': False, 'bot_id': None, 'mode': 'sandbox', 'now': clock,
                'pos': None, 'vehicle_id': None, 'health': None, 'money': None,
                'interior': None, 'route': {}, 'tail': []}
    def schedule(at, name, args):
        if len(queue) >= MAX_EVENTS: raise ValueError('слишком много событий или таймеров')
        heapq.heappush(queue, (at, next(serial), name, args))
    for e in events:
        if not isinstance(e, dict) or not isinstance(e.get('name'), str) or not re.fullmatch(r'on_[A-Za-z0-9_]{1,90}', e['name']):
            raise ValueError('у каждого события нужно корректное имя on_*')
        delay = float(e.get('at', 0))
        if not 0 <= delay <= 3600: raise ValueError('время at вне диапазона 0..3600')
        args = e.get('args', {})
        if not isinstance(args, dict): raise ValueError('args должен быть объектом')
        schedule(delay, e['name'], args)
    def consume(answer, source):
        if not answer.get('ok', True): raise RuntimeError('Lua: ' + str(answer.get('error', 'ошибка')))
        for line in answer.get('logs') or []: output('[Lua] ' + str(line))
        actions = answer.get('actions') or []
        results = []
        for action in actions:
            if not isinstance(action, dict): continue
            kind = str(action.get('kind') or '?')
            if kind == 'after':
                seconds = float(action.get('sec', 0)); name = action.get('name')
                if not 0 <= seconds <= 3600 or not isinstance(name, str): raise ValueError('неверный таймер')
                schedule(clock + seconds, name, action.get('data') or {})
                output(f'[{clock:.2f} c] таймер {name}: +{seconds:.2f} c')
                results.append({'kind': kind, 'status': 'sent', 'detail': 'виртуальный таймер'})
            else:
                # No action data is printed: chat, JSON, raw RPC and credentials may be private.
                detail = ''
                if kind in ('walk','drive','tp'):
                    detail = ' x={} y={} z={}'.format(action.get('x'), action.get('y'), action.get('z'))
                elif kind == 'route_start':
                    detail = ' маршрут={} режим={}'.format(str(action.get('route'))[:80], str(action.get('mode'))[:20])
                elif kind == 'send_json':
                    detail = ' интерфейс={}'.format(action.get('interface_id'))
                output(f'[{clock:.2f} c] {kind}{detail}: имитация, не выполнено')
                results.append({'kind': kind, 'status': 'skipped', 'detail': 'полигон: действие не выполняется'})
        if source not in ('on_load', 'on_action_result') and results:
            schedule(clock, 'on_action_result', {'source': source, 'results': results})
    try:
        engine.start(script)
        output('[ПОЛИГОН] Офлайн: подключений и игровых действий нет')
        consume(engine.request({'op':'load','path':script,'armed':False,'seed':1,'max_actions':32,'snapshot':snapshot}, timeout=10), 'on_load')
        while queue:
            clock, _, name, args = heapq.heappop(queue)
            seen += 1
            if seen > MAX_EVENTS: raise ValueError('достигнут предел 200 событий; проверьте повторяющиеся таймеры')
            snapshot['now'] = clock
            output(f'[{clock:.2f} c] событие {name}')
            consume(engine.request({'op':'event','name':name,'args':args,'snapshot':snapshot}), name)
        output(f'[ПОЛИГОН] Завершено. Обработано событий: {seen}')
        return seen
    finally:
        engine.close(force=True)


def main(argv=None):
    import argparse
    ap=argparse.ArgumentParser(description='Офлайн-полигон Lua; сценарии лежат в test_lua_scripts')
    ap.add_argument('script'); ap.add_argument('--events', default=None, help='JSON-файл событий из test_lua_scripts')
    args=ap.parse_args(argv)
    try: run(args.script, args.events)
    except Exception as exc:
        print('[ПОЛИГОН] Ошибка:', exc, file=sys.stderr); return 1
    return 0

if __name__ == '__main__': raise SystemExit(main())
