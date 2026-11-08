#!/usr/bin/env python3
"""Доказательство: бан ложится на ВЫХОДНОЙ адрес, а не на локальный порт.

Сценарий кика 999:
  1. узел подписки живёт за мостом, в аккаунте записан socks5://127.0.0.1:<порт>;
  2. приходит кик, bot.py зовёт pool.mark_banned(прокси);
  3. метка должна лечь на выходной адрес узла — или на ключ узла, если
     выход неизвестен. На локальный порт — никогда;
  4. если выходной адрес сменился, прокси снова пригоден. Порт, помеченный
     навсегда, пригодным стать не может.

Run: python3 prove_ban_scope.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from logic.proxy_pool import ProxyPool
from logic.vpn_probe import ProbeResult, STATUS_OK
from logic.vpn_sub import VpnNode


def log(line: str) -> None:
    print(line, flush=True)


def make_pool(tmp: str, *, egress: str) -> ProxyPool:
    pool = ProxyPool(
        os.path.join(tmp, "pool.json"),
        prober=lambda nodes, **kw: [],
        clock=lambda: 1000.0,
        log=lambda m: log("    " + m),
    )
    # Мост: порт -> узел подписки. Ровно то, что делает VpnBridge.
    node = VpnNode(name="site", protocol="vless", address="site.babay.su",
                   port=443, login="uuid-1", raw="vless://uuid-1@site.babay.su:443")
    pool._cached_nodes = [node]
    result = ProbeResult(node=node, alive=True, usable=True, status=STATUS_OK,
                         egress=egress, socks_url="socks5://127.0.0.1:20464")

    class FakeBridge:
        def key_of_port(self, port):
            return node.key if int(port) == 20464 else ""

    pool.bridge = FakeBridge()
    pool.results[node.key] = result
    return pool


def main() -> int:
    tmp = tempfile.mkdtemp()
    try:
        log("== 1. кик 999 на мост: socks5://127.0.0.1:20464 ==")
        pool = make_pool(tmp, egress="201.58.185.92")
        keys = pool.mark_banned("socks5://127.0.0.1:20464",
                                reason="Античит: IP заблокирован")
        log(f"  записано в бан-лист: {keys}")
        log(f"  локальный порт в бан-листе: "
            f"{'socks5://127.0.0.1:20464' in pool.banned_keys()}")
        log(f"  выходной адрес в бан-листе: {'ip:201.58.185.92' in pool.banned_keys()}")
        log(f"  is_banned(порт моста): {pool.is_banned('socks5://127.0.0.1:20464')}")
        log(f"  is_banned(адрес):      {pool.is_banned('socks5://201.58.185.92:1080')}")
        log(f"  usable_proxies():      {pool.usable_proxies()}")

        log("== 2. локальный HAPP: тот же кик, выход 152.233.43.93 ==")
        tmp2 = tempfile.mkdtemp()
        pool2 = ProxyPool(os.path.join(tmp2, "pool.json"),
                          prober=lambda nodes, **kw: [],
                          clock=lambda: 1000.0, log=lambda m: log("    " + m))
        pool2.manual = [{"proxy": "socks5://127.0.0.1:10808",
                         "name": "HAPP локальный", "added_at": 1.0}]
        node2 = VpnNode(name="HAPP локальный", protocol="socks5",
                        address="127.0.0.1", port=10808, source="manual")
        pool2.results[node2.key] = ProbeResult(
            node=node2, alive=True, usable=True, status=STATUS_OK,
            egress="152.233.43.93")
        pool2.egress[node2.key] = "152.233.43.93"
        keys2 = pool2.mark_banned("socks5://127.0.0.1:10808",
                                  reason="Античит: IP заблокирован")
        log(f"  записано в бан-лист: {keys2}")
        log(f"  локальный порт в бан-листе: "
            f"{'socks5://127.0.0.1:10808' in pool2.banned_keys()}")
        log(f"  usable_proxies(): {pool2.usable_proxies()}  (пусто — верно: адрес забанен)")

        log("== 3. выходной адрес сменился — прокси снова пригоден ==")
        pool2.egress[node2.key] = "5.6.7.8"
        log(f"  is_banned(порт): {pool2.is_banned('socks5://127.0.0.1:10808')}")
        log(f"  usable_proxies(): {pool2.usable_proxies()}")

        log("== 4. что было бы со старой логикой (бан по порту) ==")
        old = dict(pool2.banned)
        old["socks5://127.0.0.1:10808"] = {"reason": "old"}
        pool2.banned = old
        pool2.egress.pop(node2.key, None)
        log(f"  is_banned(порт) при новом выходе 5.6.7.8: "
            f"{pool2.is_banned('socks5://127.0.0.1:10808')}  "
            "— порт забанен навсегда, прокси не вернётся")
        shutil.rmtree(tmp2, ignore_errors=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
