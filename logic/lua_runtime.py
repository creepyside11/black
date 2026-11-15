"""Per-account Lua runner. One Lua process and one ordered event loop per script."""
from __future__ import annotations
import heapq
import itertools
import os
import queue
import threading
import time
from logic.events import emitter
from logic.lua_engine import LuaScript, LuaScriptError, _hex, _json_safe
from logic.lua_routes import RouteController, save_route
from logic.lua_log import sink as lua_log_sink

SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'scripts', 'lua')
MAX_EVENTS = 1000
MAX_TIMERS = 256


def resolve_script(name):
    root = os.path.realpath(SCRIPT_DIR)
    path = os.path.realpath(os.path.join(root, name))
    if not path.startswith(root + os.sep) or not path.endswith('.lua'):
        raise ValueError('скрипт должен находиться в scripts/lua и иметь расширение .lua')
    if not os.path.isfile(path):
        raise ValueError('файл скрипта не найден: ' + str(name))
    return path


def list_scripts():
    os.makedirs(SCRIPT_DIR, exist_ok=True)
    return sorted(os.path.relpath(os.path.join(root, name), SCRIPT_DIR)
                  for root, _, files in os.walk(SCRIPT_DIR)
                  for name in files if name.endswith('.lua'))


class LuaRuntime:
    def __init__(self, bot, path, *, mode='addon', armed=True, packets=False, own_ui=False, tick=0.25):
        if mode not in ('addon', 'bot'):
            raise ValueError('режим: addon или bot')
        self.bot, self.mode, self.path = bot, mode, path
        self.packets = bool(packets)
        self.own_ui = mode == 'bot' or bool(own_ui)
        self.tick = max(0.05, float(tick))
        self.events = queue.Queue(maxsize=MAX_EVENTS)
        self.stop_event = threading.Event()
        self._timers = []
        self._seq = itertools.count()
        self._subscriptions = []
        self._thread = None
        self.dropped = 0
        self.failure = ''
        self._captcha_lock = threading.RLock()
        self._captcha_generation = 0
        self._captcha_url = None
        self._captcha_inflight = set()
        self.log = lua_log_sink(bot)
        self.route = RouteController(bot, self._enqueue, self.log, armed=armed)
        self.script = LuaScript(path, api=getattr(bot, 'api', None), bot=bot, mode=mode,
                                armed=armed, log=self.log, schedule=self.schedule,
                                sleep=lambda n:self.stop_event.wait(n),
                                should_stop=self.stop_event.is_set,
                                captcha_handler=self.request_captcha, route_handler=self._route_action)

    def _route_action(self, action):
        kind = action["kind"]
        if kind == "route_save":
            count = save_route(action.get("route"), action.get("points"), self.route.root)
            self.log(f'[МАРШРУТ] сохранён {action.get("route")}: {count} точек')
        elif kind == "route_start":
            options = action.get("options") or {}
            if not isinstance(options, dict): raise ValueError("настройки маршрута должны быть таблицей")
            self.route.start(action.get("route"), action.get("mode") or "walk", options.get("timeout", 120), options.get("stuck_timeout", 30))
        elif kind == "route_pause": self.route.pause()
        elif kind == "route_resume": self.route.resume()
        elif kind == "route_stop": self.route.stop()

    @property
    def running(self):
        return not self.stop_event.is_set() and self._thread is not None and self._thread.is_alive()

    def schedule(self, seconds, name, data):
        if not 0 <= seconds <= 3600 or not name or len(name) > 100:
            raise LuaScriptError('некорректный таймер')
        if len(self._timers) >= MAX_TIMERS:
            raise LuaScriptError('слишком много таймеров')
        heapq.heappush(self._timers, (time.monotonic()+seconds, next(self._seq), name, _json_safe(data)))

    def _enqueue(self, name, payload):
        if self.stop_event.is_set(): return
        try:
            self.events.put_nowait((name, _json_safe(payload)))
            return True
        except queue.Full:
            self.dropped += 1
            return False

    def _bind(self):
        # EventEmitter supplies the API object, not the account. Resolve it at
        # delivery time because setup_session creates a new API on each start.
        mapping = {
          'onReceiveJSON': ('on_json', lambda a:{'interface_id':a[0], 'data':a[1]}),
          'onReceiveRPC': ('on_rpc', lambda a:{'rpc_id':a[0], 'payload_hex':_hex(a[1]), 'size':len(a[1] or b'')}),
          'onShowDialog': ('on_dialog', lambda a:{'id':a[0],'style':a[1],'title':a[2],'text':a[3],'button1':a[4],'button2':a[5]}),
          'onSpawnSelect': ('on_spawn_menu', lambda a:{'data':a[0]}),
          'onRegistrationState': ('on_auth', lambda a:{'state':a[0]}),
          'onShowChat': ('on_chat', lambda a:{'message':a[0]}),
          'onNpcDialog': ('on_npc_dialog', lambda a:{'npc':a[0], 'text':a[1], 'model':a[2], 'buttons':a[3]}),
          'onNotification': ('on_notification', lambda a:{'text':a[0], 'button':a[1], 'has_button':a[2]}),
          'onSetMapIcon': ('on_map_icon', lambda a:{'id':a[0], 'position':a[1], 'marker_type':a[2], 'style':a[3]}),
          'onRemoveMapIcon': ('on_map_icon_remove', lambda a:{'id':a[0]}),
          'OnSetCheckpoint': ('on_checkpoint', lambda a:{'x':a[0],'y':a[1],'z':a[2],'radius':a[3]}),
          'OnDisableCheckpoint': ('on_checkpoint_clear', lambda a:{}),
          'OnShowTextDraw': ('on_textdraw', lambda a:{'id':a[0],'data':a[1]}),
          'OnTextDrawSetString': ('on_textdraw_text', lambda a:{'id':a[0],'text':a[1]}),
          'onHideTextDraw': ('on_textdraw_hide', lambda a:{'id':a[0]}),
          'OnCoordMoveEnd': ('on_move_end', lambda a:{'movement':a[0],'status':a[1]}),
          'onSpawn': ('on_spawn', lambda a:{}),
          'onconnect': ('on_connect', lambda a:{}),
          'disconnect': ('on_disconnect', lambda a:{}),
        }
        if self.packets:
            mapping['onReceivePacket'] = ('on_packet', lambda a:{'packet_id':a[0], 'body_hex':_hex(a[1]), 'size':len(a[1] or b'')})
        for event, (name, convert) in mapping.items():
            def receive(source, *args, _name=name, _convert=convert, **kwargs):
                if source is not getattr(self.bot, 'api', None): return
                try:
                    if _name == 'on_json' and len(args) >= 2 and args[0] == 26:
                        self._observe_captcha(args[1])
                    self._enqueue(_name, _convert(args))
                except Exception as exc: self.bot.add_log(f'[LUA] ошибка события {_name}: {exc}')
            emitter.subscribe(event, receive, sync=True)
            self._subscriptions.append((event, receive))

    def _observe_captcha(self, data):
        if not isinstance(data, dict):
            return
        with self._captcha_lock:
            if data.get('c'):
                self._captcha_generation += 1
                self._captcha_url = None
            elif data.get('u'):
                url = str(data['u'])
                if self._captcha_url != url:
                    self._captcha_generation += 1
                    self._captcha_url = url

    def request_captcha(self, data):
        from logic.captcha import CaptchaAnswerer, from_interface
        challenge = from_interface(26, data)
        session = getattr(self.bot, 'session', None)
        if challenge is None or not session or not getattr(session, 'connected', False):
            return False
        url = str(challenge.image_url or '')
        if not url:
            return False
        with self._captcha_lock:
            if self._captcha_url != url:
                return False
            token = self._captcha_generation
            key = (id(session), token, url)
            if key in self._captcha_inflight:
                return False
            self._captcha_inflight.add(key)
        runtime = self
        class GuardedSession:
            @property
            def connected(self):
                return (not runtime.stop_event.is_set() and runtime.bot.session is session
                        and getattr(session, 'connected', False))
            def send_json(self, *args, **kwargs):
                with runtime._captcha_lock:
                    if (not self.connected or token != runtime._captcha_generation or
                            (runtime._captcha_url is not None and runtime._captcha_url != url)):
                        return False
                    return session.send_json(*args, **kwargs)
        def solve():
            try:
                solver = CaptchaAnswerer(log=self.bot.add_log)
                solver.handle(GuardedSession(), challenge)
            except Exception as exc:
                if not self.stop_event.is_set():
                    self.bot.add_log(f'[LUA] Ошибка решателя капчи: {exc}')
            finally:
                with self._captcha_lock:
                    self._captcha_inflight.discard(key)
        threading.Thread(target=solve, name=f'lua-captcha-{self.bot.bot_id}', daemon=True).start()
        return True

    def start(self):
        self.script.load(execute_actions=False)  # compile before connecting
        self._bind()
        self._thread = threading.Thread(target=self._run, name=f'lua-{self.bot.bot_id}', daemon=True)
        self._thread.start()
        return self

    def _feed(self, name, data):
        self.script.route_status = self.route.report()
        results = self.script.feed(name, data)
        if name == 'on_route_point': self.route.point_pending = False
        if results and name != 'on_action_result' and not self.stop_event.is_set():
            self.script.feed('on_action_result', {'source': name, 'results': results})

    def _run(self):
        next_tick = time.monotonic() + self.tick
        try:
            self.script.api = getattr(self.bot, 'api', None)
            initial = self.script.initial_actions
            self.script.initial_actions = []
            if initial:
                self.script.execute_all(initial)
            while not self.stop_event.is_set():
                current_api = getattr(self.bot, 'api', None)
                if current_api is not self.script.api:
                    self.script.movement_id = None
                    self.script.api = current_api
                now = time.monotonic()
                while self._timers and self._timers[0][0] <= now:
                    _, _, name, data = heapq.heappop(self._timers)
                    self._feed(name, data)
                if now >= next_tick:
                    self.route.tick()
                    self._feed('on_tick', {})
                    next_tick = time.monotonic() + self.tick
                wait = min(0.1, max(0.0, next_tick-time.monotonic()))
                try: name, data = self.events.get(timeout=wait)
                except queue.Empty: continue
                if name == 'on_move_end':
                    self.route.on_move_end(data.get('movement'), data.get('status'))
                self._feed(name, data)
        except Exception as exc:
            if not self.stop_event.is_set():
                self.failure = f'{type(exc).__name__}: {exc}'
                self.log('[LUA] Скрипт остановлен: ' + self.failure)
        finally:
            self.route.stop()
            self.stop_event.set()
            for event, callback in self._subscriptions: emitter.off(event, callback)
            self._subscriptions.clear()
            self.script.close(force=True)
            if getattr(self.bot, 'lua_runtime', None) is self:
                self.bot.lua_spawn_control = False
                self.bot.lua_ui_control = False

    def stop(self):
        if self.stop_event.is_set(): return
        self.stop_event.set()
        # Kill a script stuck in a handler rather than waiting for its timeout.
        if self._thread and self._thread.is_alive():
            self.script.engine._kill()
            if threading.current_thread() is not self._thread:
                self._thread.join(timeout=4)
        else:
            self.script.close(force=True)
        for event, callback in self._subscriptions: emitter.off(event, callback)
        self._subscriptions.clear()
        self.bot.lua_spawn_control = False
        self.bot.lua_ui_control = False

    def report(self):
        out = self.script.report()
        out.update(mode=self.mode, own_ui=self.own_ui, running=self.running, queued=self.events.qsize(),
                   queue_dropped=self.dropped, pending_timers=len(self._timers), failure=self.failure, route=self.route.report())
        return out


def handle_command(manager, line):
    """Console command; scripts must be placed under scripts/lua."""
    import shlex
    try:
        parts = shlex.split(line)
        if len(parts) < 2 or parts[1] in ('help', 'помощь'):
            print('Lua: /lua list | /lua addon ID ФАЙЛ [--dry] | /lua bot ID ФАЙЛ [--dry] | /lua status ID | /lua reload ID | /lua stop ID')
            return
        cmd = parts[1].lower()
        if cmd == 'list':
            files = list_scripts()
            print('Lua-скрипты в scripts/lua:')
            for path in files: print('  ' + path)
            if not files: print('  нет файлов')
            return
        if len(parts) < 3 or not parts[2].isdigit():
            raise ValueError('укажите числовой ID аккаунта')
        bot = manager.bots.get(int(parts[2]))
        if bot is None: raise ValueError('аккаунт не найден')
        current = getattr(bot, 'lua_runtime', None)
        if cmd == 'status':
            print('[LUA]', current.report() if current else 'скрипт не запущен')
            return
        if cmd == 'stop':
            if current: current.stop(); bot.lua_runtime = None
            print('[LUA] Скрипт остановлен')
            return
        if cmd == 'reload':
            if not current: raise ValueError('нет скрипта для перезагрузки')
            if current.mode == 'bot' and (getattr(bot.session, 'connected', False) or
                                           (getattr(bot, 'thread', None) is not None and bot.thread.is_alive())):
                raise ValueError('в режиме bot сначала остановите аккаунт перед перезагрузкой')
            mode, path, armed, packets, own_ui = current.mode, current.path, current.script.armed, current.packets, current.own_ui
            current.stop()
        elif cmd in ('addon', 'bot'):
            if len(parts) < 4: raise ValueError('укажите файл после ID')
            bad_options = [option for option in parts[4:] if option not in ('--dry', '--packets', '--own-ui')]
            if bad_options:
                raise ValueError('неизвестные параметры: ' + ', '.join(bad_options))
            mode, path, armed, packets, own_ui = cmd, resolve_script(parts[3]), '--dry' not in parts[4:], '--packets' in parts[4:], '--own-ui' in parts[4:]
            if current and current.running: raise ValueError('сначала остановите текущий Lua-скрипт')
            if mode == 'bot' and ((bot.session is not None and getattr(bot.session, 'connected', False)) or
                                  (getattr(bot, 'thread', None) is not None and bot.thread.is_alive())):
                raise ValueError('режим bot запускается до подключения; остановите аккаунт')
        else:
            raise ValueError('неизвестная команда /lua; используйте /lua help')
        runtime = LuaRuntime(bot, path, mode=mode, armed=armed, packets=packets, own_ui=own_ui)
        # Suppress automatic spawn before Lua is subscribed and before network start.
        bot.lua_spawn_control = mode == 'bot'
        bot.lua_ui_control = runtime.own_ui
        bot.lua_runtime = runtime
        try:
            runtime.start()
            if mode == 'bot':
                if getattr(bot, 'active_task', None):
                    bot.active_task.stop(); bot.active_task = None
                if not bot.start(): raise RuntimeError('запуск аккаунта отклонён; проверьте лог')
        except Exception:
            runtime.stop()
            bot.lua_runtime = None
            bot.lua_spawn_control = False
            bot.lua_ui_control = False
            raise
        print(f'[LUA] {mode}: {os.path.basename(path)} запущен для #{bot.bot_id}; ' + ('холостой ход' if not armed else 'действия включены'))
    except (ValueError, OSError, LuaScriptError, RuntimeError) as exc:
        print(f'[LUA] {exc}')
