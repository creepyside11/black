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

DEFAULT_INTERVAL = 300.0  # 5 минут
DEFAULT_TIMEOUT = 6.0

# Ключи для ручных прокси, чтобы они не конфликтовали с узлами подписок.
MANUAL_PREFIX = "manual"


class ProxyPoolError(Exception):
    """Пул не смог выполнить операцию."""


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
        """Ручной прокси строкой: socks5://user:pass@host:port."""
        node = parse_link(proxy)
        if node is None:
            raise ProxyPoolError(f"не понял прокси: {proxy!r}")
        if not node.is_direct:
            raise ProxyPoolError(
                f"{node.protocol} вручную не годится: бот умеет только "
                "socks5/socks4/http"
            )
        with self._lock:
            if not any(m.get("proxy") == node.proxy_url() for m in self.manual):
                self.manual.append(
                    {
                        "proxy": node.proxy_url(),
                        "name": (name or "").strip() or node.name,
                        "added_at": self._clock(),
                    }
                )
        self.save()
        self.log(f"[ПРОКСИ] добавлен вручную: {name or node.name} -> {node.proxy_url()}")
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

        with self._lock:
            self.results = {res.node.key: res for res in results}
            self.last_refresh = self._clock()
            self.last_error = ""
        self._report(results)
        return results

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

    def check_node(self, node: VpnNode) -> ProbeResult:
        """Проверить один узел прямо сейчас, без ожидания таймера."""
        kwargs = {"timeout": self.timeout, "target_port": self.target_port}
        if self.target_host:
            kwargs["target_host"] = self.target_host
        # Через тот же пробер, что и обновление по таймеру: тогда проверку
        # можно подменить в тестах и поведение не разъедется.
        try:
            results = self._prober([node], **kwargs)
            result = results[0] if results else probe_node(node, **kwargs)
        except Exception as exc:
            result = ProbeResult(
                node=node,
                status=STATUS_DEAD,
                reason=f"проверка упала: {exc}",
                checked_at=self._clock(),
            )
        with self._lock:
            self.results[node.key] = result
        return result

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
        return [res.node.proxy_url() for res in self.usable() if res.node.proxy_url()]

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
