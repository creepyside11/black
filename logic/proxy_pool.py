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

from .vpn_probe import (
    ProbeResult,
    STATUS_DEAD,
    STATUS_NEEDS_CLIENT,
    STATUS_OK,
    probe_many,
    probe_node,
)
from .vpn_sub import (
    SubscriptionError,
    VpnNode,
    dedupe,
    load_subscription,
    parse_link,
)
from .vpn_bridge import VpnBridge, bridge_available

DEFAULT_INTERVAL = 300.0  # 5 минут
DEFAULT_TIMEOUT = 6.0

# Ключи для ручных прокси, чтобы они не конфликтовали с узлами подписок.
MANUAL_PREFIX = "manual"


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
        target_port: int = 5125,
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
        self._prober = prober or probe_many
        self._loader = loader or load_subscription
        self._clock = clock

        self._lock = threading.RLock()
        self.subscriptions: list[dict] = []
        self.manual: list[dict] = []
        self.results: dict[str, ProbeResult] = {}
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
        if not self.hwid:
            self.hwid = _new_hwid()
            self.save()

    def save(self) -> None:
        with self._lock:
            data = {
                "subscriptions": list(self.subscriptions),
                "manual": list(self.manual),
                "hwid": self.hwid,
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
            with self._lock:
                self.results = {}
                self.last_refresh = self._clock()
            return []

        kwargs = {
            "timeout": self.timeout,
            "target_port": self.target_port,
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
        workers = max(1, min(6, len(pending)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(self._probe_via_bridge, res.node, res): res.node.key
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
        return [done.get(res.node.key, res) for res in results]

    def _report(self, results: list[ProbeResult]) -> None:
        alive = [r for r in results if r.alive]
        usable = [r for r in results if r.usable]
        dead = [r for r in results if not r.alive]
        self.log(
            f"[ПРОКСИ] проверено {len(results)} | рабочих {len(usable)} | "
            f"живых без клиента {len(alive) - len(usable)} | мёртвых {len(dead)}"
        )
        for res in dead:
            self.log(f"    ❌ {res.node.name} ({res.node.address}:{res.node.port}) — {res.reason}")

    def _probe(self, node: VpnNode) -> ProbeResult:
        kwargs = {"timeout": self.timeout, "target_port": self.target_port}
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
        return result

    def _probe_via_bridge(self, node: VpnNode, base: ProbeResult) -> ProbeResult:
        """Поднять мост под узел и проверить получившийся SOCKS5."""
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
            # Мост поднялся, но трафик не идёт — это мёртвый узел, а не
            # «нужен клиент». Держим процесс, вдруг оживёт на следующей
            # проверке.
            bridged.status = STATUS_DEAD
            bridged.reason = f"{node.protocol.upper()} через мост не пускает: {bridged.reason}"
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

    # ---------- выдача ----------

    def statuses(self) -> list[ProbeResult]:
        """Все известные статусы, отсортированные: рабочие, живые, мёртвые."""
        with self._lock:
            items = list(self.results.values())
        order = {STATUS_OK: 0, STATUS_NEEDS_CLIENT: 1, STATUS_DEAD: 2}

        def sort_key(res: ProbeResult):
            return (order.get(res.status, 3), res.latency_ms or 1e9, res.node.name)

        return sorted(items, key=sort_key)

    def usable(self) -> list[ProbeResult]:
        """Только те, с которыми бот реально может работать."""
        return [res for res in self.statuses() if res.usable]

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
        return {
            "subscriptions": len(self.subscriptions),
            "manual": len(self.manual),
            "total": len(items),
            "usable": sum(1 for r in items if r.usable),
            "alive": sum(1 for r in items if r.alive),
            "dead": sum(1 for r in items if not r.alive),
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
