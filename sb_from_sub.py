"""Подписка -> конфиг sing-box: каждый узел на своём локальном socks-порту.

Зачем: BR банит по IP за частые входы, и проверять гипотезы об ответе на
диалог с одного адреса невозможно. Подписка даёт выходы, каждый узел
поднимается отдельным inbound'ом, бот ходит через нужный порт.

    python3 sb_from_sub.py --sub <url> --out _sb/config.json --base-port 20001
    python3 sb_from_sub.py --list
"""

import argparse
import base64
import json
import os
import re
import sys
import urllib.parse

ROOT = os.path.dirname(os.path.abspath(__file__))
SUB_FILE = os.path.join(ROOT, "_sub.txt")
CONF_DIR = os.path.join(ROOT, "_sb")


def read_sub(url=None, path=SUB_FILE):
    if url:
        import urllib.request
        req = urllib.request.Request(url, headers={"User-Agent": "sing-box/1.14"})
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
        with open(path, "wb") as f:
            f.write(raw)
    else:
        with open(path, "rb") as f:
            raw = f.read()
    text = raw.decode("utf-8", errors="replace").strip()
    # Подписка бывает base64 целиком, а бывает уже текстом.
    if "://" not in text:
        try:
            text = base64.b64decode(raw + b"=" * (-len(raw) % 4)).decode("utf-8", errors="replace")
        except Exception:
            pass
    return [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]


def _qs(rest):
    return urllib.parse.parse_qs(rest.split("?", 1)[1] if "?" in rest else "", keep_blank_values=True)


def _one(q, key, default=""):
    v = q.get(key)
    return v[0] if v else default


def parse_vless(uri):
    rest = uri[len("vless://"):]
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    if "@" not in rest:
        return None
    userinfo, hostpart = rest.split("@", 1)
    hostport = hostpart.split("/", 1)[0].split("?", 1)[0]
    if hostport.startswith("["):
        host, _, port = hostport.rpartition("]:")
        host = host.lstrip("[")
    else:
        host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return None
    q = _qs(rest)
    security = _one(q, "security", "none")
    net = _one(q, "type", "tcp")

    out = {
        "type": "vless",
        "tag": "",
        "server": host,
        "server_port": int(port),
        "uuid": userinfo,
    }
    flow = _one(q, "flow")
    if flow:
        out["flow"] = flow

    tls = {}
    if security in ("tls", "reality"):
        tls["enabled"] = True
        sni = _one(q, "sni") or _one(q, "host")
        if sni:
            tls["server_name"] = sni
        alpn = _one(q, "alpn")
        if alpn:
            tls["alpn"] = [a for a in alpn.split(",") if a]
        fp = _one(q, "fp")
        if fp:
            tls["utls"] = {"enabled": True, "fingerprint": fp}
        if security == "reality":
            pk = _one(q, "pbk")
            sid = _one(q, "sid")
            reality = {"enabled": True}
            if pk:
                reality["public_key"] = pk
            if sid:
                reality["short_id"] = sid
            tls["reality"] = reality
        if _one(q, "allowInsecure") in ("1", "true"):
            tls["insecure"] = True
        out["tls"] = tls

    if net in ("ws", "grpc", "http", "httpupgrade", "xhttp"):
        tr = {"type": "ws" if net == "xhttp" else net}
        path = _one(q, "path")
        hosthdr = _one(q, "host")
        if net == "grpc":
            svc = _one(q, "serviceName")
            tr = {"type": "grpc"}
            if svc:
                tr["service_name"] = svc
        else:
            if path:
                tr["path"] = path
            if hosthdr:
                tr["headers"] = {"Host": hosthdr}
        out["transport"] = tr

    return {"name": name, "kind": "vless", "outbound": out}


def parse_hy2(uri):
    proto = "hysteria2" if uri.startswith("hysteria2://") else "hy2"
    rest = uri[len(proto) + 3:]
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    if "@" not in rest:
        return None
    password, hostpart = rest.split("@", 1)
    hostport = hostpart.split("/", 1)[0].split("?", 1)[0]
    if hostport.startswith("["):
        host, _, port = hostport.rpartition("]:")
        host = host.lstrip("[")
    else:
        host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return None
    q = _qs(rest)
    tls = {"enabled": True}
    sni = _one(q, "sni")
    if sni:
        tls["server_name"] = sni
    if _one(q, "insecure") in ("1", "true"):
        tls["insecure"] = True
    out = {
        "type": "hysteria2",
        "tag": "",
        "server": host,
        "server_port": int(port),
        "password": urllib.parse.unquote(password),
        "tls": tls,
    }
    obfs = _one(q, "obfs")
    obfs_pw = _one(q, "obfs-password")
    if obfs and obfs_pw:
        out["obfs"] = {"type": obfs, "password": obfs_pw}
    return {"name": name, "kind": "hysteria2", "outbound": out}


def parse_ss(uri):
    rest = uri[len("ss://"):]
    name = ""
    if "#" in rest:
        rest, frag = rest.split("#", 1)
        name = urllib.parse.unquote(frag)
    if "@" in rest:
        userinfo, hostpart = rest.split("@", 1)
        try:
            decoded = base64.b64decode(userinfo + "=" * (-len(userinfo) % 4)).decode()
        except Exception:
            decoded = urllib.parse.unquote(userinfo)
    else:
        body = rest.split("?", 1)[0]
        try:
            decoded_all = base64.b64decode(body + "=" * (-len(body) % 4)).decode()
        except Exception:
            return None
        if "@" not in decoded_all:
            return None
        decoded, hostpart = decoded_all.split("@", 1)
    if ":" not in decoded:
        return None
    method, password = decoded.split(":", 1)
    hostport = hostpart.split("/", 1)[0].split("?", 1)[0]
    host, _, port = hostport.rpartition(":")
    if not host or not port.isdigit():
        return None
    out = {
        "type": "shadowsocks",
        "tag": "",
        "server": host,
        "server_port": int(port),
        "method": method,
        "password": password,
    }
    return {"name": name, "kind": "shadowsocks", "outbound": out}


PARSERS = {"vless": parse_vless, "hysteria2": parse_hy2, "hy2": parse_hy2, "ss": parse_ss}


def parse_all(lines):
    nodes = []
    for line in lines:
        if "://" not in line:
            continue
        proto = line.split("://", 1)[0].lower()
        fn = PARSERS.get(proto)
        if not fn:
            continue
        try:
            n = fn(line)
        except Exception as exc:
            print(f"[!] {proto}: {exc!r}", file=sys.stderr)
            continue
        if n:
            nodes.append(n)
    return nodes


def resolve_hosts(nodes):
    """Резолвим имена узлов заранее.

    У sing-box на Android нет доступа к системному резолверу, и запросы
    вида usiwi12.vitalvpn.site падают с «connection refused» на 127.0.0.1:53.
    Подставляем IP, имя оставляем в sni — TLS от этого не страдает.
    """
    import socket
    cache = {}
    for n in nodes:
        ob = n["outbound"]
        host = ob["server"]
        try:
            socket.inet_aton(host)
            continue
        except OSError:
            pass
        if host not in cache:
            try:
                cache[host] = socket.gethostbyname(host)
            except Exception:
                cache[host] = None
        ip = cache[host]
        if ip:
            n["resolved"] = ip
            ob["server"] = ip
        else:
            n["resolved"] = None
    return nodes


def build_config(nodes, base_port=20001):
    inbounds = []
    outbounds = []
    rules = []
    for i, n in enumerate(nodes):
        port = base_port + i
        tag = f"n{i:02d}"
        inbounds.append({
            "type": "socks",
            "tag": f"in{tag}",
            "listen": "127.0.0.1",
            "listen_port": port,
        })
        ob = dict(n["outbound"])
        ob["tag"] = tag
        outbounds.append(ob)
        rules.append({"inbound": [f"in{tag}"], "outbound": tag})
    outbounds.append({"type": "direct", "tag": "direct"})
    rules.append({"outbound": "direct"})
    return {
        "log": {"level": "warn"},
        "inbounds": inbounds,
        "outbounds": outbounds,
        "route": {"rules": rules},
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sub", default="")
    ap.add_argument("--out", default=os.path.join(CONF_DIR, "config.json"))
    ap.add_argument("--base-port", type=int, default=20001)
    ap.add_argument("--only", default="", help="фильтр по имени узла (подстрока)")
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    lines = read_sub(args.sub or None)
    nodes = parse_all(lines)
    if not nodes:
        print("[-] узлов не разобрано")
        return 1

    if args.only:
        nodes = [n for n in nodes if args.only.lower() in n["name"].lower()]
        if not nodes:
            print(f"[-] нет узлов с {args.only!r}")
            return 1

    if args.list:
        for i, n in enumerate(nodes):
            ob = n["outbound"]
            print(f"{i:3} {args.base_port + i} {n['kind']:11} {ob['server']}:{ob['server_port']:6} {n['name'][:44]}")
        return 0

    resolve_hosts(nodes)
    os.makedirs(CONF_DIR, exist_ok=True)
    conf = build_config(nodes, args.base_port)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=1)

    meta = []
    for i, n in enumerate(nodes):
        meta.append({
            "index": i,
            "port": args.base_port + i,
            "tag": f"n{i:02d}",
            "kind": n["kind"],
            "name": n["name"],
            "server": n["outbound"]["server"],
            "resolved": n.get("resolved"),
            "server_port": n["outbound"]["server_port"],
        })
    with open(os.path.join(CONF_DIR, "nodes.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)

    print(f"[+] узлов: {len(nodes)}")
    print(f"[+] конфиг: {args.out}")
    print(f"[+] порты: {args.base_port}..{args.base_port + len(nodes) - 1}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
