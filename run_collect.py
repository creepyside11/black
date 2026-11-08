"""Боевой запуск: сбор предметов на Нижегородске по кругу.

    python3 run_collect.py --id 1                 # холостой ход
    python3 run_collect.py --id 1 --armed         # реально поднимать

Полный цикл:

    1. Вход в игру. Спавн — ТОЛЬКО через меню (logic.spawn). RPC-хендшейк
       сервер наказывает киком античита кодом 25.
    2. Проверка спавна. Годится только два случая:
           - бот уже на площадке сбора (аккаунт вошёл на сохранённую
             позицию — так и было в записи route2.pcap);
           - бот на Нижегородске 1 или 2, откуда его везут на площадку.
       Любой другой вокзал, зона авторизации, небо или нули — НЕ тот
       спавн. Тогда бот выходит из игры, ждёт WRONG_SPAWN_WAIT секунд и
       заходит снова. И так до попадания.
    3. На площадке — круг из пяти точек: телепорт -> RPC 413 тело 18 ->
       ждём уведомление -> следующая. Круг 249 м.
    4. Круг повторяется: предметы на земле появляются снова.
    5. Выкинуло в мир (респавн) — ждём RESPAWN_WAIT секунд, спавнимся
       заново и возвращаемся на площадку.

Телепорт — delta-метод на SAFE_SPEED (8.5 м/с): позиция и velocity в
пакете сходятся, античит видит обычный бег. Быстрее сервер откатывает
на доверенную точку, и бот остаётся на месте.

Холостой ход ничего не отправляет: бот ходит и пишет, что отправил бы.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.collect_route import (
    WORK_ANCHOR, WORK_POINTS, describe_route, needs_travel,
    nearest_work_point, station_summary,
)
from logic.events import emitter
from logic.ground_pickup import GroundPickup
from logic.spawn import SPAWNS, ensure_spawn, is_spawned_position
from logic.delta_teleport import SAFE_SPEED

TICK = 0.3                 # пауза между шагами автомата
WAIT_INGAME = 240.0        # сколько ждать входа в игру
RESPAWN_WAIT = 120.0       # «от двух минут» после респавна в мире
WRONG_SPAWN_WAIT = 60.0    # «через минуту» при неверном спавне
ROUND_PAUSE = 3.0          # пауза между кругами
# Больше стольких перезаходов подряд не делаем: сервер уже обратил на
# аккаунт внимание, и продолжать — значит сжечь его в бан.
MAX_REENTRY = 8
# Скорость телепорта — ровно та, что требует владелец: «8 м/с». Берём её
# из самого модуля перелёта (logic.delta_teleport.SAFE_SPEED = 8.0), а не
# своим числом: два независимых значения уже разъезжались (8.5 здесь
# против 8.0 там), и лаунчер телепортировал быстрее движка.
# test_collect_route держит это равенство.
TP_SPEED = SAFE_SPEED
TELEPORT_CHUNK = 350.0     # метров за один вызов: больше — бот не едет

# Вокзал при входе сервер выдаёт случайно. Годится любой, кроме Южного.
# Нижегородск указан первым только для наглядности — с него ближе всего.
ALLOWED_STATIONS = ("Нижегородск 1", "Нижегородск 2")
DENIED_STATIONS = ("Южный",)


def find_bot(manager, bot_id=None, nick=None):
    """Найти бота по номеру или нику. Ник — без учёта регистра."""
    bots = getattr(manager, "bots", {}) or {}
    if bot_id is not None:
        return bots.get(int(bot_id))
    if nick:
        want = str(nick).strip().lower()
        for b in bots.values():
            if str(getattr(b, "nickname", "")).strip().lower() == want:
                return b
        for b in bots.values():
            if want in str(getattr(b, "nickname", "")).strip().lower():
                return b
    return None


def log_to(bot, tag="СБОР"):
    def _log(msg):
        line = f"[{tag}] {msg}"
        try:
            bot.add_log(line)
        except Exception:
            pass
        print(line, flush=True)

    return _log


def acceptable_spawn(pos, *, anchor=WORK_ANCHOR, radius=300.0):
    """Годится ли то, куда нас поставило. Возвращает (да/нет, почему).

    Вокзал сервер выдаёт случайно, поэтому подходит любой — везём
    телепортом откуда угодно. Не берём только Южный и «не в мире»:
    зона авторизации, камера кинематика, нули.
    """
    if not is_spawned_position(pos):
        return False, "не в мире (зона авторизации, небо или нули)"
    info = station_summary(pos)
    if not needs_travel(pos, anchor=anchor, radius=radius):
        return True, f"уже на площадке ({info['to_anchor']:.0f} м от центра)"
    if info["station"] in DENIED_STATIONS:
        return False, f"{info['station']} не берём ({info['to_anchor']:.0f} м от центра)"
    return True, f"вокзал {info['station']}, ехать {info['to_anchor']:.0f} м"


def is_banned(bot) -> bool:
    """Помечен ли аккаунт забаненным — своим флагом или статусом."""
    if getattr(bot, "is_banned", False):
        return True
    status = str(getattr(bot, "status", "") or "").upper()
    return "ЗАБАНЕН" in status or "BANNED" in status


def wait_in_game(bot, *, timeout=WAIT_INGAME):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if "В игре" in str(getattr(bot, "status", "")):
            return True
        time.sleep(1.0)
    return False


def _tp_tag(res) -> str:
    """Статус и причину перелёта — в лог.

    Без reason строка «сдвиг 0 м [aborted]» ничего не объясняет: это либо
    «сервер вернул позицию назад» (server_reset), либо «сессия отвалилась
    ещё до первого пакета» (session_down). Разница принципиальная — в одном
    случае виноват античит на движение, в другом бота выкинули до перелёта.
    """
    if not isinstance(res, dict):
        return f" [{res}]" if res else ""
    status = res.get("status")
    reason = res.get("reason")
    if not status:
        return ""
    return f" [{status}/{reason}]" if reason else f" [{status}]"


def make_travel(log, speed=TP_SPEED, chunk=TELEPORT_CHUNK):
    """Перелёт delta-методом, кусками.

    Одним вызовом на 2 км бот не едет: DeltaTeleportTo возвращает статус,
    а персонаж остаётся на месте (проверено — промах 2084 м из 2084).
    В _diag/agent.py ровно поэтому режут по 350 м. Здесь так же, плюс
    сверка позиции после каждого куска и повтор, если сервер откатил.
    """

    def _dist(a, b):
        return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5

    def go(bot, target):
        x, y, z = (float(v) for v in target)
        start = bot.api.getbotposition()
        if start:
            log(f"телепорт -> ({x:.1f}, {y:.1f}, {z:.1f}), {_dist(start, (x, y, z)):.0f} м")

        stuck = 0
        for hop_i in range(1, 41):
            cur = bot.api.getbotposition()
            if not cur or len(cur) < 3:
                log("    нет позиции — перелёт прерван")
                return None
            left = _dist(cur, (x, y, z))
            if left <= 2.0:
                break

            if left > chunk:
                k = chunk / left
                hop = (cur[0] + (x - cur[0]) * k,
                       cur[1] + (y - cur[1]) * k,
                       cur[2] + (z - cur[2]) * k)
            else:
                hop = (x, y, z)

            try:
                res = bot.api.DeltaTeleportTo(hop[0], hop[1], hop[2], speed=speed)
            except Exception as exc:
                log(f"    кусок {hop_i} упал: {exc!r}")
                res = None
            time.sleep(0.4)

            now = bot.api.getbotposition()
            moved = _dist(cur, now) if now and len(now) >= 3 else 0.0
            log(f"    кусок {hop_i}: дошёл до ({hop[0]:.0f}, {hop[1]:.0f}, {hop[2]:.0f}) "
                f"-> сдвиг {moved:.0f} м, осталось {_dist(now, (x, y, z)):.0f} м"
                + _tp_tag(res))

            if moved < 1.0:
                stuck += 1
                if stuck >= 3:
                    log("    три куска подряд без сдвига — бросаю перелёт")
                    return bot.api.getbotposition()
            else:
                stuck = 0

        after = bot.api.getbotposition()
        if after and len(after) >= 3:
            log(f"    итог ({after[0]:.1f}, {after[1]:.1f}, {after[2]:.1f}), "
                f"промах {_dist(after, (x, y, z)):.1f} м")
        return after

    return go


def make_walk_travel(log, timeout=900.0):
    """Пеший перелёт: медленно и по-человечески.

    Живой клиент в записи route2.pcap шёл 3.3 м/с. Delta-телепорт шлёт
    позицию 50 раз в секунду и на этом палится. Ходьба — запасной путь,
    если телепорт стабильно приводит к обрыву.
    """

    def go(bot, target):
        x, y, z = (float(v) for v in target)
        before = bot.api.getbotposition()
        if before and len(before) >= 3:
            d = ((before[0] - x) ** 2 + (before[1] - y) ** 2 + (before[2] - z) ** 2) ** 0.5
            log(f"иду пешком -> ({x:.1f}, {y:.1f}, {z:.1f}), {d:.0f} м "
                f"(это около {d / 3.3 / 60:.0f} мин)")
        try:
            bot.api.MoveToCoord(x, y, z, mode="walk", completion_distance=1.0)
        except Exception as exc:
            log(f"    ходьба не удалась: {exc!r}")
        after = bot.api.getbotposition()
        if after and len(after) >= 3:
            left = ((after[0] - x) ** 2 + (after[1] - y) ** 2 + (after[2] - z) ** 2) ** 0.5
            log(f"    пришёл ({after[0]:.1f}, {after[1]:.1f}, {after[2]:.1f}), "
                f"не дошёл {left:.1f} м")
        return after

    return go


def reenter(bot, log, *, wait=WRONG_SPAWN_WAIT, direct=False):
    """Выйти из игры, подождать и зайти снова.

    Через manager.stop_bot/start_bot ходить НЕЛЬЗЯ: start_bot тянет за
    собой весь пул (в прогоне он поднял 125 аккаунтов). Бот стартует сам,
    своим объектом, и прокси остаётся тем, с которым мы вошли.
    """
    log(f"выхожу из игры, вернусь через {wait:.0f} с")
    try:
        bot.stop()
    except Exception as exc:
        log(f"остановка не удалась: {exc!r}")

    # start() проверяет прокси и откажется стартовать с мёртвым.
    if direct and getattr(bot, "proxy", ""):
        bot.proxy = ""
        bot.proxy_node = ""

    time.sleep(max(0.0, wait))
    try:
        ok = bot.start()
    except Exception as exc:
        log(f"запуск не удался: {exc!r}")
        return False
    if ok is False:
        log("старт отклонён (бан или мёртвый прокси)")
        return False
    if not wait_in_game(bot):
        log(f"не вошёл за {WAIT_INGAME:.0f} с")
        return False
    log("снова в игре")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="сбор предметов по кругу")
    ap.add_argument("--id", type=int, default=None, help="номер бота из accounts.json")
    ap.add_argument("--nick", default=None, help="ник бота (если номер не помнишь)")
    ap.add_argument("--proxy", default=None,
                    help="свой прокси, например socks5://127.0.0.1:10808")
    ap.add_argument("--no-proxy", action="store_true",
                    help="идти напрямую, без прокси аккаунта (сервер отвечает "
                         "IP-баном, кик 999)")
    ap.add_argument("--armed", action="store_true",
                    help="РЕАЛЬНО поднимать (по умолчанию холостой ход)")
    ap.add_argument("--speed", type=float, default=TP_SPEED, help="скорость телепорта, м/с")
    ap.add_argument("--travel", choices=("teleport", "walk"), default="teleport",
                    help="как добираться: delta-телепорт (быстро, палевно) "
                         "или пешком (медленно, как живой клиент)")
    ap.add_argument("--respawn-wait", type=float, default=RESPAWN_WAIT,
                    help="сколько ждать после респавна в мире, с")
    ap.add_argument("--wrong-spawn-wait", type=float, default=WRONG_SPAWN_WAIT,
                    help="сколько ждать перед повторным входом при чужом спавне, с")
    ap.add_argument("--loops", type=int, default=0, help="сколько кругов, 0 — бесконечно")
    ap.add_argument("--mode", choices=("collect", "idle"), default="collect",
                    help="collect — боевой сбор по кругу; idle — просто стою "
                         "на спавне и чуть шевелюсь, ничего не собираю")
    args = ap.parse_args()
    if args.id is None and not args.nick:
        ap.error("нужен --id или --nick")

    print("[*] Круг сбора:")
    print(describe_route())
    for name in ALLOWED_STATIONS:
        p = SPAWNS.get(name)
        if p:
            d = ((p[0] - WORK_ANCHOR[0]) ** 2 + (p[1] - WORK_ANCHOR[1]) ** 2) ** 0.5
            print(f"    {name}: {d:.0f} м до площадки")
    print()
    if not args.armed:
        print("[!] ХОЛОСТОЙ ХОД. Пакеты не отправляются. Для боя: --armed")

    from bot import BotManager

    manager = BotManager()

    # Менеджер — многоботовый: его сторож прокси и обработчик IP-бана
    # (кик 999) переписывают accounts.json через save_accounts(). В прогоне
    # это уже стирало прокси аккаунта из файла. Наш скрипт ведёт ОДНОГО
    # бота и файл трогать не должен. Заморозка теперь штатная и логируемая:
    # попытка записи оставляет след в логе, а не исчезает молча.
    manager.freeze_accounts("одноразовый прогон run_collect: реестр только на чтение")

    bot = find_bot(manager, args.id, args.nick)
    if bot is None:
        who = f"#{args.id}" if args.id is not None else repr(args.nick)
        print(f"[!] Нет бота {who} в accounts.json")
        return 2
    bot_id = bot.bot_id

    if args.proxy:
        bot.proxy = args.proxy
        bot.proxy_node = ""
        # Метку «забанен» мог поставить обработчик кика 999, и тогда пул
        # не отдаёт живой прокси. Раз прокси задан руками — снимаем.
        try:
            pool = getattr(manager, "proxy_pool", None)
            if pool is not None and pool.is_banned(bot.proxy):
                pool.unban(bot.proxy)
                print(f"[*] Метка «забанен» снята с {bot.proxy}")
        except Exception as exc:
            print(f"[*] Метку снять не вышло: {exc!r}")
    elif args.no_proxy:
        print(f"[*] Прокси {bot.proxy or '—'} отключён — идём напрямую")
        bot.proxy = ""
        bot.proxy_node = ""
    print(f"[*] Идём через прокси {bot.proxy or '— НЕТ, напрямую'}")

    log = log_to(bot)
    manager.start_selected([bot_id])

    if not wait_in_game(bot):
        print(f"[!] Бот не вошёл за {WAIT_INGAME:.0f} с, статус: {bot.status}")
        manager.stop_all()
        return 3

    log(f"в игре: {getattr(bot, 'nickname', '?')} @ {getattr(bot, 'server_name', '?')}")

    stand = GroundPickup(
        bot,
        route=WORK_POINTS,
        mode="route",
        armed=bool(args.armed),
        travel=(make_walk_travel(log) if args.travel == "walk"
                else make_travel(log, args.speed)),
        log=log,
    )
    # on() — декоратор и падает на связанном методе; subscribe умеет.
    emitter.subscribe("onReceiveJSON", stand.on_receive_json, sync=True)
    emitter.subscribe("onReceiveRPC", stand.on_receive_rpc, sync=True)

    # --- спавн: свой вокзал или перезаход --------------------------------
    # Вокзал при входе сервер выдаёт СЛУЧАЙНО, поэтому не подбираем пункт
    # меню, а просто перезаходим, пока не повезёт с Нижегородском.
    spawn_ok = False
    for attempt in range(1, MAX_REENTRY + 1):
        ensure_spawn(bot, timeout=90.0, log=log)
        pos = bot.api.getbotposition()
        ok, why = acceptable_spawn(pos)
        log(f"спавн: {why}")
        if ok:
            spawn_ok = True
            break
        if is_banned(bot):
            log("аккаунт помечен забаненным — останавливаюсь, чтобы не жечь его дальше")
            try:
                bot.stop()
            except Exception:
                pass
            return 5
        log(f"не тот вокзал (попытка {attempt} из {MAX_REENTRY}) — выхожу и захожу снова")
        if not reenter(bot, log, wait=args.wrong_spawn_wait):
            log("перезайти не вышло, жду и пробую ещё раз")
            time.sleep(args.wrong_spawn_wait)

    if not spawn_ok:
        log(f"за {MAX_REENTRY} попыток нужный вокзал не выпал — "
            f"останавливаюсь, чтобы не выжечь аккаунт")
        try:
            bot.stop()
        except Exception:
            pass
        return 4

    # --- режим присутствия: топчемся у вокзала и никуда не едем ------------
    if args.mode == "idle":
        from logic.idle_presence import IdlePresence, describe as describe_idle
        log(describe_idle())
        presence = IdlePresence(bot.api, log=log)
        presence.remember_anchor(bot.api.getbotposition())
        try:
            while True:
                if is_banned(bot):
                    log("аккаунт помечен забаненным — останавливаюсь, чтобы не жечь его")
                    break
                if not getattr(bot.session, "connected", False):
                    log("сессия оборвалась — перезаход в мир")
                    if not reenter(bot, log, wait=args.wrong_spawn_wait):
                        time.sleep(args.wrong_spawn_wait)
                        continue
                    ensure_spawn(bot, timeout=90.0, log=log)
                    presence = IdlePresence(bot.api, log=log)
                    presence.remember_anchor(bot.api.getbotposition())
                presence.run(max_steps=1)
        except KeyboardInterrupt:
            log("остановлено вручную")
        finally:
            try:
                bot.stop()
            except Exception:
                pass
        return 0

    stand.start()

    # --- первый перелёт на площадку -----------------------------------------
    pos = bot.api.getbotposition()
    if needs_travel(pos):
        _, target, _ = nearest_work_point(pos)
        log(f"везу на площадку: {station_summary(pos)['to_anchor']:.0f} м")
        try:
            stand.travel(bot, target)
        except Exception as exc:
            log(f"перелёт не удался: {exc!r}")

    loops_done = 0

    try:
        while True:
            pos = bot.api.getbotposition()

            # Выкинуло в мир или поставило не туда — ждём и возвращаемся.
            if not is_spawned_position(pos) or needs_travel(pos):
                info = station_summary(pos)
                ok, why = acceptable_spawn(pos)
                if not ok:
                    # Южный или вообще не в мире — перезаходим и спавнимся заново.
                    log(f"место не годится: {why} — перезаход")
                    if is_banned(bot):
                        log("аккаунт помечен забаненным — останавливаюсь")
                        break
                    if reenter(bot, log, wait=args.wrong_spawn_wait):
                        ensure_spawn(bot, timeout=90.0, log=log)
                    else:
                        time.sleep(args.wrong_spawn_wait)
                    pos = bot.api.getbotposition()
                    _, target, _ = nearest_work_point(pos)
                    try:
                        stand.travel(bot, target)
                    except Exception as exc:
                        log(f"перелёт не удался: {exc!r}")
                    stand.next_round()
                    continue
                log(f"не на площадке ({info['to_anchor']:.0f} м) — "
                    f"жду {args.respawn_wait:.0f} с")
                time.sleep(max(0.0, args.respawn_wait))
                ensure_spawn(bot, timeout=90.0, log=log)
                pos = bot.api.getbotposition()
                if not is_spawned_position(pos):
                    log("спавн не удался, жду ещё")
                    time.sleep(30.0)
                    continue
                _, target, _ = nearest_work_point(pos)
                try:
                    stand.travel(bot, target)
                except Exception as exc:
                    log(f"перелёт не удался: {exc!r}")
                stand.next_round()
                continue

            text = stand.step()
            if text and not text.startswith("иду"):
                log(text)

            if stand.state == "done":
                loops_done += 1
                log(f"круг {loops_done} закрыт: поднято {stand.grants}, "
                    f"без ответа {stand.misses}")
                if args.loops and loops_done >= args.loops:
                    break
                time.sleep(ROUND_PAUSE)
                stand.next_round()

            time.sleep(TICK)
    except KeyboardInterrupt:
        log("остановка по Ctrl-C")
        stand.stop("Ctrl-C")
    finally:
        print()
        print(stand.dump_journal())
        print()
        for k, v in stand.report().items():
            print(f"  {k}: {v}")
        print(f"  кругов: {loops_done}")
        try:
            manager.stop_all()
        except Exception:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
