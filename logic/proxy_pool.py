"""Пул прокси: подписки, ручные прокси, проверка и выдача боту.

Задача одна: к моменту запуска бота знать, какой прокси живой. Поэтому
пул не просто хранит строки — он держит статус каждого узла и обновляет его
по таймеру (по умолчанию раз в 5 минут).

Правила:

    мёртвый узел        alive=False            красный
    живой, но не наш    alive=True, usable=False жёлтый (нужен клиент)
    рабочий             alive=True, usable=True  зелёный

Бот берёт только `usable`. Перед подключением пул ещё раз проверяет
выбранный узел сквозным запросом — «сначала запрос, потом заход», как и
должно быть: иначе бот уйдёт в коннект с заведомо мёртвым прокси и будет
висеть в зоне авторизации.

Хранится в `proxies_pool.json` рядом с `accounts.json`.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import threading
import time

from dataclasses import replace

from .vpn_probe import (
    GAME_TARGET_HOST,
    GAME_TARGET_PORT,
    ProbeResult,
    STATUS_UNKNOWN,
    STATUS_BAD_UDP,
    STATUS_BANNED,
    STATUS_DEAD,
    STATUS_NEEDS_CLIENT,
    STATUS_NO_GAME,
    STATUS_NO_TRAFFIC,
    STATUS_OK,
    probe_many,
    probe_node,
    tcp_egress,
)
from .vpn_sub import (
    SubscriptionError,
    VpnNode,
    dedupe,
    load_subscription,
    parse_link,
)
from .vpn_bridge import VpnBridge, bridge_available, port_for

DEFAULT_INTERVAL = 300.0  # 5 минут
DEFAULT_TIMEOUT = 6.0

# Ключи для ручных прокси, чтобы они не конфликтовали с узлами подписок.
MANUAL_PREFIX = "manual"

# Бан по адресу хранится под этим префиксом.
#
# Античит банит ВЫХОДНОЙ адрес — тот, с которого бот приходит на игровой
# сервер. Локальный порт моста (127.0.0.1:<порт>) — это наш собственный
# клиентский сокет, игровой сервер его не видит вообще. Пометить его
# забаненным значит навсегда выкинуть живой прокси из раздачи: даже когда
# HAPP переподключится и выйдет с другого адреса, порт останется
# «забаненным». Поэтому выходной адрес и локальный эндпоинт — разные вещи,
# и в списке банов хранится первое.
BAN_IP_PREFIX = "ip:"
LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1", "0.0.0.0")


def is_local_address(address: str) -> bool:
    """Локальный эндпоинт — наш сокет, а не адрес, который видит игра."""
    return (address or "").strip().lower() in LOCAL_HOSTS


def _port_of(proxy: str) -> int | None:
    """Порт локального прокси-строки. Локальный — только 127.0.0.1."""
    parsed = parse_link(proxy) if "://" in str(proxy) else None
    if parsed is None or not is_local_address(parsed.address):
        return None
    try:
        return int(parsed.port)
    except (TypeError, ValueError):
        return None


class ProxyPoolError(Exception):
    """Пул не смог выполнить операцию."""


# Схемы, которые означают «это отдельный ключ узла», а не URL подписки.
NODE_KEY_SCHEMES = ("vless://", "vmess://", "trojan://", "ss://", "ssr://",
                    "hysteria://", "hysteria2://", "tuic://", "socks5://",
                    "socks5h://", "socks4://", "http://", "https://")


def _looks_like_node_key(line: str) -> bool:
    """Отдельный ключ узла, а не URL подписки.

    http/https сюда попадают только с явным путём на .txt/.json — иначе
    это подписка. Проще: подписку почти всегда добавляют как https://...,
    а ключ — как vless://...
    """
    low = (line or "").strip().lower()
    if not low:
        return False
    if low.startswith(("vless://", "vmess://", "trojan://", "ss://", "ssr://",
                       "hysteria://", "hysteria2://", "tuic://",
                       "socks5://", "socks5h://", "socks4://")):
        return True
    return False


def _now() -> float:
    return time.time()


def _new_hwid() -> str:
    """Постоянный идентификатор устройства для панелей подписок."""
    import uuid

    return uuid.uuid4().hex


class ProxyPool:
    """Подписки + ручные прокси + их статус."""

    def __init__(
        self,
        path: str | None = None,
        *,
        log=None,
        interval: float = DEFAULT_INTERVAL,
        timeout: float = DEFAULT_TIMEOUT,
        target_host: str = "",
        # Порт для TCP-проверки. 5125 сюда ставить НЕЛЬЗЯ: игровой сервер
        # слушает там только UDP, TCP-коннект туда не проходит ни у одного
        # прокси — получался ложный «мёртвый» на живом прокси. Проверяем
        # TCP на обычном порту, UDP — отдельно, через STUN.
        target_port: int = 443,
        game_host: str = GAME_TARGET_HOST,
        game_port: int = GAME_TARGET_PORT,
        prober=None,
        loader=None,
        clock=_now,
        bridge=None,
    ):
        self.path = path or os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "proxies_pool.json",
        )
        # Лог может прийти во время выключения интерпретатора — тогда
        # print падает на заблокированном stdout. Пул из-за этого умирать
        # не должен.
        self.log = log or (lambda _msg: None)
        self.interval = float(interval)
        self.timeout = float(timeout)
        self.target_host = target_host
        self.target_port = int(target_port)
        # Игровой сервер, до которого обязан доставать прокси. Все игровые
        # адреса в 80.66.82.0/24, поэтому одного достаточно как признака.
        self.game_host = game_host
        self.game_port = int(game_port)
        self._prober = prober or probe_many
        self._loader = loader or load_subscription
        self._clock = clock

        self._lock = threading.RLock()
        self.subscriptions: list[dict] = []
        self.manual: list[dict] = []
        self.results: dict[str, ProbeResult] = {}
        # Забаненные адреса: key -> {"name", "address", "reason", "ts"}.
        # Это отдельный от «мёртвых» список: мёртвый прокси может ожить,
        # а забаненный сервером не оживёт никогда. Храним на диске, иначе
        # после перезапуска бот снова возьмёт тот же адрес и словит бан.
        self.banned: dict[str, dict] = {}
        # Выходной адрес по ключу прокси: key -> ip. Это факт, а не бан.
        # Нужен, чтобы «забанен адрес 1.2.3.4» находился и у моста, у
        # которого в аккаунте записан локальный порт, и чтобы один и тот же
        # выход не выдавался под другим узлом.
        self.egress: dict[str, str] = {}
        self._cached_nodes: list[VpnNode] = []
        self.last_refresh: float = 0.0
        self.last_error: str = ""
        # Панели Remnawave не отдают узлы без x-hwid. Идентификатор должен
        # быть постоянным, иначе каждая проверка выглядит как новое
        # устройство и панель начинает упираться в лимит.
        self.hwid: str = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.running = False
        # Мост VLESS/VMess/Trojan/SS -> SOCKS5. Без него такие узлы из
        # подписки остаются жёлтыми: живой, но применить некуда.
        if bridge is None and bridge_available():
            bridge = VpnBridge(log=self.log)
        self.bridge = bridge

        self.load()
        if not self.hwid:
            self.hwid = _new_hwid()

    # ---------- хранение ----------

    def load(self) -> None:
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return
        with self._lock:
            self.subscriptions = [
                item for item in (data.get("subscriptions") or [])
                if isinstance(item, dict) and item.get("url")
            ]
            self.manual = [
                item for item in (data.get("manual") or [])
                if isinstance(item, dict) and item.get("proxy")
            ]
            self.hwid = str(data.get("hwid") or "")
            banned = data.get("banned") or {}
            if isinstance(banned, dict):
                self.banned = {
                    str(k): dict(v) for k, v in banned.items()
                    if isinstance(v, dict)
                }
            egress = data.get("egress") or {}
            if isinstance(egress, dict):
                self.egress = {
                    str(k): str(v) for k, v in egress.items() if v
                }
        migrated = self._migrate_local_bans()
        if not self.hwid:
            self.hwid = _new_hwid()
            self.save()
        elif migrated:
            self.save()

    def _migrate_local_bans(self) -> bool:
        """Перевести старые баны с локальных портов на выходные адреса.

        Раньше обработчик кика метил `socks5://127.0.0.1:10808` — то есть
        локальный порт HAPP. Игровой сервер этого порта не видит: он видит
        выходной адрес HAPP. Порт, помеченный навсегда, означает, что даже
        после смены выхода прокси больше никогда не выдадут.

        Что делаем: знаем выходной адрес — переносим метку на него;
        не знаем — спрашиваем его у самого прокси (один короткий запрос на
        старую запись) и переносим. Только если прокси не отвечает,
        снимаем метку с порта и говорим об этом в лог: ложная метка на
        локальном порту хуже её отсутствия, потому что навсегда выкидывает
        живой прокси из раздачи.
        """
        moved = False
        for key in list(self.banned):
            parsed = parse_link(key) if "://" in key else None
            if parsed is None or not is_local_address(parsed.address):
                continue
            entry = self.banned.pop(key, {})
            egress = self.egress.get(key, "")
            if not egress:
                try:
                    egress = tcp_egress(
                        parsed.address, int(parsed.port),
                        login=parsed.login, password=parsed.password,
                        timeout=min(4.0, float(self.timeout)),
                    )
                except Exception:
                    egress = ""
            if egress:
                self.egress[key] = egress
                self.banned[f"{BAN_IP_PREFIX}{egress}"] = dict(
                    entry, address=egress,
                    reason=(entry.get("reason") or "") +
                    " [перенесено с локального порта]",
                )
                self.log(f"[ПРОКСИ] бан {key} перенесён на выходной адрес {egress}")
            else:
                self.log(
                    f"[ПРОКСИ] снят бессмысленный бан с локального порта {key}: "
                    "игровой сервер видит выходной адрес, а не наш порт. "
                    "Адрес будет помечен заново, если сервер его отвергнет."
                )
            moved = True
        return moved

    def save(self) -> None:
        with self._lock:
            data = {
                "subscriptions": list(self.subscriptions),
                "manual": list(self.manual),
                "hwid": self.hwid,
                "banned": dict(self.banned),
                "egress": dict(self.egress),
            }
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = f"{self.path}.tmp"
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError as exc:
            self.log(f"[ПРОКСИ] не сохранил {self.path}: {exc}")

    # ---------- подписки ----------

    def add_any(self, value: str, name: str = "", *, fetch: bool = True) -> list[VpnNode]:
        """Принять что угодно: ссылку подписки, отдельный ключ узла или пачку ключей.

        Раньше в «Добавить VPN-подписку» можно было вставить только URL.
        Одиночный ключ вида vless://... уходил в загрузчик подписок и падал
        с «не скачал подписку» — то есть новый ключ было не добавить вовсе.
        Теперь одиночный ключ (или несколько, по строке на ключ) кладётся
        как ручной узел.
        """
        value = (value or "").strip()
        if not value:
            raise ProxyPoolError("Пустое значение")

        lines = [line.strip() for line in value.splitlines() if line.strip()]
        keys = [line for line in lines if _looks_like_node_key(line)]
        if keys:
            nodes: list[VpnNode] = []
            for index, key in enumerate(keys):
                label = name if len(keys) == 1 else (name or f"ключ {index + 1}")
                nodes.append(self.add_manual(key, name=label))
            return nodes

        if not value.lower().startswith(("http://", "https://")):
            raise ProxyPoolError(
                "не понял формат: нужен URL подписки или ключ "
                "vless:// vmess:// trojan:// ss://"
            )
        return self.add_subscription(value, name=name, fetch=fetch)

    def add_subscription(self, url: str, name: str = "", *, fetch: bool = True) -> list[VpnNode]:
        """Добавить подписку. Сразу тянем узлы, чтобы отсеять битую ссылку."""
        url = (url or "").strip()
        if not url:
            raise ProxyPoolError("Пустая ссылка подписки")
        nodes: list[VpnNode] = []
        if fetch:
            try:
                nodes = self._load(url)
            except SubscriptionError as exc:
                raise ProxyPoolError(str(exc)) from exc
            except Exception as exc:
                raise ProxyPoolError(f"не скачал подписку: {exc}") from exc
            if not nodes:
                raise ProxyPoolError("подписка не содержит рабочих узлов")

        with self._lock:
            existing = next((s for s in self.subscriptions if s.get("url") == url), None)
            if existing is None:
                self.subscriptions.append(
                    {
                        "url": url,
                        "name": (name or "").strip() or url,
                        "added_at": self._clock(),
                        "nodes": len(nodes),
                    }
                )
            else:
                existing["nodes"] = len(nodes)
        self.save()
        self.log(f"[ПРОКСИ] подписка добавлена: {name or url} | узлов {len(nodes)}")
        return nodes

    def remove_subscription(self, needle: str) -> bool:
        needle = (needle or "").strip()
        removed = False
        with self._lock:
            keep = []
            for item in self.subscriptions:
                if needle and (needle == item.get("url") or needle == item.get("name")):
                    removed = True
                    continue
                keep.append(item)
            self.subscriptions = keep
        if removed:
            self.save()
        return removed

    def add_manual(self, proxy: str, name: str = "") -> VpnNode:
        """Ручной узел: socks5://user:pass@host:port или ключ vless://...

        Ключи vless/vmess/trojan/ss тоже принимаются: их поднимает мост
        sing-box, и бот получает локальный SOCKS5. Раньше такие ключи
        отклонялись с «бот умеет только socks5», то есть добавить узел из
        подписки вручную было нельзя.
        """
        node = parse_link(proxy)
        if node is None:
            raise ProxyPoolError(f"не понял прокси: {proxy!r}")
        if not node.is_direct and not node.needs_client:
            raise ProxyPoolError(f"{node.protocol} не поддерживается")
        # Для direct-узла храним готовую socks5-строку, для остальных —
        # исходный ключ: из него мост собирает конфиг sing-box.
        stored = node.proxy_url() or (proxy or "").strip()
        label = (name or "").strip() or node.name
        with self._lock:
            if not any(m.get("proxy") == stored for m in self.manual):
                self.manual.append(
                    {
                        "proxy": stored,
                        "name": label,
                        "added_at": self._clock(),
                    }
                )
        self.save()
        # Имя из аргумента важнее имени в ссылке: человек назвал узел сам.
        node.name = label
        self.log(f"[ПРОКСИ] добавлен вручную: {label} -> {stored}")
        return node

    def remove_manual(self, needle: str) -> bool:
        needle = (needle or "").strip()
        removed = False
        with self._lock:
            keep = []
            for item in self.manual:
                if needle and (needle == item.get("proxy") or needle == item.get("name")):
                    removed = True
                    continue
                keep.append(item)
            self.manual = keep
        if removed:
            self.save()
        return removed

    # ---------- узлы ----------

    def _load(self, url: str) -> list[VpnNode]:
        """Скачать подписку нашим hwid: без него панель отдаёт заглушки."""
        return self._loader(url, timeout=max(8.0, self.timeout * 2), hwid=self.hwid)

    def _manual_node(self, item: dict) -> VpnNode | None:
        node = parse_link(item.get("proxy", ""))
        if node is None:
            return None
        node.name = item.get("name") or node.name
        node.source = MANUAL_PREFIX
        return node

    def nodes(self) -> list[VpnNode]:
        """Все узлы: ручные первыми, потом из подписок (без скачивания)."""
        result: list[VpnNode] = []
        with self._lock:
            for item in self.manual:
                node = self._manual_node(item)
                if node is not None:
                    result.append(node)
            cached = list(self._cached_nodes)
        result.extend(cached)
        return dedupe(result)

    def fetch_all(self) -> list[VpnNode]:
        """Скачать все подписки. Ошибки не роняют остальные."""
        collected: list[VpnNode] = []
        with self._lock:
            subs = list(self.subscriptions)
        for sub in subs:
            url = sub.get("url", "")
            try:
                nodes = self._load(url)
            except Exception as exc:
                self.log(f"[ПРОКСИ] подписка не отвечает ({sub.get('name') or url}): {exc}")
                continue
            collected.extend(nodes)
            sub["nodes"] = len(nodes)
        with self._lock:
            self._cached_nodes = collected
        return collected

    # ---------- проверка ----------

    def refresh(self, *, fetch: bool = True) -> list[ProbeResult]:
        """Скачать подписки и проверить все узлы."""
        if fetch:
            self.fetch_all()
        nodes = self.nodes()
        if not nodes:
            # Подписка не отдала узлы (сеть моргнула, панель ответила пустым).
            # Стирать прошлые результаты нельзя: один неудачный запрос
            # обнулял весь список, и панель показывала «узлов нет» на живом
            # пуле. Оставляем что было и просто отмечаем время проверки.
            with self._lock:
                self.last_refresh = self._clock()
                return list(self.results.values())

        # Сколько проверять одновременно. На одноядерном сервере восемь
        # параллельных проб вместе с мостами забивают процессор, и UDP не
        # укладывается в таймаут — узлы выглядят мёртвыми, хотя живые.
        # Считаем от числа ядер, но не больше четырёх.
        workers = max(1, min(4, (os.cpu_count() or 1) * 2))
        kwargs = {
            "timeout": self.timeout,
            "workers": workers,
            "target_port": self.target_port,
            # Проверяем не «пускает ли в интернет», а «пускает ли в игру»:
            # узел с выходом в США отдаёт пустоту там, где казахстанский
            # отвечает, и без этого боты висели в «сервер не отвечает».
            "game_host": self.game_host,
            "game_port": self.game_port,
        }
        if self.target_host:
            kwargs["target_host"] = self.target_host
        try:
            results = self._prober(nodes, **kwargs)
        except Exception as exc:
            self.last_error = str(exc)
            self.log(f"[ПРОКСИ] проверка упала: {exc}")
            return []

        # Узлы, которым нужен внешний клиент, проверяются только через мост.
        # Раньше это делал check_node, но плановая проверка идёт пачкой и
        # check_node обходила — то есть весь путь через мост в бою не
        # работал, и подписка оставалась жёлтой при живом sing-box.
        results = self._probe_needs_client(results)

        with self._lock:
            self.results = {res.node.key: res for res in results}
            self.last_refresh = self._clock()
            self.last_error = ""
        self._remember_egress(results)
        self._report(results)
        return results

    def _probe_needs_client(self, results: list[ProbeResult]) -> list[ProbeResult]:
        """Догнать узлы «нужен клиент» проверкой через мост.

        Параллельно: каждый мост поднимается до 8 секунд, а узлов в
        подписке полсотни — последовательно это минуты на цикл.
        """
        bridge = self.bridge
        if bridge is None or not getattr(bridge, "available", lambda: False)():
            return results

        pending = [r for r in results if r.status == STATUS_NEEDS_CLIENT]
        if not pending:
            return results

        done: dict[str, ProbeResult] = {}
        stopped: set[str] = set()
        # Два моста за раз, не шесть. На сервере одно ядро: шесть
        # одновременно поднятых sing-box вместе с восемью проберами
        # забивали процессор, UDP-пробы не укладывались в таймаут, и все
        # живые узлы разом получали «UDP не проходит». Лучше медленнее,
        # но с правдой в статусе.
        workers = max(1, min(2, len(pending)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._probe_via_bridge, res.node, res,
                            stopped=stopped): res.node.key
                for res in pending
            }
            for future in concurrent.futures.as_completed(futures):
                key = futures[future]
                try:
                    done[key] = future.result()
                except Exception as exc:
                    self.log(f"[ПРОКСИ] мост упал на {key[:40]}: {exc}")

        if not done:
            return results

        merged = [done.get(res.node.key, res) for res in results]

        # Гасим мосты, которые не пустили трафик. На 956 МБ сервера 67
        # висящих sing-box съедали под гигабайт RSS: живые нужны боту,
        # мёртвые — просто память.
        keep = {res.node.key for res in merged if res.usable}
        for key in getattr(bridge, "running", lambda: [])():
            # Мост мог быть погашен ещё внутри _probe_via_bridge. Второй
            # stop по тому же ключу — уже не «погасить мёртвый», а шум:
            # на живом пуле это десяток лишних вызовов за цикл.
            if key not in keep and key not in stopped:
                try:
                    bridge.stop(key)
                except Exception:
                    pass
        return merged

    def _report(self, results: list[ProbeResult]) -> None:
        """Сводка так, чтобы по логу было видно, что именно чинить.

        «Рабочих 0» — это не диагноз, а его отсутствие. Забаненный адрес,
        узел без UDP и узел, который вообще не везёт трафик, лечатся
        по-разному: первый — сменой выходного адреса, второй — другим
        транспортом, третий — починкой узла.
        """
        counts: dict[str, int] = {}
        banned: list[tuple[ProbeResult, dict]] = []
        dead: list[ProbeResult] = []
        for res in results:
            entry = self._ban_entry_for(res)
            if entry is not None:
                banned.append((res, entry))
                continue
            counts[res.status] = counts.get(res.status, 0) + 1
            if not res.alive:
                dead.append(res)

        usable = counts.get(STATUS_OK, 0)
        self.log(
            f"[ПРОКСИ] проверено {len(results)} | рабочих {usable} | "
            f"адрес забанен сервером {len(banned)} | "
            f"узел не везёт UDP {counts.get(STATUS_BAD_UDP, 0)} | "
            f"узел не везёт трафик {counts.get(STATUS_NO_TRAFFIC, 0)} | "
            f"игра не отвечает {counts.get(STATUS_NO_GAME, 0)} | "
            f"нужен клиент {counts.get(STATUS_NEEDS_CLIENT, 0)} | "
            f"мёртвых {counts.get(STATUS_DEAD, 0)}"
        )
        for res, entry in banned:
            where = entry.get("address") or self._egress_of(
                res.proxy_url or res.node.key, res.node) or "?"
            self.log(
                f"    🚫 {res.node.name} ({res.node.address}:{res.node.port}) — "
                f"забанен адрес {where}: {entry.get('reason') or 'бан по IP'}"
            )
        no_udp = [r for r in results
                  if r.status == STATUS_BAD_UDP and self._ban_entry_for(r) is None]
        for res in no_udp:
            self.log(f"    ⚠ {res.node.name} — узел жив, но UDP не везёт: {res.reason}")
        no_traffic = [r for r in results
                      if r.status == STATUS_NO_TRAFFIC and self._ban_entry_for(r) is None]
        for res in no_traffic:
            self.log(f"    ⚠ {res.node.name} — узел не везёт ни TCP, ни UDP: {res.reason}")
        for res in dead:
            self.log(f"    ❌ {res.node.name} ({res.node.address}:{res.node.port}) — {res.reason}")

        if not usable and banned:
            self.log(
                "[ПРОКСИ] рабочих нет, и причина — не мёртвые узлы: живые "
                f"выходят с адресов ({len(banned)}), которые игровой сервер уже "
                "заблокировал. Это лечится сменой выходного адреса, а не "
                "починкой узла."
            )

    def _probe(self, node: VpnNode) -> ProbeResult:
        kwargs = {"timeout": self.timeout, "target_port": self.target_port,
                  "game_host": self.game_host, "game_port": self.game_port}
        if self.target_host:
            kwargs["target_host"] = self.target_host
        # Через тот же пробер, что и обновление по таймеру: тогда проверку
        # можно подменить в тестах и поведение не разъедется.
        try:
            results = self._prober([node], **kwargs)
            return results[0] if results else probe_node(node, **kwargs)
        except Exception as exc:
            return ProbeResult(
                node=node,
                status=STATUS_DEAD,
                reason=f"проверка упала: {exc}",
                checked_at=self._clock(),
            )

    def check_node(self, node: VpnNode) -> ProbeResult:
        """Проверить один узел прямо сейчас, без ожидания таймера.

        Узлы, которым нужен внешний клиент (vless/vmess/trojan/ss), сначала
        поднимаются через мост sing-box, а потом проверяются как обычный
        SOCKS5 — сквозным запросом до игрового сервера. Только так «живой по
        TCP» превращается в «рабочий»: TCP-коннект до VPN-сервера ничего не
        говорит о том, пускает ли он дальше.
        """
        result = self._probe(node)

        if result.status == STATUS_NEEDS_CLIENT:
            result = self._probe_via_bridge(node, result)

        with self._lock:
            self.results[node.key] = result
        self._remember_egress([result])
        return result

    def _probe_via_bridge(self, node: VpnNode, base: ProbeResult,
                          stopped: set | None = None) -> ProbeResult:
        """Поднять мост под узел и проверить получившийся SOCKS5.

        `stopped` — необязательное множество ключей, которые этот прогон
        уже погасил: вызывающий затем не гасит их второй раз.
        """
        bridge = self.bridge
        if bridge is None or not getattr(bridge, "available", lambda: False)():
            return base

        try:
            url = bridge.start(node.key, node.raw)
        except Exception as exc:
            base.reason = f"мост не поднялся: {exc}"
            return base
        if not url:
            base.reason = f"{node.protocol.upper()} — мост не поднялся"
            return base

        port = bridge.port_of(node.key)
        if port is None:
            base.reason = f"{node.protocol.upper()} — мост без порта"
            return base

        # Проверяем уже SOCKS5-вход моста, тем же пробером, что и ручные
        # socks5-прокси: TCP + UDP до игрового сервера.
        socks_node = VpnNode(
            name=node.name,
            protocol="socks5",
            address="127.0.0.1",
            port=int(port),
            source=node.source,
        )
        bridged = self._probe(socks_node)
        bridged.node = node
        bridged.socks_url = url
        if bridged.usable:
            bridged.status = STATUS_OK
            latency = f"{bridged.latency_ms:.0f}мс" if bridged.latency_ms else "?"
            bridged.reason = f"{node.protocol.upper()} через мост ({latency})"
        else:
            # Мост поднялся, но трафик не идёт. Статус НЕ переписываем:
            # пробер уже различил «узел работает, но не везёт датаграммы»
            # (no_udp) и «узел не везёт ничего» (no_traffic), а также
            # «игра не отвечает» (no_game). Раньше здесь всё живое
            # схлопывалось в no_udp, и по логу нельзя было понять, чинить
            # узел или менять выходной адрес.
            if bridged.status in (STATUS_OK, STATUS_UNKNOWN, STATUS_NEEDS_CLIENT, ""):
                bridged.status = STATUS_BAD_UDP if bridged.alive else STATUS_DEAD
            # Время отказа мерялось до 127.0.0.1: sing-box отвечает «не могу»
            # сам, не сходив в сеть, поэтому выходило 1-2 мс. Это время
            # локального сокета, а не пинг узла: мёртвые узлы выглядели
            # самыми быстрыми. Берём реальное время до самого узла.
            bridged.latency_ms = base.latency_ms
            bridged.reason = f"{node.protocol.upper()} через мост не пускает: {bridged.reason}"
            # Гасим мост сразу, а не в конце цикла. Раньше остановка была
            # после проверки всех узлов, и к концу набегало три десятка
            # процессов sing-box на одном ядре. Процессор захлёбывался,
            # UDP-пробы не укладывались в таймаут, и живые узлы получали
            # «UDP не проходит». Ошибка была не в узлах, а в нехватке CPU.
            try:
                bridge.stop(node.key)
                if stopped is not None:
                    stopped.add(node.key)
            except Exception:
                pass
        return bridged

    def verify_before_connect(
        self, proxy: str, *, max_age: float = 20.0
    ) -> tuple[bool, str]:
        """Сквозная проверка прокси перед запуском бота.

        Это и есть «сначала запрос, потом заход»: если прокси не пускает
        до игрового сервера, бота запускать нельзя.

        max_age — сколько секунд можно верить прошлому результату. Нужен,
        чтобы переподключение не гоняло проверку заново каждые пару секунд,
        но и не полагалось на утренний статус.
        """
        node = parse_link(proxy)
        if node is None:
            return False, f"не понял прокси {proxy!r}"
        # Забаненный адрес не проверяем вовсе: сервер его уже отверг, и
        # запускать с него бота — гарантированный бан нового аккаунта.
        # Метка лежит на выходном адресе, поэтому ищем и её: у моста в
        # аккаунте записан локальный порт, а забанен выход узла.
        egress = self._egress_of(proxy, node)
        with self._lock:
            entry = self.banned.get(node.key)
            if entry is None and egress:
                entry = self.banned.get(f"{BAN_IP_PREFIX}{egress}")
        if entry is not None:
            why = entry.get("reason") or "забанен сервером"
            where = egress or entry.get("address") or node.address
            return False, f"адрес {where} забанен сервером: {why}"
        if not node.is_direct:
            return False, f"{node.protocol} не поддерживается напрямую"

        cached = self.results.get(node.key)
        if (
            max_age > 0
            and cached is not None
            and cached.checked_at
            and (self._clock() - cached.checked_at) <= max_age
        ):
            result = cached
        else:
            result = self.check_node(node)
        if result.usable:
            latency = f"{result.latency_ms:.0f}мс" if result.latency_ms else "?"
            # В причине лежит внешний адрес, который увидит сервер. Полезно:
            # сразу видно, с какого IP пойдёт бот.
            detail = f", {result.reason}" if result.reason else ""
            return True, f"прокси живой ({latency}{detail})"
        return False, result.reason or "прокси не прошёл проверку"

    # ---------- забаненные адреса ----------
    #
    # Античит умеет банить по IP: сервер отвечает «connection banned» и не
    # пускает никого с этого адреса. Такой прокси нельзя просто пометить
    # мёртвым — проверка покажет его живым, и пул выдаст его снова, а
    # каждый следующий бот словит тот же бан. Поэтому ведём отдельный
    # список и в раздачу его не пускаем никогда.

    def node_for_proxy(self, proxy: str) -> VpnNode | None:
        """Какой узел стоит за строкой прокси бота.

        Аккаунт хранит socks5://127.0.0.1:<порт> — это порт моста, а не
        узел. Банить надо узел, иначе пометка уходит в выдуманный
        «127.0.0.1:23699», а настоящий VLESS-узел продолжает раздаваться.
        """
        text = str(proxy or "").strip()
        if not text:
            return None
        node = self.find_node(text)
        if node is not None:
            return node
        # Порт моста -> ключ узла -> сам узел.
        parsed = parse_link(text) if "://" in text else None
        if parsed is not None and parsed.address in ("127.0.0.1", "localhost"):
            bridge = self.bridge
            key_of_port = getattr(bridge, "key_of_port", None)
            if key_of_port is not None:
                try:
                    key = key_of_port(parsed.port)
                except Exception:
                    key = ""
                if key:
                    for known in self.nodes():
                        if known.key == key:
                            return known
        return None

    def _resolve_egress(self, proxy: str, *, timeout: float | None = None) -> str:
        """Выходной адрес прокси: то, с чего бот придёт на игровой сервер.

        Спрашиваем только когда прокси локальный. Удалённый адрес сам
        является выходным, а лезть в сеть на каждый бан нельзя: проверка
        бан-списка должна оставаться дешёвой.
        """
        parsed = parse_link(proxy) if "://" in str(proxy) else None
        if parsed is None or not parsed.is_direct:
            return ""
        if not is_local_address(parsed.address):
            return ""
        try:
            return tcp_egress(
                parsed.address, int(parsed.port),
                login=parsed.login, password=parsed.password,
                timeout=float(timeout or self.timeout),
            )
        except Exception:
            return ""

    def _egress_of(self, proxy: str, node=None) -> str:
        """Известный выход прокси. Сеть не трогает."""
        text = str(proxy or "").strip()
        if node is not None:
            with self._lock:
                known = self.egress.get(node.key) or ""
                if not known:
                    cached = self.results.get(node.key)
                    known = getattr(cached, "egress", "") or ""
            if known:
                return known
        with self._lock:
            known = self.egress.get(text) or ""
        if known:
            return known
        if node is not None:
            # Свой адрес — выход только у узла без моста. У узла за мостом
            # адрес узла это ВХОД, а не выход: игра видит адрес, с которого
            # узел выходит в сеть, и он другой.
            if not node.is_direct:
                return ""
            return "" if is_local_address(node.address) else node.address
        parsed = parse_link(text) if "://" in text else None
        if parsed is not None and not is_local_address(parsed.address):
            return parsed.address
        return ""

    def _remember_egress(self, results: list[ProbeResult]) -> None:
        """Запомнить выходные адреса узлов.

        Выход нужен, когда придёт кик: банить надо адрес, а не порт моста.
        Держим его на диске, потому что кик может прийти уже после
        перезапуска — кэш результатов к тому моменту пуст.
        """
        changed = False
        with self._lock:
            for res in results:
                egress = getattr(res, "egress", "") or ""
                if egress and self.egress.get(res.node.key) != egress:
                    self.egress[res.node.key] = egress
                    changed = True
        # Пишем на диск только когда это на что-то влияет: без бан-записей
        # выход не нужен никому, а лишняя запись файла ни к чему.
        if changed and self.banned:
            self.save()

    def mark_banned(self, proxy: str, reason: str = "") -> list[str]:
        """Пометить адрес забаненным. Принимает URL, ключ узла или адрес.

        Метка ложится на ВЫХОДНОЙ адрес: узел подписки — под своим ключом,
        всё остальное — под `ip:<адрес>`. Локальный порт моста не банится
        никогда: игровой сервер его не видит, и такая метка навсегда
        выкидывает живой прокси из раздачи.

        Возвращает ключи, которые реально попали в список.
        """
        node = self.node_for_proxy(proxy)
        parsed = parse_link(proxy) if "://" in str(proxy) else None
        keys: list[str] = []

        # Узел подписки (или узел за портом моста) баним под его ключом.
        if node is not None and not is_local_address(node.address):
            keys.append(node.key)

        egress = self._egress_of(proxy, node)
        if not egress:
            egress = self._resolve_egress(proxy)
            if egress and node is not None:
                with self._lock:
                    self.egress[node.key] = egress
        if egress:
            keys.append(f"{BAN_IP_PREFIX}{egress}")
        elif node is None and parsed is not None and not is_local_address(parsed.address):
            # Ни узла, ни выхода — но адрес удалённый, значит он и есть
            # выход. Баним его как адрес.
            keys.append(f"{BAN_IP_PREFIX}{parsed.address}")
        elif node is not None and not keys:
            keys.append(node.key)

        if not keys:
            # Локальный эндпоинт без известного выхода. Банить порт
            # бессмысленно, а выдумывать адрес — врать. Говорим как есть.
            self.log(
                f"[ПРОКСИ] кик пришёл с {proxy}, но выходной адрес неизвестен — "
                "помечать нечего. Локальный порт не баним: он не тот адрес, "
                "который видит игровой сервер."
            )
            return []

        listed: list[str] = []
        with self._lock:
            for key in keys:
                if not key:
                    continue
                entry = {
                    "reason": str(reason or "бан по IP"),
                    "ts": float(self._clock()),
                    "address": egress or getattr(node, "address", "") or "",
                    "name": getattr(node, "name", "") or "",
                }
                self.banned[key] = entry
                listed.append(key)
        if listed:
            self.save()
            names = ", ".join(listed)
            self.log(f"[ПРОКСИ] забанен и больше не выдаётся: {names}")
        return listed

    def unban(self, proxy: str) -> bool:
        """Снять бан вручную: адрес мог смениться или бан был ошибкой."""
        node = self.node_for_proxy(proxy)
        keys = []
        if node is not None:
            keys.append(node.key)
        parsed = parse_link(proxy) if "://" in str(proxy) else None
        if parsed is not None:
            keys.append(parsed.key)
        keys.append(str(proxy).strip())
        # Бан по выходному адресу снимается вместе с узлом: иначе узел
        # останется забаненным через `ip:`, и «разбан» ничего не изменит.
        egress = self._egress_of(proxy, node)
        if egress:
            keys.append(f"{BAN_IP_PREFIX}{egress}")
        removed = False
        with self._lock:
            for key in keys:
                if key in self.banned:
                    del self.banned[key]
                    removed = True
        if removed:
            self.save()
        return removed

    def is_banned(self, proxy: str) -> bool:
        """Забанен ли адрес. Понимает URL, ключ и голый host:port."""
        text = str(proxy or "").strip()
        if not text:
            return False
        with self._lock:
            if text in self.banned:
                return True
        node = self.node_for_proxy(text)
        if node is not None:
            with self._lock:
                if node.key in self.banned:
                    return True
            egress = self._egress_of(text, node)
            if egress:
                with self._lock:
                    return f"{BAN_IP_PREFIX}{egress}" in self.banned
            return False
        parsed = parse_link(text) if "://" in text else None
        if parsed is not None:
            with self._lock:
                if parsed.key in self.banned:
                    return True
                if f"{BAN_IP_PREFIX}{parsed.address}" in self.banned:
                    return True
                return f"{parsed.address}:{parsed.port}" in self.banned
        return False

    def banned_keys(self) -> list[str]:
        with self._lock:
            return list(self.banned)

    # ---------- выдача ----------

    def _ban_entry_for(self, res: ProbeResult) -> dict | None:
        """Бан-запись для результата проверки, если она есть.

        Смотрим три вещи, и это принципиально: узел может быть помечен под
        своим ключом, выходной адрес — под `ip:<адрес>`, а узел без моста
        сам является выходным адресом.
        """
        egress = self.egress.get(res.node.key) or res.egress
        if not egress and res.node.is_direct and not is_local_address(res.node.address):
            egress = res.node.address
        with self._lock:
            entry = self.banned.get(res.node.key)
            if entry is None and egress:
                entry = self.banned.get(f"{BAN_IP_PREFIX}{egress}")
        return entry

    def statuses(self) -> list[ProbeResult]:
        """Все известные статусы: рабочие, живые, мёртвые, забаненные.

        Забаненные помечаются поверх всего: технически адрес может быть
        живым, но играть через него нельзя, и путать его с рабочим нельзя.
        """
        with self._lock:
            items = list(self.results.values())
        order = {STATUS_OK: 0, STATUS_NEEDS_CLIENT: 1, STATUS_NO_GAME: 2,
                 STATUS_NO_TRAFFIC: 3, STATUS_BAD_UDP: 3, STATUS_DEAD: 4,
                 STATUS_BANNED: 5}

        # Копируем, а не правим на месте: результат лежит в кэше, и если
        # испортить его здесь, снятие бана не вернёт узел в работу —
        # он останется помеченным забаненным до следующей проверки.
        patched: list[ProbeResult] = []
        for res in items:
            entry = self._ban_entry_for(res)
            if entry is None:
                patched.append(res)
                continue
            copy = replace(
                res,
                status=STATUS_BANNED,
                usable=False,
                # Пинг забаненного узла ничего не значит: он остался от
                # прошлой проверки и только путает. Показывать его — врать.
                latency_ms=None,
                reason=entry.get("reason") or "забанен сервером",
            )
            patched.append(copy)
        items = patched

        def sort_key(res: ProbeResult):
            return (order.get(res.status, 4), res.latency_ms or 1e9, res.node.name)

        return sorted(items, key=sort_key)

    def usable(self) -> list[ProbeResult]:
        """Только те, с которыми бот реально может работать.

        Забаненные исключены: иначе следующая раздача снова даст адрес,
        с которого сервер уже не пускает.
        """
        return [
            res for res in self.statuses()
            if res.usable and self._ban_entry_for(res) is None
        ]

    def usable_proxies(self) -> list[str]:
        """Готовые строки прокси для ботов.

        Для узлов через мост это socks5://127.0.0.1:<порт> — у самого узла
        proxy_url пустой, он не direct.
        """
        return [res.proxy_url for res in self.usable() if res.proxy_url]

    def find_node(self, needle: str) -> VpnNode | None:
        """Найти узел среди настроенных, не требуя свежей проверки.

        Нужно для раздачи прокси: имя из /vpn local должно превращаться в
        адрес сразу, даже если проверка ещё ни разу не прогонялась.
        """
        needle = (needle or "").strip().lower()
        if not needle:
            return None
        nodes = self.nodes()
        for node in nodes:
            if needle in (node.key.lower(), node.name.lower(), node.address.lower()):
                return node
        for node in nodes:
            if needle in node.name.lower() or needle in node.address.lower():
                return node
        return None

    def socks_url_for(self, node: VpnNode) -> str:
        """Прокси-строка для бота по узлу, поднимая мост при необходимости.

        Direct-узел отдаёт сам себя. Узлу с внешним клиентом нужен мост:
        без него применить его некуда.
        """
        if node.is_direct:
            return node.proxy_url()

        bridge = self.bridge
        if bridge is None or not getattr(bridge, "available", lambda: False)():
            return ""
        try:
            return bridge.start(node.key, node.raw) or ""
        except Exception:
            return ""

    # ---------- занятость мостов ----------
    #
    # Мост — процесс на узел, и он один на всех, кто через этот узел
    # ходит. Периодическая проверка пула гасит мосты, которые «не пустили
    # трафик», но проба может провалиться на живом узле (таймаут, потеря
    # UDP, загруженный процессор) — и тогда у бота, который в этот момент
    # играет, рвётся соединение с кодом -15. Поэтому бот, подключившийся
    # через мост, помечает его занятым, а уборка откладывается до release.

    def _bridge_key_for(self, proxy: str, port: int) -> str:
        """Какой узел стоит за локальным портом моста.

        Три способа, от точного к запасному: мост знает свои порты; номер
        порта считается из ключа узла, поэтому его можно найти по списку
        узлов; наконец, сам пул уже отдавал этот адрес какому-то узлу.
        Запасные нужны, потому что мост мог поднять прошлый процесс.
        """
        bridge = self.bridge
        try:
            key = bridge.key_of_port(port) if bridge is not None else ""
        except Exception:
            key = ""
        if key:
            return key
        try:
            for node in self.nodes():
                if port_for(node.key) == int(port):
                    return node.key
        except Exception:
            pass
        try:
            for res in self.statuses():
                if proxy and proxy in (res.socks_url, res.proxy_url):
                    return res.node.key
        except Exception:
            pass
        return ""

    def acquire_proxy(self, proxy: str) -> str:
        """Отметить мост под прокси занятым живой сессией.

        Возвращает метку-держатель вида «ключ|порт»: её надо сохранить и
        отдать в release_proxy. Пустая строка — держать нечего (прокси не
        локальный или мост неизвестен).
        """
        bridge = self.bridge
        acquire = getattr(bridge, "acquire", None)
        if bridge is None or acquire is None:
            return ""
        port = _port_of(proxy)
        if port is None:
            return ""
        key = self._bridge_key_for(proxy, port)
        try:
            acquire(key, port)
        except Exception as exc:
            self.log(f"[ПРОКСИ] не смог пометить мост занятым: {exc}")
            return ""
        return f"{key}|{port}"

    def release_proxy(self, token: str) -> None:
        """Снять метку занятости. Отложенная уборка сработает здесь."""
        bridge = self.bridge
        release = getattr(bridge, "release", None)
        if bridge is None or release is None or not token:
            return
        # Ключ узла разделителем не содержит — режем по последнему.
        key, _, port_text = str(token).rpartition("|")
        try:
            port = int(port_text) if port_text.isdigit() else None
        except ValueError:
            port = None
        try:
            release(key, port)
        except Exception as exc:
            self.log(f"[ПРОКСИ] не снял метку занятости с {str(key)[:40]}: {exc}")

    def rotate(self, bot_count: int, *, per_node: int = 3) -> list[str]:
        """Раскладка прокси по ботам: несколько ботов на узел, по кругу.

        Один туннель на всех — это один IP на все аккаунты, то есть ровно
        то же самое, что заходить без прокси: сервер видит кучу аккаунтов
        с одного адреса. Поэтому рабочие узлы раздаются группами.

        Возвращает список прокси длиной bot_count. Пустая строка — своё IP.
        """
        usable = [res for res in self.usable() if res.proxy_url]
        if not usable or bot_count <= 0:
            return [""] * max(0, bot_count)

        urls: list[str] = []
        for res in usable:
            for _ in range(max(1, int(per_node))):
                urls.append(res.proxy_url)
        return [urls[i % len(urls)] for i in range(bot_count)]

    def find(self, needle: str) -> ProbeResult | None:
        """Найти узел по ключу, имени или адресу."""
        needle = (needle or "").strip().lower()
        if not needle:
            return None
        for res in self.statuses():
            node = res.node
            if needle in (node.key.lower(), node.name.lower(), node.address.lower()):
                return res
        for res in self.statuses():
            node = res.node
            if needle in node.name.lower() or needle in node.address.lower():
                return res
        return None

    def best(self) -> ProbeResult | None:
        """Самый быстрый рабочий узел."""
        working = self.usable()
        return working[0] if working else None

    def summary(self) -> dict:
        with self._lock:
            items = list(self.results.values())
        usable = [r for r in items if r.usable and self._ban_entry_for(r) is None]
        banned_items = [r for r in items if self._ban_entry_for(r) is not None]
        return {
            "subscriptions": len(self.subscriptions),
            "manual": len(self.manual),
            "total": len(items),
            "usable": len(usable),
            "alive": sum(1 for r in items if r.alive),
            "dead": sum(1 for r in items
                        if not r.alive and self._ban_entry_for(r) is None),
            "banned": len(banned_items),
            "no_udp": sum(1 for r in items if r.status == STATUS_BAD_UDP
                          and self._ban_entry_for(r) is None),
            "no_traffic": sum(1 for r in items if r.status == STATUS_NO_TRAFFIC
                              and self._ban_entry_for(r) is None),
            "last_refresh": self.last_refresh,
            "last_error": self.last_error,
            "interval": self.interval,
        }

    # ---------- таймер ----------

    def start(self) -> None:
        """Проверка по кругу: сразу и дальше каждые `interval` секунд."""
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self.running = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.running = False
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.refresh()
            except Exception as exc:  # фон не должен умирать
                self.last_error = str(exc)
                self.log(f"[ПРОКСИ] обновление упало: {exc}")
            if self._stop.wait(self.interval):
                break
