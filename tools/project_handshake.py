#!/usr/bin/env python3
"""Что отвечает сервер проекта на первый пакет: BR или стандартный RakNet.

Зачем. У Black Russia свой первый пакет — два байта b"BR" — и свой
OPEN_CONNECTION_REPLY. SA-MP-серверы (и всё, что от них отпочковалось)
начинают стандартно: OFFLINE-сообщение 0x05, ответ 0x06, затем 0x07/0x08 и
запрос подключения 0x09. Пока неизвестно, к какому лагерю относится Arizona,
гадать бессмысленно: один замер отвечает на этот вопрос.

    python3 tools/project_handshake.py arz
    python3 tools/project_handshake.py arz --server 2
    python3 tools/project_handshake.py --host mobile3.arizona-rp.com --port 5125
    python3 tools/project_handshake.py arz --only raknet --out .probe/arz_handshake.json

Скрипт ничего не пишет в игру и не трогает аккаунты: только offline-обмен.
Запускать надо с той машины, где бот реально играет — UDP 5125 из мобильного
интернета часто не ходит, и тогда ответа не будет ни у одного варианта.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import br_servers
import projects as projects_mod
from samp import raknet_offline as offline

# Первый пакет Black Russia. Не два байта b"BR", как можно подумать по
# имени метода в коде: в сокет уходит id пакета 0x04 и следом маркер.
# Проверено дампом исходящих у живого бота — 044252, и сервер отвечает
# 0700. С голым b"BR" сервер молчит, и проба врёт.
BR_REQUEST = b"\x04BR"


def http_pre_ping(host: str, http_host: str, port: int = 80,
                  timeout: float = 5.0, log=print) -> dict:
    """То же, что бот делает перед первым UDP-пакетом.

    Без этого проба врёт. Проверено живьём: голое b"BR" на сервер Black
    Russia молчит, а тот же сервер после HTTP-запроса отвечает, и бот через
    него входит. Сервер (nginx на игровом порту 80) запоминает адрес и
    только после этого пускает UDP.
    """
    request = (
        "GET / HTTP/1.1\r\n"
        "User-Agent: Dalvik/2.1.0 (Linux; U; Android 13; en-US; Pixel 8 Build/TQ3A.230817.082)\r\n"
        f"Host: {http_host}\r\n"
        "Connection: Keep-Alive\r\n"
        "Accept-Encoding: gzip\r\n"
        "\r\n"
    ).encode("ascii", errors="ignore")
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        log(f"  pre-ping: соединение не поднялось ({exc!r})")
        return {"ok": False, "reason": repr(exc)}
    try:
        sock.sendall(request)
        sock.settimeout(timeout)
        data = sock.recv(512)
        first = data.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        log(f"  pre-ping: {first or 'пусто'} ({len(data)}б)")
        return {"ok": bool(data), "status": first, "bytes": len(data)}
    except OSError as exc:
        log(f"  pre-ping: ошибка {exc!r}")
        return {"ok": False, "reason": repr(exc)}
    finally:
        sock.close()


def probe_br(host: str, port: int, timeout: float = 3.0, log=print) -> dict:
    """BR-хендшейк: id 0x04 + маркер и ждём, что вообще прилетит."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    started = time.time()
    try:
        sock.sendto(BR_REQUEST, (host, port))
        try:
            data, _ = sock.recvfrom(4096)
        except socket.timeout:
            log("  BR: молчит")
            return {"ok": False, "reason": "timeout", "sent": BR_REQUEST.hex()}
        except OSError as exc:
            log(f"  BR: сеть {exc!r}")
            return {"ok": False, "reason": repr(exc)}
        ms = (time.time() - started) * 1000
        log(f"  BR: ответ {len(data)}б за {ms:.0f}мс: {data[:32].hex()}")
        return {"ok": True, "bytes": len(data), "ms": round(ms),
                "sent": BR_REQUEST.hex(), "hex": data[:64].hex()}
    finally:
        sock.close()


def resolve_target(args) -> tuple[str, int, str, str]:
    """Цель пробы: адрес, порт, подпись и Host для pre-ping.

    Host для pre-ping — не то же самое, что адрес: у Black Russia это
    <сервер>.blackrussia.online, а у Arizona в списке уже готовое имя, и
    лишний суффикс дал бы несуществующий домен.
    """
    if args.host:
        return args.host, int(args.port or 5125), args.host, args.host
    project = (projects_mod.get_project(args.project) if args.project
               else projects_mod.active_project())
    servers = br_servers.get_servers(project)
    server = (br_servers.find_server(args.server, project) if args.server
              else (servers[0] if servers else None))
    if not server:
        raise SystemExit(f"[!] сервер '{args.server}' не найден в списке "
                         f"{project.title}")
    host = str(server.get("ip") or "")
    port = int(server.get("port") or 5125)
    name = project.clean_server_name(
        server.get("firstname") or server.get("name") or "")
    host_name = str(server.get("firstname") or "").strip()
    if project.http_host_suffix and host_name:
        http_host = f"{host_name.lower()}.{project.http_host_suffix}"
    elif host and not host.replace(".", "").isdigit():
        # В списке уже домен — он и есть Host (так у Arizona).
        http_host = host
    else:
        http_host = str(server.get("name") or host).strip()
    return host, port, f"{name} [{project.title}]", http_host


def verdict_for(br_ok: bool, rk_ok: bool, only: str | None = None) -> str:
    """Что показала проверка — одной строкой.

    Отдельная функция, потому что это единственный вывод, по которому
    принимается решение, и врать он не должен: при --only считаем только то,
    что реально спросили у сервера.
    """
    if only == "br":
        return ("BR-хендшейк: принят" if br_ok else
                "BR-хендшейк: молчит (проверялся только он, RakNet не трогали)")
    if only == "raknet":
        return ("стандартный RakNet: offline-обмен прошёл до "
                "CONNECTION_REQUEST_ACCEPTED" if rk_ok else
                "стандартный RakNet: молчит (проверялся только он)")
    if rk_ok and not br_ok:
        return ("стандартный RakNet: offline-обмен прошёл до "
                "CONNECTION_REQUEST_ACCEPTED")
    if rk_ok and br_ok:
        return "отвечает и на BR, и на стандартный RakNet — идти стандартным путём"
    if br_ok and not rk_ok:
        return "только BR-хендшейк (свой протокол, как у Black Russia)"
    return ("нет ответа ни на один вариант: либо UDP не ходит с этой машины, "
            "либо сервер ждёт пре-пинг по HTTP. C той же машины проверь "
            "BR-сервер — если и он молчит, дело в сети")


def main() -> int:
    parser = argparse.ArgumentParser(description="первый пакет сервера проекта")
    parser.add_argument("project", nargs="?", default=None, help="ключ проекта")
    parser.add_argument("--server", default=None, help="номер или имя сервера")
    parser.add_argument("--host", default=None, help="адрес вручную (минуя список)")
    parser.add_argument("--port", type=int, default=None, help="порт вручную")
    parser.add_argument("--timeout", type=float, default=2.5, help="таймаут на пакет")
    parser.add_argument("--only", choices=("br", "raknet"), default=None,
                        help="проверить только один вариант")
    parser.add_argument("--no-pre-ping", action="store_true",
                        help="не делать HTTP pre-ping (для сравнения)")
    parser.add_argument("--out", default=None, help="куда записать JSON-отчёт")
    args = parser.parse_args()

    host, port, title, http_host = resolve_target(args)
    print(f"[*] {title} — {host}:{port}", flush=True)

    # Чем этот проект входит сейчас — чтобы отчёт читался вместе с настройкой,
    # а не отдельно от неё. Профиль применяем и здесь: из него берётся версия
    # протокола, которую потом вписывать не придётся.
    project = (projects_mod.get_project(args.project) if args.project
               else projects_mod.active_project())
    projects_mod.apply_rpc_profile(project, log=lambda m: print(m, flush=True))
    report = {"host": host, "port": port, "title": title, "project": project.key,
              "project_handshake": project.handshake,
              "ts": time.time(), "timeout": args.timeout}
    if project.handshake == "raknet":
        options = projects_mod.raknet_options()
        print(f"[*] Проект {project.title}: вход raknet "
              f"{options or {'version': 5, 'magic': False}}", flush=True)
    else:
        print(f"[*] Проект {project.title}: вход {project.handshake}", flush=True)

    if args.no_pre_ping:
        print("[*] HTTP pre-ping выключен — проба пойдёт голым пакетом", flush=True)
    else:
        print(f"[*] HTTP pre-ping (Host: {http_host}):", flush=True)
        report["pre_ping"] = http_pre_ping(host, http_host)

    print(f"[*] BR-хендшейк ({BR_REQUEST.hex()}):", flush=True)

    if args.only != "raknet":
        report["br"] = probe_br(host, port, timeout=args.timeout)

    if args.only != "br":
        # Перед сменой протокола pre-ping повторяем: сервер открывает UDP
        # своему адресу на время, и к моменту второй пробы окно могло закрыться.
        if not args.no_pre_ping:
            print("[*] HTTP pre-ping перед RakNet:", flush=True)
            http_pre_ping(host, http_host)
        print("[*] Стандартный RakNet (перебор REQUEST_1):", flush=True)
        handshake = offline.OfflineHandshake(host, port,
                                             timeout=args.timeout)
        report["raknet"] = handshake.run()

    # Вердикт. Нужен именно он: по нему решается, каким путём идти дальше.
    br_ok = bool((report.get("br") or {}).get("ok"))
    rk_ok = bool((report.get("raknet") or {}).get("ok"))
    verdict = verdict_for(br_ok, rk_ok, args.only)
    report["verdict"] = verdict
    print(f"\n[*] Вердикт: {verdict}", flush=True)

    out = args.out or os.path.join(
        projects_mod.BASE_DIR, ".probe",
        f"handshake_{(args.project or 'active')}.json")
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        print(f"[*] Отчёт: {out}", flush=True)
    except OSError as exc:
        print(f"[!] Отчёт не записался: {exc!r}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
