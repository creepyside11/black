"""Мост из VLESS/VMess/Trojan/SS в локальный SOCKS5 через sing-box.

Зачем это нужно. Подписка отдаёт узлы вида `vless://...`, а бот умеет
ходить только через SOCKS5 (`bot.proxy = "socks5://host:port"`). Между ними
не было ничего, поэтому такие узлы в пуле висели жёлтыми — «живой, но
нужен внешний клиент» — и применить их было нельзя. Пул показывал узлы,
которые некуда деть.

Теперь на каждый живой узел поднимается свой sing-box:

    bot  ->  socks5://127.0.0.1:<port>  ->  sing-box  ->  vless/vmess/... ->  сервер

Один узел — один порт — один процесс. Порты выдаются стабильно по имени
узла, поэтому перезапуск не ломает уже розданные прокси.

Требуется бинарник sing-box. Если его нет, мост считается недоступным и
пул ведёт себя как раньше: узел жёлтый, применить нельзя.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse

# Диапазон портов под мосты. Ниже 20000 обычно занято чем-то ещё, выше
# 30000 — уже случайные эфемерные порты ядра.
PORT_BASE = 20000
PORT_SPAN = 4000

STATE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "_vpn_bridge")

BINARY_CANDIDATES = ("sing-box", "/usr/local/bin/sing-box", "/usr/bin/sing-box",
                     "/opt/sing-box/sing-box")


class BridgeError(Exception):
    """Мост не смог подняться."""


def find_binary() -> str | None:
    """Путь к sing-box или None."""
    env = os.environ.get("SING_BOX_BIN")
    if env and os.path.exists(env):
        return env
    for cand in BINARY_CANDIDATES:
        path = shutil.which(cand) if "/" not in cand else (cand if os.path.exists(cand) else None)
        if path:
            return path
    return None


def bridge_available() -> bool:
    return find_binary() is not None


def port_for(key: str) -> int:
    """Стабильный порт под узел: один и тот же ключ — один и тот же порт."""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return PORT_BASE + int.from_bytes(digest[:4], "big") % PORT_SPAN


def _b64decode_any(text: str) -> str | None:
    raw = (text or "").strip().replace("\n", "").replace("\r", "").replace(" ", "")
    if not raw:
        return None
    raw = raw.replace("-", "+").replace("_", "/")
    pad = (-len(raw)) % 4
    try:
        return base64.b64decode(raw + "=" * pad).decode("utf-8", "replace")
    except Exception:
        return None


def _split_hostport(value: str) -> tuple[str, int]:
    value = value.strip()
    if value.startswith("["):
        host, _, rest = value[1:].partition("]")
        port = rest.lstrip(":")
        try:
            return host, int(port)
        except ValueError:
            return host, 443
    if ":" in value:
        host, _, port = value.rpartition(":")
        try:
            return host, int(port)
        except ValueError:
            return value, 443
    return value, 443


def parse_config(raw: str) -> dict | None:
    """Конфиг Xray/V2Ray -> outbound sing-box.

    Часть подписок отдаёт не ссылки, а готовые JSON-конфиги Xray: при
    запросе с клиентским User-Agent сервер решает, что клиенту нужен
    конфиг, и присылает его. Формат другой — protocol/settings/streamSettings
    вместо type/server/uuid, — поэтому ссылочный разбор на нём просто
    возвращал None, и все узлы такой подписки висели неприменимыми.
    """
    text = (raw or "").strip()
    if not text.startswith("{"):
        return None
    try:
        cfg = json.loads(text)
    except ValueError:
        return None
    if not isinstance(cfg, dict):
        return None

    # Уже готовый outbound sing-box — отдаём как есть.
    if "type" in cfg and "protocol" not in cfg:
        return cfg if cfg.get("type") in _SINGBOX_TYPES else None

    for item in cfg.get("outbounds") or []:
        if not isinstance(item, dict):
            continue
        out = _xray_outbound(item)
        if out:
            return out
    return None


_SINGBOX_TYPES = ("vless", "vmess", "trojan", "shadowsocks", "socks", "http")

_XRAY_PROTOCOLS = {
    "vless": "vless",
    "vmess": "vmess",
    "trojan": "trojan",
    "shadowsocks": "shadowsocks",
    "ss": "shadowsocks",
}


def _xray_outbound(item: dict) -> dict | None:
    """Один outbound Xray -> outbound sing-box."""
    proto = (item.get("protocol") or "").lower()
    kind = _XRAY_PROTOCOLS.get(proto)
    if not kind:
        return None

    settings = item.get("settings") or {}
    stream = item.get("streamSettings") or {}
    out: dict = {"type": kind}

    if kind in ("vless", "vmess"):
        vnext = (settings.get("vnext") or [{}])[0]
        server = vnext.get("address")
        port = vnext.get("port")
        user = (vnext.get("users") or [{}])[0]
        if not server or not port or not user.get("id"):
            return None
        out["server"] = str(server)
        out["server_port"] = int(port)
        out["uuid"] = str(user["id"])
        if kind == "vless":
            flow = user.get("flow")
            if flow:
                out["flow"] = flow
        else:
            out["security"] = (user.get("security") or "auto").lower()
            try:
                out["alter_id"] = int(user.get("alterId") or 0)
            except (TypeError, ValueError):
                out["alter_id"] = 0
    else:
        servers = (settings.get("servers") or [{}])[0]
        server = servers.get("address")
        port = servers.get("port")
        if not server or not port:
            return None
        out["server"] = str(server)
        out["server_port"] = int(port)
        if kind == "trojan":
            if not servers.get("password"):
                return None
            out["password"] = str(servers["password"])
        else:
            if not servers.get("method") or not servers.get("password"):
                return None
            out["method"] = str(servers["method"])
            out["password"] = str(servers["password"])

    tls = _xray_tls(stream)
    if tls:
        out["tls"] = tls
    elif (stream.get("security") or "").lower() in ("reality", "xtls"):
        # reality без publicKey — узел не поднимется. Молча отдать его без
        # tls значит получить «живой» прокси, который ничего не проксирует.
        return None
    transport = _xray_transport(stream)
    if transport:
        out["transport"] = transport
    return out


def _xray_tls(stream: dict) -> dict | None:
    """security=tls|reality -> блок tls для sing-box."""
    security = (stream.get("security") or "none").lower()
    if security not in ("tls", "reality", "xtls"):
        return None

    tls_settings = stream.get("tlsSettings") or {}
    reality = stream.get("realitySettings") or {}

    server_name = (
        reality.get("serverName")
        or tls_settings.get("serverName")
        or ""
    )
    tls: dict = {"enabled": True}
    if server_name:
        tls["server_name"] = str(server_name)

    fingerprint = reality.get("fingerprint") or tls_settings.get("fingerprint")
    if fingerprint:
        tls["utls"] = {"enabled": True, "fingerprint": str(fingerprint)}

    if security == "reality":
        public_key = reality.get("publicKey")
        if not public_key:
            return None
        tls["reality"] = {
            "enabled": True,
            "public_key": str(public_key),
            "short_id": str(reality.get("shortId") or ""),
        }
    elif tls_settings.get("allowInsecure"):
        tls["insecure"] = True
    return tls


def _xray_transport(stream: dict) -> dict | None:
    """network=ws|grpc|http -> блок transport для sing-box."""
    network = (stream.get("network") or "tcp").lower()
    if network in ("tcp", "none", ""):
        return None

    if network in ("ws", "websocket"):
        ws = stream.get("wsSettings") or {}
        tr: dict = {"type": "ws", "path": str(ws.get("path") or "/")}
        host = (ws.get("headers") or {}).get("Host")
        if host:
            tr["headers"] = {"Host": str(host)}
        return tr

    if network == "grpc":
        grpc = stream.get("grpcSettings") or {}
        tr = {"type": "grpc"}
        if grpc.get("serviceName"):
            tr["service_name"] = str(grpc["serviceName"])
        return tr

    if network in ("http", "h2"):
        hs = stream.get("httpSettings") or {}
        tr = {"type": "http"}
        if hs.get("path"):
            tr["path"] = str(hs["path"])
        hosts = hs.get("host") or []
        if hosts:
            tr["host"] = [str(hosts[0])]
        return tr

    return None


def parse_link(link: str) -> dict | None:
    """Разобрать узел в outbound для sing-box.

    Принимает и ссылку (vless://, vmess://, ...), и полный конфиг
    Xray/V2Ray в JSON: подписки отдают то одно, то другое в зависимости
    от User-Agent, которым их просят.
    """
    link = (link or "").strip()
    if not link:
        return None
    if link.startswith("{"):
        return parse_config(link)
    scheme = link.split("://", 1)[0].lower() if "://" in link else ""

    if scheme == "vless":
        return _parse_vless(link)
    if scheme == "vmess":
        return _parse_vmess(link)
    if scheme == "trojan":
        return _parse_trojan(link)
    if scheme in ("ss", "shadowsocks"):
        return _parse_ss(link)
    return None


def _query(body: str) -> tuple[str, str, dict]:
    name = ""
    if "#" in body:
        body, name = body.split("#", 1)
        name = urllib.parse.unquote(name)
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    return body, name, urllib.parse.parse_qs(query)


def _transport(params: dict) -> dict | None:
    """Собрать transport для ws/grpc/http, если он задан."""
    kind = (params.get("type", ["tcp"])[0] or "tcp").lower()
    if kind in ("tcp", "none", ""):
        return None
    if kind == "ws":
        tr = {"type": "ws", "path": params.get("path", ["/"])[0] or "/"}
        host = params.get("host", [""])[0]
        if host:
            tr["headers"] = {"Host": host}
        return tr
    if kind == "grpc":
        return {"type": "grpc", "service_name": params.get("serviceName", [""])[0]}
    if kind == "http":
        return {"type": "http", "path": params.get("path", ["/"])[0] or "/"}
    return None


def _tls(params: dict, server_name: str) -> dict | None:
    security = (params.get("security", ["none"])[0] or "none").lower()
    if security in ("none", ""):
        return None
    tls: dict = {"enabled": True, "server_name": server_name}
    fp = params.get("fp", [""])[0]
    if fp:
        tls["utls"] = {"enabled": True, "fingerprint": fp}
    if security == "reality":
        tls["reality"] = {
            "enabled": True,
            "public_key": params.get("pbk", [""])[0],
            "short_id": params.get("sid", [""])[0],
        }
    alpn = params.get("alpn", [""])[0]
    if alpn:
        tls["alpn"] = [a for a in alpn.split(",") if a]
    if params.get("allowInsecure", ["0"])[0] in ("1", "true"):
        tls["insecure"] = True
    return tls


def _parse_vless(link: str) -> dict | None:
    body, _name, params = _query(link[len("vless://"):])
    if "@" not in body:
        return None
    uuid, hostpart = body.rsplit("@", 1)
    host, port = _split_hostport(hostpart)
    if not host:
        return None

    outbound: dict = {
        "type": "vless",
        "server": host,
        "server_port": int(port),
        "uuid": urllib.parse.unquote(uuid),
    }
    flow = params.get("flow", [""])[0]
    if flow:
        outbound["flow"] = flow
    tls = _tls(params, params.get("sni", [""])[0] or params.get("host", [""])[0] or host)
    if tls:
        outbound["tls"] = tls
    transport = _transport(params)
    if transport:
        outbound["transport"] = transport
    return outbound


def _parse_vmess(link: str) -> dict | None:
    payload = _b64decode_any(link[len("vmess://"):])
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    host = str(data.get("add") or "")
    if not host:
        return None
    try:
        port = int(data.get("port") or 443)
    except (TypeError, ValueError):
        port = 443

    outbound: dict = {
        "type": "vmess",
        "server": host,
        "server_port": port,
        "uuid": str(data.get("id") or ""),
        "security": str(data.get("scy") or "auto"),
        "alter_id": int(data.get("aid") or 0),
    }
    net = str(data.get("net") or "tcp")
    if net == "ws":
        tr = {"type": "ws", "path": str(data.get("path") or "/")}
        if data.get("host"):
            tr["headers"] = {"Host": str(data["host"])}
        outbound["transport"] = tr
    elif net == "grpc":
        outbound["transport"] = {"type": "grpc", "service_name": str(data.get("path") or "")}

    if str(data.get("tls") or "") in ("tls", "reality"):
        tls = {"enabled": True, "server_name": str(data.get("sni") or data.get("host") or host)}
        outbound["tls"] = tls
    return outbound


def _parse_trojan(link: str) -> dict | None:
    body, _name, params = _query(link[len("trojan://"):])
    if "@" not in body:
        return None
    password, hostpart = body.rsplit("@", 1)
    host, port = _split_hostport(hostpart)
    if not host:
        return None
    outbound: dict = {
        "type": "trojan",
        "server": host,
        "server_port": int(port),
        "password": urllib.parse.unquote(password),
    }
    tls = _tls(params, params.get("sni", [""])[0] or host)
    outbound["tls"] = tls or {"enabled": True, "server_name": host}
    transport = _transport(params)
    if transport:
        outbound["transport"] = transport
    return outbound


def _parse_ss(link: str) -> dict | None:
    body = link.split("://", 1)[1]
    if "#" in body:
        body = body.split("#", 1)[0]
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)

    if "@" in body:
        userinfo, hostpart = body.rsplit("@", 1)
        decoded = _b64decode_any(userinfo) or urllib.parse.unquote(userinfo)
        if ":" not in decoded:
            return None
        method, _, password = decoded.partition(":")
    else:
        decoded = _b64decode_any(body)
        if not decoded or "@" not in decoded:
            return None
        userinfo, hostpart = decoded.rsplit("@", 1)
        if ":" not in userinfo:
            return None
        method, _, password = userinfo.partition(":")

    host, port = _split_hostport(hostpart)
    if not host:
        return None
    return {
        "type": "shadowsocks",
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }


def build_config(node_key: str, outbound: dict, port: int) -> dict:
    """Конфиг sing-box: SOCKS5 на 127.0.0.1:port -> один outbound."""
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "socks",
                "tag": "in",
                "listen": "127.0.0.1",
                "listen_port": int(port),
                "sniff": True,
            }
        ],
        "outbounds": [
            dict(outbound, tag="out"),
            {"type": "direct", "tag": "direct"},
        ],
        "route": {"rules": [], "final": "out"},
    }


def socks_alive(port: int, timeout: float = 1.0) -> bool:
    """Принимает ли мост SOCKS5-подключения."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


class VpnBridge:
    """Поднимает и держит sing-box по одному на узел."""

    def __init__(self, *, state_dir: str | None = None, binary: str | None = None,
                 log=None):
        self.state_dir = state_dir or STATE_DIR
        self.binary = binary or find_binary()
        self.log = log
        self._procs: dict[str, subprocess.Popen] = {}
        self._ports: dict[str, int] = {}
        self._lock = threading.RLock()

    # --- служебное ---

    def _note(self, text: str) -> None:
        if self.log:
            try:
                self.log(text)
            except Exception:
                pass

    def available(self) -> bool:
        return bool(self.binary and os.path.exists(self.binary))

    def _config_path(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return os.path.join(self.state_dir, f"node-{digest}.json")

    def _alloc_port(self, key: str) -> int:
        """Свободный порт под узел.

        Порт считается как хеш ключа, поэтому у двух узлов он может
        совпасть. Тогда второй мост не мог занять порт, но socks_alive()
        видел чужой слушающий сокет и рапортовал успех — узел «работал»
        через туннель соседа. Держим карту занятых портов и на коллизии
        сдвигаемся вверх.
        """
        known = self._ports.get(key)
        if known:
            return known

        taken = {port for other, port in self._ports.items() if other != key}
        port = port_for(key)
        for _ in range(PORT_SPAN):
            if port not in taken and not socks_alive(port):
                self._ports[key] = port
                return port
            port += 1
            if port >= PORT_BASE + PORT_SPAN:
                port = PORT_BASE
        # Все порты диапазона заняты — это уже аварийная ситуация.
        raise RuntimeError("нет свободного порта под мост")

    # --- жизненный цикл ---

    def start(self, key: str, link: str) -> str | None:
        """Поднять мост под узел. Возвращает socks-адрес или None."""
        if not self.available():
            return None
        outbound = parse_link(link)
        if not outbound:
            self._note(f"[МОСТ] не разобрал ссылку узла {key}")
            return None

        with self._lock:
            proc = self._procs.get(key)
            known = self._ports.get(key)
            if proc is not None and proc.poll() is None and known and socks_alive(known):
                return f"socks5://127.0.0.1:{known}"

            try:
                port = self._alloc_port(key)
            except RuntimeError as exc:
                self._note(f"[МОСТ] {exc}")
                return None
            url = f"socks5://127.0.0.1:{port}"

            os.makedirs(self.state_dir, exist_ok=True)
            cfg_path = self._config_path(key)
            cfg = build_config(key, outbound, port)
            try:
                with open(cfg_path, "w", encoding="utf-8") as f:
                    json.dump(cfg, f, ensure_ascii=False, indent=2)
            except OSError as exc:
                self._note(f"[МОСТ] не записал конфиг {key}: {exc}")
                return None

            # Порт мог остаться занятым прошлым процессом — снимаем.
            self._kill(key)

            try:
                proc = subprocess.Popen(
                    [self.binary, "run", "-c", cfg_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError as exc:
                self._note(f"[МОСТ] sing-box не запустился для {key}: {exc}")
                return None

            self._procs[key] = proc
            self._ports[key] = port

        # Ждём, пока SOCKS-порт начнёт принимать. Дольше 8 секунд ждать
        # смысла нет: либо поднялся, либо конфиг неверный.
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self._note(f"[МОСТ] sing-box упал для {key} (код {proc.returncode})")
                self._kill(key)
                return None
            if socks_alive(port):
                return url
            time.sleep(0.2)

        self._note(f"[МОСТ] порт {port} не открылся для {key}")
        self._kill(key)
        return None

    def _kill(self, key: str) -> None:
        proc = self._procs.pop(key, None)
        # Порт освобождаем вместе с процессом: иначе упавший мост навсегда
        # держит за собой номер, и следующий узел с той же коллизией уедет
        # на соседний.
        self._ports.pop(key, None)
        if proc is None:
            return
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def stop(self, key: str) -> None:
        with self._lock:
            self._kill(key)

    def stop_all(self) -> None:
        with self._lock:
            for key in list(self._procs):
                self._kill(key)

    def running(self) -> list[str]:
        with self._lock:
            return [k for k, p in self._procs.items() if p.poll() is None]

    def port_of(self, key: str) -> int | None:
        with self._lock:
            return self._ports.get(key)
