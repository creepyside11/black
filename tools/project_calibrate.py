#!/usr/bin/env python3
"""Калибровка проекта: какие id RPC и интерфейсов шлёт конкретный сервер.

Зачем. Black Russia — не SA-MP из коробки: у неё своя нумерация (ShowDialog
это интерфейс 347, ответ на диалог — RPC 406). Arizona RP Mobile — свой
SA-MP-клиент на RakNet, серверные скрипты у него свои, и часть id почти
наверняка другая. Гадать по числам нельзя: неверный id — это либо тишина,
либо кик античитом. Поэтому id не зашиты в код, а берутся из профиля
проекта, и профиль наполняется замером.

Что делает этот скрипт:

    python3 tools/project_calibrate.py arz
        Подключиться к серверу проекта и молча слушать. Ничего не
        отправляется: видно пре-логиновый обмен — хендшейк, интерфейс
        авторизации, меню вокзалов. Это уже даёт половину карты.

    python3 tools/project_calibrate.py arz --login
        Взять из реестра проекта первый аккаунт и войти им (ник и пароль с
        диска). Тогда видно и то, что приходит после входа: диалоги, меню,
        RPC чата и статистики.

    python3 tools/project_calibrate.py arz --server 2 --seconds 120 --out .probe/arz.json

Отчёт: таблица «id -> сколько раз -> пример тела» и JSON рядом с проектом.
Сравни с Black Russia (тот же скрипт без аргумента проекта) и впиши в
projects/<ключ>.profile.json только отличия.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import br_servers
import projects as projects_mod
from core import security
from core.network import Network
from core.protocol import RakNetProtocol
from samp.session import SampSession
from samp.sync import PlayerSyncScheduler
from logic.json_ui import JsonUIConfig
from logic.registration import RegistrationConfig

MAX_BODY = 96          # сколько байт тела хранить в примере
MAX_EXAMPLES = 3       # примеров на один id


class Tap:
    """Перехватчик входящих пакетов.

    Ставится на session.handler.process_packet: сюда приходит уже
    разобранный пакет, поэтому видно и тип, и rpc_id, и interface_id, и
    исходные байты. Правки в сам обработчик не нужны.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.interfaces: dict[int, dict] = {}
        self.rpcs: dict[int, dict] = {}
        self.types: dict[str, int] = {}
        self.sent: dict[str, int] = {}
        # Первые пакеты, ушедшие в сокет: из них видно, чем начинается вход.
        self.sent_bytes: list[bytes] = []
        self.total = 0

    def note_incoming(self, result, body: bytes) -> None:
        if not isinstance(result, dict):
            return
        with self.lock:
            self.total += 1
            kind = str(result.get("type") or "?")
            self.types[kind] = self.types.get(kind, 0) + 1
            target = None
            key = None
            if result.get("interface_id") is not None:
                target, key = self.interfaces, int(result["interface_id"])
            elif result.get("rpc_id") is not None:
                target, key = self.rpcs, int(result["rpc_id"])
            if target is None:
                return
            entry = target.setdefault(key, {"count": 0, "examples": [],
                                            "type": kind, "names": {}})
            entry["count"] += 1
            name = result.get("packet_name") or result.get("name")
            if name:
                entry["names"][str(name)] = entry["names"].get(str(name), 0) + 1
            if len(entry["examples"]) < MAX_EXAMPLES:
                sample = {
                    "hex": bytes(body or b"")[:MAX_BODY].hex(),
                    "len": len(body or b""),
                }
                payload = result.get("json")
                if payload is not None:
                    sample["json"] = payload
                target[key]["examples"].append(sample)

    def note_sent_bytes(self, data: bytes) -> None:
        """Первые исходящие пакеты — их и надо повторить при разборе."""
        if len(self.sent_bytes) >= 12:
            return
        self.sent_bytes.append(data)

    def note_sent(self, name: str) -> None:
        with self.lock:
            self.sent[name] = self.sent.get(name, 0) + 1

    def report(self) -> dict:
        with self.lock:
            return {
                "total_incoming": self.total,
                "types": dict(sorted(self.types.items())),
                "interfaces": {str(k): v for k, v in sorted(self.interfaces.items())},
                "rpcs": {str(k): v for k, v in sorted(self.rpcs.items())},
                "sent": dict(sorted(self.sent.items())),
            }


def _install_spy(session, tap: Tap) -> None:
    original = session.handler.process_packet

    def spy(raknet_packet):
        result = original(raknet_packet)
        try:
            tap.note_incoming(result, raknet_packet.get("data") or b"")
        except Exception:
            pass
        return result

    session.handler.process_packet = spy

    # Что бот отправляет сам: без этого непонятно, на какой наш id сервер
    # отвечает диалогом.
    #
    # PacketSender объявлен со __slots__, поэтому подменить метод у
    # экземпляра нельзя — только у класса. Для одиночного прогона это
    # нормально: патч живёт до конца процесса, а процесс здесь один.
    # Что уходит в сокет побайтово. Без этого не видно, чем на самом деле
    # начинается вход: у Black Russia это два байта b"BR", и по одному
    # названию метода этого не угадать.
    from core.network import Network

    if not getattr(Network.send, "_calib_dump", False):
        original_network_send = Network.send

        def network_send_spy(self, data):
            tap.note_sent_bytes(bytes(data))
            return original_network_send(self, data)

        network_send_spy._calib_dump = True
        Network.send = network_send_spy

    from samp.sender import PacketSender

    if not getattr(PacketSender.send, "_calib_spy", False):
        original_send = PacketSender.send

        def send_spy(self, payload, *, packet_name=None, rpc_name=None,
                     event_name=None):
            name = rpc_name or packet_name or ""
            if name:
                tap.note_sent(str(name))
            return original_send(
                self, payload, packet_name=packet_name, rpc_name=rpc_name,
                event_name=event_name)

        send_spy._calib_spy = True
        PacketSender.send = send_spy


def _load_login(project, nickname: str | None):
    """Взять ник и пароль из реестра проекта. Пароль в лог не печатается."""
    path = project.accounts_path
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None, None
    if not isinstance(data, list):
        return None, None
    for item in data:
        if not isinstance(item, dict):
            continue
        nick = str(item.get("nickname") or "").strip()
        pwd = str(item.get("password") or "").strip()
        if not nick or not pwd:
            continue
        if nickname and nick.lower() != nickname.lower():
            continue
        return nick, pwd
    return None, None


def _print_table(title: str, data: dict) -> None:
    print(f"\n=== {title} ===", flush=True)
    if not data:
        print("  (ничего не пришло)", flush=True)
        return
    for key, item in sorted(data.items(), key=lambda kv: -kv[1]["count"]):
        names = ",".join(sorted(item.get("names") or {})) or "-"
        print(f"  id={key:<5} раз={item['count']:<5} тип={item.get('type','?'):<18} имя={names}",
              flush=True)
        for sample in item["examples"][:2]:
            payload = sample.get("json")
            tail = f" json={json.dumps(payload, ensure_ascii=False)[:120]}" if payload is not None else ""
            print(f"      len={sample['len']} {sample['hex'][:64]}{tail}", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="калибровка id проекта")
    parser.add_argument("project", nargs="?", default=None,
                        help="ключ проекта: br, arz (по умолчанию активный)")
    parser.add_argument("--server", default=None,
                        help="номер или имя сервера (по умолчанию первый в списке)")
    parser.add_argument("--nick", default=None,
                        help="ник из реестра проекта для входа")
    parser.add_argument("--login", action="store_true",
                        help="войти аккаунтом из реестра (по умолчанию — молча слушать)")
    parser.add_argument("--seconds", type=float, default=60.0,
                        help="сколько секунд слушать после подключения")
    parser.add_argument("--out", default=None, help="куда записать JSON-отчёт")
    args = parser.parse_args()

    project = projects_mod.get_project(args.project) if args.project else projects_mod.active_project()
    print(f"[*] Проект: {project.title} ({project.key})", flush=True)
    # Профиль проекта: без него калибровка показала бы BR-шные id вместо
    # настоящих — и «отличий нет» оказалось бы ложью.
    projects_mod.apply_rpc_profile(project, log=lambda m: print(m, flush=True))

    if not br_servers.is_configured(project):
        print(f"[!] У {project.title} не задан список серверов: "
              f"projects.json -> projects.{project.key}.servers_api", flush=True)
        return 2

    try:
        servers = br_servers.get_servers(project)
    except Exception as exc:
        print(f"[!] Список серверов не получен: {exc}", flush=True)
        return 2

    if args.server:
        server = br_servers.find_server(args.server, project)
    else:
        server = servers[0] if servers else None
    if not server:
        print(f"[!] Сервер '{args.server}' не найден в списке проекта", flush=True)
        return 2

    host = str(server.get("ip") or "")
    port = int(server.get("port") or 0)
    name = project.clean_server_name(server.get("name") or server.get("firstname") or "")
    print(f"[*] Сервер: {name} — {host}:{port}", flush=True)

    nickname, password = (None, None)
    if args.login or args.nick:
        nickname, password = _load_login(project, args.nick)
        if not nickname:
            print("[!] В реестре проекта нет подходящего аккаунта — слушаю молча", flush=True)
    if args.login and not password:
        print("[!] Пароль не найден: вход отменён, слушаю молча", flush=True)

    if not nickname:
        # Безымянный слушатель: сервер увидит подключение без ника. Для
        # пре-логинового обмена этого достаточно, аккаунт не тратится.
        nickname = f"Calib{int(time.time()) % 100000}"

    registration = None
    if password:
        registration = RegistrationConfig(
            enabled=True,
            auto_login=True,
            password=password,
            step_delay=float(os.environ.get("BR_AUTH_STEP_DELAY", "0.6")),
            ack_timeout=float(os.environ.get("BR_AUTH_TIMEOUT", "15.0")),
        )
        print(f"[*] Вход аккаунтом {nickname} (пароль не печатаю)", flush=True)
    else:
        print(f"[*] Молчаливое подключение ником {nickname}, пакеты не шлём", flush=True)

    network = Network(host, port)
    protocol = RakNetProtocol(network, security)
    sync = PlayerSyncScheduler(rate_player=0.5, rate_player_min=0.5,
                               rate_player_max=0.75)
    server_names = {}
    try:
        server_names = {
            str(item.get("ip")): project.clean_server_name(item.get("firstname") or "")
            for item in servers if isinstance(item, dict) and item.get("ip")
        }
    except Exception:
        server_names = {}

    session = SampSession(
        protocol,
        host,
        port,
        nickname,
        sync_scheduler=sync,
        fps=30.0,
        pings_ips_continuous=False,
        http_pre_ping=True,
        http_pre_ping_port=80,
        http_pre_ping_timeout=5.0,
        http_pre_ping_host_suffix=project.http_host_suffix,
        http_server_names=server_names,
        registration=registration,
        json_ui=JsonUIConfig(console_output=False, auto_close_cinematic=True),
        # Вход такой же, как у бота на этом проекте: у Black Russia это b"BR",
        # у Arizona — стандартный offline-обмен RakNet. С BR-входом на
        # Arizona не будет ни одного входящего пакета, и «ничего не пришло»
        # выглядело бы как пустой сервер, а не как неверный хендшейк.
        handshake=project.handshake,
        handshake_options=projects_mod.raknet_options(),
    )

    print(f"[*] Вход: {project.handshake}"
          + (f" {projects_mod.raknet_options()}"
             if project.handshake == "raknet" else ""), flush=True)

    tap = Tap()
    _install_spy(session, tap)

    print("[*] Подключаюсь...", flush=True)
    connected = False
    try:
        connected = bool(session.connect())
    except Exception as exc:
        print(f"[!] Подключение упало: {exc!r}", flush=True)
    print(f"[*] Подключение: {'есть init_game' if connected else 'нет ответа на хендшейк'}",
          flush=True)

    deadline = time.time() + max(1.0, float(args.seconds))
    try:
        while time.time() < deadline:
            time.sleep(0.5)
            if not session.connected:
                print("[*] Сессия закрыта сервером", flush=True)
                break
    except KeyboardInterrupt:
        print("[*] Прервано", flush=True)

    report = tap.report()
    _print_table("Интерфейсы (iid)", report["interfaces"])
    _print_table("RPC", report["rpcs"])
    print("\n=== Типы пакетов ===", flush=True)
    for kind, count in sorted(report["types"].items(), key=lambda kv: -kv[1]):
        print(f"  {kind:<24} {count}", flush=True)
    if report["sent"]:
        print("\n=== Отправлено нами ===", flush=True)
        for kind, count in sorted(report["sent"].items(), key=lambda kv: -kv[1]):
            print(f"  {kind:<24} {count}", flush=True)
    if tap.sent_bytes:
        print("\n=== Первые байты в сокет ===", flush=True)
        for i, data in enumerate(tap.sent_bytes, 1):
            head = data[:40].hex()
            tail = "…" if len(data) > 40 else ""
            print(f"  {i:>2}. {len(data):>4}б  {head}{tail}", flush=True)
        report["sent_first"] = [d.hex() for d in tap.sent_bytes]

    out = args.out or os.path.join(
        projects_mod.BASE_DIR, ".probe", f"calib_{project.key}.json")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        report["project"] = project.as_dict()
        report["server"] = {"name": name, "host": host, "port": port}
        report["logged_in"] = bool(password)
        report["ts"] = time.time()
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"\n[*] Отчёт: {out}", flush=True)
    except Exception as exc:
        print(f"[!] Отчёт не записался: {exc!r}", flush=True)

    profile = project.rpc_profile_path
    if profile:
        print(f"[*] Отличия вписать в {profile}", flush=True)

    try:
        session.disconnect()
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
