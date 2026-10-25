"""Мост между веб-панелью и живым процессом бота.

Панель — отдельный процесс на своём порту. Она НЕ импортирует bot.py и не
поднимает второй менеджер: второй менеджер полез бы в accounts.json и в те
же аккаунты, а два клиента на одном нике — это кик «уже в сети» и драка за
файл реестра.

Вместо этого панель кладёт JSON-команды в очередь, а бот забирает их своим
потоком и исполняет теми же методами менеджера, которыми пользуется
Telegram-контроллер. Запущенные сессии при этом не трогаются: команда либо
касается конкретного бота, либо это чтение.

Файлы:

    _web/inbox/   <id>.json   — команды, ждущие исполнения
    _web/outbox/  <id>.json   — ответы на них
    _web/state.json           — снимок состояния (пишет бот, читает панель)

Команда:
    {"id": "...", "cmd": "list", "args": {...}, "ts": 1234567.8}

Ответ:
    {"id": "...", "ok": true, "result": ..., "error": null}
"""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
import uuid

BASE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_web"
)
INBOX = os.path.join(BASE_DIR, "inbox")
OUTBOX = os.path.join(BASE_DIR, "outbox")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

# Сколько живёт файл команды, прежде чем считаться мусором.
STALE_SECONDS = 120.0
# Как часто бот проверяет очередь и обновляет снимок.
POLL_INTERVAL = 0.5
SNAPSHOT_INTERVAL = 2.0


def _ensure_dirs() -> None:
    for path in (BASE_DIR, INBOX, OUTBOX):
        os.makedirs(path, exist_ok=True)


def _write_atomic(path: str, payload: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def submit(cmd: str, args: dict | None = None, timeout: float = 20.0) -> dict:
    """Положить команду в очередь и дождаться ответа. Используется панелью."""
    _ensure_dirs()
    cmd_id = uuid.uuid4().hex
    path = os.path.join(INBOX, f"{cmd_id}.json")
    _write_atomic(path, {
        "id": cmd_id,
        "cmd": cmd,
        "args": args or {},
        "ts": time.time(),
    })

    reply_path = os.path.join(OUTBOX, f"{cmd_id}.json")
    deadline = time.monotonic() + max(1.0, timeout)
    while time.monotonic() < deadline:
        if os.path.exists(reply_path):
            try:
                with open(reply_path, encoding="utf-8") as fh:
                    reply = json.load(fh)
            except Exception:
                time.sleep(0.05)
                continue
            try:
                os.unlink(reply_path)
            except OSError:
                pass
            return reply
        time.sleep(0.05)

    try:
        os.unlink(path)
    except OSError:
        pass
    return {"id": cmd_id, "ok": False, "result": None,
            "error": "бот не ответил за отведённое время"}


class WebBridge:
    """Сторона бота: читает очередь, исполняет, пишет снимок состояния."""

    def __init__(self, manager, *, log=None):
        self.manager = manager
        self.log = log or (lambda _m: None)
        self.running = False
        self._thread = None
        self._last_snapshot = 0.0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.running:
            return
        _ensure_dirs()
        self._sweep()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="web-bridge")
        self._thread.start()
        self.log(f"[WEB] мост панели запущен, очередь {INBOX}")

    def stop(self) -> None:
        self.running = False

    def _sweep(self) -> None:
        """Убрать протухшие файлы: панель могла умереть, не дождавшись."""
        now = time.time()
        for folder in (INBOX, OUTBOX):
            try:
                names = os.listdir(folder)
            except OSError:
                continue
            for name in names:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(folder, name)
                try:
                    if now - os.path.getmtime(path) > STALE_SECONDS:
                        os.unlink(path)
                except OSError:
                    pass

    def _loop(self) -> None:
        while self.running:
            try:
                self._drain()
                if time.time() - self._last_snapshot >= SNAPSHOT_INTERVAL:
                    self._last_snapshot = time.time()
                    self._snapshot()
            except Exception as exc:
                self.log(f"[WEB] мост: {exc!r}")
            time.sleep(POLL_INTERVAL)

    def _drain(self) -> None:
        try:
            names = sorted(os.listdir(INBOX))
        except OSError:
            return
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(INBOX, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    request = json.load(fh)
            except Exception:
                # Недописанный или битый файл: не трогаем, уберёт sweep.
                continue
            try:
                os.unlink(path)
            except OSError:
                continue

            cmd_id = request.get("id") or name[:-5]
            try:
                result = self._execute(request.get("cmd"), request.get("args") or {})
                reply = {"id": cmd_id, "ok": True, "result": result, "error": None}
            except Exception as exc:
                reply = {"id": cmd_id, "ok": False, "result": None,
                         "error": f"{exc}"}
                self.log(f"[WEB] команда {request.get('cmd')} упала: "
                         f"{traceback.format_exc(limit=3)}")
            try:
                _write_atomic(os.path.join(OUTBOX, f"{cmd_id}.json"), reply)
            except OSError as exc:
                self.log(f"[WEB] ответ не записался: {exc!r}")

    # --- исполнение ---------------------------------------------------

    def _execute(self, cmd: str, args: dict):
        handler = getattr(self, f"_cmd_{cmd}", None)
        if handler is None:
            raise ValueError(f"неизвестная команда: {cmd}")
        with self._lock:
            return handler(args)

    def _cmd_ping(self, args):
        return {"pong": True, "pid": os.getpid()}

    def _cmd_list(self, args):
        return {"bots": self._bots_payload()}

    def _cmd_start(self, args):
        bot_id = int(args["id"])
        bot = self.manager.bots.get(bot_id)
        if bot is None:
            raise ValueError(f"бот #{bot_id} не найден")
        self.manager.start_bot(bot_id)
        return {"started": bot_id}

    # --- пачки -------------------------------------------------------
    #
    # Панель умеет выделять несколько аккаунтов. Каждая команда ниже
    # принимает список id и запускает работу в своём потоке: мост живёт
    # в потоке бота, и ждать в нём полсотни подключений нельзя — панель
    # отвалилась бы по таймауту, а бот перестал бы отвечать на всё
    # остальное.

    def _ids(self, args) -> list[int]:
        raw = args.get("ids")
        if raw is None and args.get("id") is not None:
            raw = [args["id"]]
        if not isinstance(raw, (list, tuple)):
            raise ValueError("нужен список ids")
        ids = []
        for value in raw:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if not ids:
            raise ValueError("пустой список ids")
        return ids

    def _bulk(self, ids: list[int], action: str, name: str) -> dict:
        known = [i for i in ids if i in self.manager.bots]

        def run():
            for bot_id in known:
                try:
                    action(bot_id)
                except Exception as exc:
                    self.log(f"[WEB] {name} #{bot_id}: {exc!r}")

        threading.Thread(target=run, daemon=True,
                         name=f"web-bulk-{name}").start()
        return {"queued": known, "count": len(known),
                "skipped": [i for i in ids if i not in known]}

    def _cmd_start_many(self, args):
        ids = self._ids(args)
        return self._bulk(ids, self.manager.start_bot, "start")

    def _cmd_stop_many(self, args):
        ids = self._ids(args)
        return self._bulk(ids, self.manager.stop_bot, "stop")

    def _cmd_delete_many(self, args):
        ids = self._ids(args)
        return self._bulk(ids, self.manager.delete_bot, "delete")

    def _cmd_stats(self, args):
        """Обновить уровень и опыт через /mm. Работает в фоне."""
        from logic import stats as stats_mod

        ids = self._ids(args) if (args.get("ids") or args.get("id")) else [
            b.bot_id for b in list(self.manager.bots.values())
        ]
        queued = []
        for bot_id in ids:
            bot = self.manager.bots.get(bot_id)
            if bot is None or not bot.session or not bot.session.connected:
                continue

            def run(target=bot):
                try:
                    stats_mod.refresh(target, log=self.log)
                except Exception as exc:
                    self.log(f"[WEB] статистика #{target.bot_id}: {exc!r}")

            threading.Thread(target=run, daemon=True,
                             name=f"web-stats-{bot_id}").start()
            queued.append(bot_id)
        return {"queued": queued, "count": len(queued)}

    def _cmd_real_world(self, args):
        """Вывести ботов в общий мир: спавн плюс проверка стрима."""
        from logic import real_world

        ids = self._ids(args) if (args.get("ids") or args.get("id")) else [
            b.bot_id for b in list(self.manager.bots.values())
        ]
        queued = []
        for bot_id in ids:
            bot = self.manager.bots.get(bot_id)
            if bot is None or not bot.session or not bot.session.connected:
                continue
            real_world.ensure_async(bot, log=self.log)
            queued.append(bot_id)
        return {"queued": queued, "count": len(queued)}


    def _cmd_stop(self, args):
        bot_id = int(args["id"])
        bot = self.manager.bots.get(bot_id)
        if bot is None:
            raise ValueError(f"бот #{bot_id} не найден")
        self.manager.stop_bot(bot_id)
        return {"stopped": bot_id}

    def _cmd_restart(self, args):
        bot_id = int(args["id"])
        self.manager.stop_bot(bot_id)
        time.sleep(1.0)
        self.manager.start_bot(bot_id)
        return {"restarted": bot_id}

    def _cmd_goto(self, args):
        bot_id = int(args["id"])
        bot = self.manager.bots.get(bot_id)
        if bot is None or not bot.api or not bot.session or not bot.session.connected:
            raise ValueError(f"бот #{bot_id} не подключён")
        x, y, z = (float(args[k]) for k in ("x", "y", "z"))
        mode = str(args.get("mode") or "walk")
        movement_id = bot.api.MoveToCoord(x, y, z, mode=mode)
        return {"movement_id": movement_id, "target": [x, y, z], "mode": mode}

    def _cmd_delta_goto(self, args):
        import threading
        from logic.delta_teleport import DeltaTeleport, SAFE_SPEED, step_for_speed

        bot_id = int(args["id"])
        bot = self.manager.bots.get(bot_id)
        if bot is None or not bot.api or not bot.session or not bot.session.connected:
            raise ValueError(f"бот #{bot_id} не подключён")
        target = tuple(float(args[k]) for k in ("x", "y", "z"))
        session = bot.session
        engine = DeltaTeleport(
            session.players.bot,
            bot.api._delta_sync_sender,
            step=step_for_speed(SAFE_SPEED, 0.02),
            interval=0.02,
            velocity_mode="consistent",
            jitter=0.0,
            settle_packets=1,
            abort_check=lambda: not session.connected,
        )
        def run():
            was_enabled = bool(getattr(session.sync, "enabled", False))
            if was_enabled:
                session.sync.stop()
            try:
                engine.run(target)
            finally:
                if was_enabled and session.connected:
                    session.sync.start()
        threading.Thread(target=run, daemon=True, name=f"agent-delta-goto-{bot_id}").start()
        return {"started": True, "target": list(target), "speed": SAFE_SPEED}

    def _cmd_unban(self, args):
        bot_id = int(args["id"])
        self.manager.unban_bot(bot_id)
        return {"unbanned": bot_id}

    def _cmd_delete(self, args):
        bot_id = int(args["id"])
        self.manager.delete_bot(bot_id)
        return {"deleted": bot_id}

    def _cmd_task(self, args):
        bot_id = int(args["id"])
        task = str(args.get("task") or "lvl")
        bot = self.manager.bots.get(bot_id)
        if bot is None:
            raise ValueError(f"бот #{bot_id} не найден")
        note = bot.set_task(task)
        self.manager.save_accounts()
        return {"task": task, "note": note}

    def _cmd_add(self, args):
        # "none" — это «своё IP». Без нормализации строка "none" уехала бы
        # в bot.proxy, и бот пытался бы подключиться к прокси с именем none.
        raw_proxy = str(args.get("proxy") or "").strip()
        node_key = str(args.get("node") or "").strip()
        if node_key:
            pool = getattr(self.manager, "proxy_pool", None)
            node = pool.find_node(node_key) if pool else None
            if node is None:
                raise ValueError("узел не найден")
            raw_proxy = pool.socks_url_for(node) or ""
            if not raw_proxy:
                raise ValueError(f"{node.name}: узел не поднялся (нужен мост)")
        if raw_proxy.lower() in ("none", "off", "direct", "-"):
            raw_proxy = ""
        bot = self.manager.add_bot(
            str(args["server"]),
            str(args.get("nickname") or ""),
            str(args.get("password") or ""),
            default_task=str(args.get("task") or "lvl"),
            proxy=raw_proxy,
        )
        if bot is None:
            raise ValueError("аккаунт не создан — проверь сервер и пароль")
        return {"id": bot.bot_id, "nickname": bot.nickname,
                "server": bot.server_name}

    def _cmd_proxy_bot(self, args):
        bot_id = int(args["id"])
        proxy = str(args.get("proxy") or "")
        ok = self.manager.set_bot_proxy(bot_id, proxy)
        if not ok:
            raise ValueError("прокси не годится")
        return {"id": bot_id, "proxy": proxy}

    def _cmd_proxy_all(self, args):
        proxy = str(args.get("proxy") or "")
        result = self.manager.apply_proxy_to_bots(
            proxy, bot_ids=args.get("ids") or None)
        return result

    def _cmd_goal_many(self, args):
        """Цель по уровню пачке аккаунтов. level=0 снимает цель."""
        ids = self._ids(args)
        level = args.get("level")
        if level is None:
            level = 0
        changed = self.manager.set_target_level(ids, level)
        return {"changed": changed, "count": len(changed), "level": int(level or 0)}

    def _cmd_add_many(self, args):
        """Создать партию аккаунтов, сразу с прокси.

        Прокси берётся одним из трёх способов:
        proxy — конкретная строка или имя сохранённого (на всех);
        node — узел пула (на всех, через мост);
        пусто и spread=true — рабочие узлы вразброс, как при запуске.
        """
        count = int(args.get("count") or 1)
        if count < 1:
            raise ValueError("количество должно быть больше нуля")
        server = str(args.get("server") or "").strip()
        if not server:
            raise ValueError("не указан сервер")

        proxies: list[str] = []
        pool = getattr(self.manager, "proxy_pool", None)
        node_key = str(args.get("node") or "").strip()
        raw = str(args.get("proxy") or "").strip()

        if node_key:
            node = pool.find_node(node_key) if pool else None
            if node is None:
                raise ValueError("узел не найден")
            url = pool.socks_url_for(node) or ""
            if not url:
                raise ValueError(f"{node.name}: узел не поднялся (нужен мост)")
            proxies = [url] * count
        elif raw and raw not in ("none", "spread"):
            proxies = [raw] * count
        elif raw == "spread" or args.get("spread"):
            if pool is not None:
                try:
                    spread = pool.rotate(count)
                except Exception:
                    spread = []
                proxies = [item for item in spread if item]

        created = self.manager.add_bots_batch(
            server, count,
            password=str(args.get("password") or ""),
            default_task=str(args.get("task") or "lvl"),
            proxies=proxies or None,
        )
        ok = [b for b in created if getattr(b, "nickname", None)]
        return {
            "count": len(ok),
            "ids": [b.bot_id for b in ok],
            "with_proxy": len([b for b in ok if getattr(b, "proxy", "")]),
        }

    def _cmd_proxy_many(self, args):
        """Прокси на выбранные аккаунты.

        Принимает либо готовую строку (proxy), либо ключ узла из пула
        (node). Во втором случае узел проходит через socks_url_for —
        для VLESS/VMess он поднимает мост и отдаёт локальный socks5.
        """
        ids = self._ids(args)
        raw = str(args.get("proxy") or "").strip()
        node_key = str(args.get("node") or "").strip()
        pool = getattr(self.manager, "proxy_pool", None)

        if node_key:
            node = pool.find_node(node_key) if pool else None
            if node is None:
                raise ValueError("узел не найден")
            raw = pool.socks_url_for(node) or ""
            if not raw:
                raise ValueError(
                    f"{node.name}: узел не поднялся (нужен мост sing-box)")
        if not raw:
            raise ValueError("не указан прокси")

        def run():
            for bot_id in ids:
                try:
                    self.manager.set_bot_proxy(bot_id, raw)
                except Exception as exc:
                    self.log(f"[WEB] proxy #{bot_id}: {exc!r}")

        threading.Thread(target=run, daemon=True,
                         name="web-bulk-proxy").start()
        return {"queued": ids, "count": len(ids)}

    def _cmd_vpn_status(self, args):
        return self._vpn_payload()

    def _cmd_vpn_refresh(self, args):
        pool = getattr(self.manager, "proxy_pool", None)
        if pool is None:
            raise ValueError("пул прокси не запущен")
        pool.refresh()
        return self._vpn_payload()

    def _cmd_vpn_use(self, args):
        pool = getattr(self.manager, "proxy_pool", None)
        if pool is None:
            raise ValueError("пул прокси не запущен")
        key = str(args["key"])
        node = pool.find_node(key)
        if node is None:
            raise ValueError("узел не найден")
        url = pool.socks_url_for(node)
        if not url:
            raise ValueError(f"{node.name}: узел нерабочий или мост не поднялся")
        return self.manager.apply_proxy_to_bots(url)

    def _cmd_vpn_sub_add(self, args):
        pool = getattr(self.manager, "proxy_pool", None)
        if pool is None:
            raise ValueError("пул прокси не запущен")
        name = str(args.get("name") or "подписка")
        url = str(args["url"])
        nodes = pool.add_subscription(url, name)
        return {"added": name, "nodes": len(nodes)}

    def _cmd_settings(self, args):
        settings = getattr(self.manager, "settings", None)
        if settings is None:
            raise ValueError("настройки недоступны")
        if args:
            if args.get("base_password"):
                settings.base_password = str(args["base_password"])
            if args.get("default_task"):
                settings.default_task = str(args["default_task"])
        return {"base_password": settings.password_or(),
                "default_task": settings.default_task}

    # --- снимки -------------------------------------------------------

    def _bots_payload(self) -> list:
        from logic import stats as stats_mod

        bots = []
        for bot in list(self.manager.bots.values()):
            try:
                pos = bot.api.getbotposition() if bot.api else None
            except Exception:
                pos = None
            task = getattr(bot.active_task, "name", None) if bot.active_task else None
            level = stats_mod.snapshot(bot)
            # Куда движок качки поставил бота: панель и Telegram показывают
            # вокзал и точку, а не просто «качается».
            lvl = {}
            try:
                lvl = {
                    "station": getattr(bot, "lvl_spawn_name", None),
                    "target": getattr(bot, "lvl_target", None),
                    "target_index": getattr(bot, "lvl_target_index", None),
                    "phase": getattr(bot, "lvl_phase", None),
                    "report": getattr(bot.active_task, "report", None)
                    if bot.active_task else None,
                }
            except Exception:
                lvl = {}
            bots.append({
                "id": bot.bot_id,
                "nickname": bot.nickname,
                "server": bot.server_name,
                "status": bot.status,
                "banned": bool(getattr(bot, "is_banned", False)),
                "level": level["level"],
                "level_num": level["level_num"],
                "exp_cur": level["exp_cur"],
                "exp_next": level["exp_next"],
                "exp_left": level["exp_left"],
                "exp_percent": level["exp_percent"],
                "stats_updated_at": level["stats_updated_at"],
                "money": getattr(bot, "money", 0),
                "task": task or bot.default_task_name or "нет",
                "proxy": getattr(bot, "proxy", "") or "",
                "position": list(pos) if pos else None,
                "lvl": lvl,
                "logs": list(getattr(bot, "logs", []) or [])[-8:],
            })
        bots.sort(key=lambda b: b["id"])
        return bots

    def _vpn_payload(self) -> dict:
        pool = getattr(self.manager, "proxy_pool", None)
        if pool is None:
            return {"available": False, "nodes": [], "summary": {}}
        nodes = []
        summary = {}
        try:
            statuses = pool.statuses()
        except Exception:
            statuses = []
        for res in statuses:
            node = res.node
            nodes.append({
                "key": node.key,
                "name": node.name,
                "protocol": node.protocol,
                "address": f"{node.address}:{node.port}",
                "usable": bool(res.usable),
                "alive": bool(res.alive),
                "status": getattr(res, "status", ""),
            })
        try:
            summary = pool.summary()
        except Exception:
            summary = {
                "total": len(nodes),
                "usable": sum(1 for n in nodes if n["usable"]),
            }
        # Сохранённые вручную прокси: панель строит из них выпадашку
        # «применить к выбранным», чтобы не вводить строку каждый раз.
        saved = []
        for item in (getattr(pool, "manual", None) or []):
            try:
                saved.append({
                    "name": str(item.get("name") or ""),
                    "proxy": str(item.get("proxy") or ""),
                })
            except Exception:
                continue
        return {"available": True, "nodes": nodes, "summary": summary,
                "saved": saved}

    def _snapshot(self) -> None:
        """Снимок для панели, когда её нет или бот занят."""
        try:
            payload = {
                "ts": time.time(),
                "bots": self._bots_payload(),
                "vpn": self._vpn_payload(),
            }
            _write_atomic(STATE_FILE, payload)
        except Exception as exc:
            self.log(f"[WEB] снимок не записался: {exc!r}")
