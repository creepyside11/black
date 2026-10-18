"""Пробник выбора вокзала: какой формат ответа на iid=50 реально работает.

Сервер сам вокзал не выбирает — по умолчанию ставит рандомный или последнее
место. Вокзал выбирает клиент, отдавая номер в интерфейс 50. Формат ответа
надо выяснить на живом сервере: этот скрипт перебирает стили и индексы,
глушит наш собственный sync и watchdog, чтобы данные были чистые.

    python3 probe_spawn_select.py --bot Sha_Pismenskij --host 80.66.82.35

Печатает таблицу: стиль / индекс -> позиция, и вердикт по каждому стилю.
"""

import argparse
import json
import sys
import time

import bot as botmod
from bot import BotManager
from logic import spawn as S
from logic.events import emitter

# Свой watchdog не нужен: он бы двигал бота и портил замеры
botmod.BotInstance.begin_spawn_watchdog = lambda self: None

raw_iid50 = []
rpc_log = []


@emitter.on("onReceiveJSON")
def _on_json(bot_obj, iid, data, *a, **k):
    if iid == 50:
        raw_iid50.append(data)
        print(f"    << iid=50 {json.dumps(data, ensure_ascii=False)}", flush=True)


@emitter.on("onReceiveRPC")
def _on_rpc(bot_obj, rid, payload, *a, **k):
    if rid in (372, 448, 361, 441):
        rpc_log.append(rid)
        if rid in (372, 448):
            print(f"    << RPC {rid} len={len(payload) if payload else 0}", flush=True)


def wait_auth(bot, timeout=60.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if bot.is_banned:
            return False
        if bot.session and bot.session.connected and "авторизован" in bot.status:
            return True
        time.sleep(0.3)
    return False


def probe_style(bot, style, indexes, settle=2.5):
    """Один стиль: перебираем индексы, пока позиция не уйдёт из зоны."""
    results = []
    for idx in indexes:
        if not (bot.session and bot.session.connected):
            results.append((idx, None, "сессия упала"))
            break

        payload = S.build_spawn_payload(idx, style)
        bot.session.send_json(50, payload, packet_name="probe_spawn_select")
        time.sleep(settle)

        pos = bot.api.getbotposition() if bot.api else None
        in_zone = S.in_auth_zone(pos)
        note = "в зоне" if in_zone else "ВЫШЕЛ"
        results.append((idx, pos, note))

        shown = "—" if pos is None else f"({pos[0]:8.1f},{pos[1]:8.1f},{pos[2]:7.1f})"
        print(f"    idx={idx:<3} {json.dumps(payload)} -> {shown}  {note}", flush=True)

        if not in_zone:
            # Проверяем, что позиция держится, а не кинематик
            time.sleep(2.0)
            pos2 = bot.api.getbotposition() if bot.api else None
            if pos2 is not None and not S.in_auth_zone(pos2):
                print(f"    [+] ДЕРЖИТСЯ: ({pos2[0]:.1f},{pos2[1]:.1f},{pos2[2]:.1f})", flush=True)
                return results, idx, pos2
            print("    [-] откатило — это был кинематик", flush=True)
            results[-1] = (idx, pos2, "откатило")
    return results, None, None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bot", required=True)
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=5125)
    ap.add_argument("--indexes", default="0-11")
    ap.add_argument("--styles", default="t_s,t,s,t_s_r")
    ap.add_argument("--settle", type=float, default=2.5)
    args = ap.parse_args()

    if "-" in args.indexes:
        lo, hi = args.indexes.split("-")
        indexes = list(range(int(lo), int(hi) + 1))
    else:
        indexes = [int(x) for x in args.indexes.split(",")]

    styles = [s.strip() for s in args.styles.split(",") if s.strip()]

    manager = BotManager()
    bot = next((b for b in manager.bots.values() if b.nickname == args.bot), None)
    if bot is None:
        print(f"[-] Аккаунт {args.bot} не найден.")
        return 1

    if args.host:
        bot.host = args.host
        bot.port = args.port
        bot.server_name = f"{args.host}:{args.port}"

    bot.set_task("stop")
    print(f"[*] {bot.nickname} -> {bot.host}:{bot.port}", flush=True)
    bot.start()

    if not wait_auth(bot):
        print(f"[-] Авторизация не прошла: {bot.status}")
        bot.stop()
        return 1

    print(f"[+] авторизован, pos={bot.api.getbotposition()}", flush=True)

    # Свой sync только мешает: он шлёт координаты зоны авторизации
    bot.session.sync.stop()
    print("[*] sync выключен", flush=True)

    # Ждём приглашение выбрать вокзал
    for _ in range(40):
        if raw_iid50:
            break
        time.sleep(0.25)

    print(f"\n[*] iid=50 пришёл {len(raw_iid50)} раз", flush=True)
    for i, packet in enumerate(raw_iid50[:5]):
        print(f"    [{i}] {json.dumps(packet, ensure_ascii=False)}", flush=True)

    menu = S.available_spawns()
    if menu:
        print(f"[+] Сервер прислал список вокзалов: {menu}", flush=True)
        # Меню есть — перебираем его элементы вместо числовых индексов
        indexes = list(menu)
        print(f"[*] Перебираем вокзалы из меню: {indexes}", flush=True)
    else:
        print("[*] Списка вокзалов нет — перебираем числовые индексы", flush=True)

    winner = None
    for style in styles:
        if not (bot.session and bot.session.connected):
            print("[-] Сессия упала, дальше не пробуем.")
            break
        print(f"\n=== стиль '{style}' ===", flush=True)
        _res, hit, pos = probe_style(bot, style, indexes, settle=args.settle)
        if hit is not None:
            winner = (style, hit, pos)
            break
        # Возвращаем бота в зону авторизации перед следующим стилем
        bot.session.send_json(50, {"s": 0}, packet_name="probe_reset")
        time.sleep(1.0)

    print("\n" + "=" * 62)
    print(f"RPC от сервера: {sorted(set(rpc_log))}")
    print(f"iid=50 пакетов: {len(raw_iid50)}")
    if winner:
        style, idx, pos = winner
        print(f"РАБОТАЕТ: стиль '{style}', индекс {idx}")
        print(f"  payload: {json.dumps(S.build_spawn_payload(idx, style))}")
        print(f"  позиция: ({pos[0]:.1f}, {pos[1]:.1f}, {pos[2]:.1f})")
        print(f"\nЗафиксировать: BR_SPAWN_STYLE={style}")
    else:
        print("РАБОТАЮЩИЙ ВАРИАНТ НЕ НАЙДЕН — формат другой, нужен дамп трафика.")
    print("=" * 62)

    bot.stop()
    return 0 if winner else 1


if __name__ == "__main__":
    sys.exit(main())
