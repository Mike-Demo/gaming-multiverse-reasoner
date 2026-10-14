"""Gaming Multiverse Reasoner — Day 1 gate app.

Proves the locked architecture end to end: IBM Code Engine -> FalkorDB on the
VPC VSI. Bootstraps the read-only ACL user once (as the default/master user),
then serves graph queries strictly as the read-only user via GRAPH.RO_QUERY.
Stdlib only — no pip dependencies, so the image builds anywhere.
"""
import json
import os
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = os.environ.get("FALKOR_HOST", "52.118.148.170")
PORT = int(os.environ.get("FALKOR_PORT", "6379"))
MASTER_PW = os.environ.get("FALKOR_MASTER_PASSWORD", "")
READER = os.environ.get("FALKOR_READER_USER", "app_reader")
READER_PW = os.environ.get("FALKOR_READER_PASSWORD", "")
GRAPH = os.environ.get("GRAPH_NAME", "games")

_boot_lock = threading.Lock()
_boot_done = False
_boot_error = None


# ---------- minimal RESP client ----------
class Resp:
    def __init__(self, host, port, timeout=10):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.buf = b""

    def _readline(self):
        while b"\r\n" not in self.buf:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        line, self.buf = self.buf.split(b"\r\n", 1)
        return line

    def _readn(self, n):
        while len(self.buf) < n + 2:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("socket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n + 2:]
        return data

    def _read_reply(self):
        line = self._readline()
        t, rest = line[:1], line[1:]
        if t == b"+":
            return rest.decode()
        if t == b"-":
            raise RuntimeError("Redis error: " + rest.decode())
        if t == b":":
            return int(rest)
        if t == b"$":
            n = int(rest)
            if n == -1:
                return None
            return self._readn(n).decode("utf-8", "replace")
        if t in (b"*", b"~"):
            n = int(rest)
            if n == -1:
                return None
            return [self._read_reply() for _ in range(n)]
        if t == b"%":
            n = int(rest)
            return {self._read_reply(): self._read_reply() for _ in range(n)}
        if t == b"_":
            return None
        if t == b",":
            return float(rest)
        raise RuntimeError(f"Unknown RESP type {t!r}")

    def cmd(self, *args):
        out = f"*{len(args)}\r\n".encode()
        for a in args:
            b = str(a).encode()
            out += b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n"
        self.sock.sendall(out)
        return self._read_reply()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def connect(user, pw):
    c = Resp(HOST, PORT)
    c.cmd("AUTH", user, pw)
    return c


# ---------- one-time bootstrap (master user) ----------
def bootstrap():
    """Create/refresh the read-only ACL user and seed a trivial graph so the
    gate query returns a real result. Idempotent; runs once per container."""
    global _boot_done, _boot_error
    with _boot_lock:
        if _boot_done:
            return
        try:
            c = connect("default", MASTER_PW)
            c.cmd(
                "ACL", "SETUSER", READER, "reset", "on", ">" + READER_PW,
                "-@all", "+ping", "+graph.ro_query", "+graph.query_ro", "+graph.list",
                "+info", "+auth", "+select", "+client|setinfo",
            )
            c.cmd(
                "GRAPH.QUERY", GRAPH,
                "MERGE (a:Game {name:'Elden Ring'}) "
                "MERGE (b:Game {name:'Hades'}) "
                "MERGE (a)-[:SHARES_BRIDGE]->(b)",
            )
            c.close()
            _boot_done = True
        except Exception as e:  # surfaced on the status page, not hidden
            _boot_error = f"{type(e).__name__}: {e}"


def reader_query(cypher):
    c = connect(READER, READER_PW)
    try:
        return c.cmd("GRAPH.RO_QUERY", GRAPH, cypher)
    finally:
        c.close()


def whoami_user():
    c = connect(READER, READER_PW)
    try:
        return c.cmd("ACL", "WHOAMI")
    except Exception:
        return READER
    finally:
        c.close()


# ---------- HTTP ----------
class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path.startswith("/api/fetch"):
            # outbound HTTPS test from inside Code Engine
            from urllib.parse import urlparse, parse_qs
            import urllib.request as u
            q = parse_qs(urlparse(self.path).query)
            url = q.get("url", ["https://example.com/"])[0]
            try:
                rq = u.Request(url, headers={"User-Agent": "gate-probe"})
                with u.urlopen(rq, timeout=10) as resp:
                    body = resp.read(120).decode("utf-8", "replace")
                    return self._send(200, json.dumps({"ok": True, "url": url,
                                                       "status": resp.status, "head": body}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "url": url,
                                                   "error": f"{type(e).__name__}: {e}"}))
        if self.path.startswith("/api/probe"):
            # TCP probe of the VSI's own ports only (diagnostics + gate check 4)
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            try:
                port = int(q.get("port", ["6379"])[0])
            except ValueError:
                return self._send(400, json.dumps({"ok": False, "error": "bad port"}))
            host = q.get("host", [HOST])[0]
            if host not in (HOST, "174.37.172.19", "1.1.1.1"):
                return self._send(400, json.dumps({"ok": False, "error": "host not allowed"}))
            stage = "connect"
            try:
                s = socket.create_connection((host, port), timeout=8)
                s.settimeout(6)
                stage = "read/write"
                if port == 6379:
                    s.sendall(b"PING\r\n")
                    data = s.recv(256)
                elif port == 3000:
                    s.sendall(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
                    data = s.recv(256)
                else:
                    data = s.recv(256)
                s.close()
                return self._send(200, json.dumps({"ok": True, "port": port, "stage": stage,
                                                   "reply": data.decode("utf-8", "replace")[:120]}))
            except Exception as e:
                return self._send(200, json.dumps({"ok": False, "port": port, "stage": stage,
                                                   "error": f"{type(e).__name__}: {e}"}))
        if self.path == "/healthz":
            return self._send(200, json.dumps({"ok": True}))
        if self.path == "/api/graph-check":
            bootstrap()
            if _boot_error:
                return self._send(502, json.dumps({"ok": False, "stage": "bootstrap", "error": _boot_error}))
            try:
                count = reader_query("MATCH (n:Game) RETURN count(n)")
                path = reader_query(
                    "MATCH p=(a:Game {name:'Elden Ring'})-[:SHARES_BRIDGE]->(b:Game) "
                    "RETURN a.name, b.name")
                user = whoami_user()
                return self._send(200, json.dumps({
                    "ok": True, "graph": GRAPH, "connected_as": user,
                    "query_mode": "GRAPH.RO_QUERY", "game_count_raw": count,
                    "bridge_raw": path,
                }, default=str))
            except Exception as e:
                return self._send(502, json.dumps({"ok": False, "stage": "reader_query",
                                                   "error": f"{type(e).__name__}: {e}"}))
        if self.path == "/":
            bootstrap()
            status = "bootstrap ok" if _boot_done else f"bootstrap error: {_boot_error}"
            return self._send(200,
                "<h1>Gaming Multiverse Reasoner — Day 1 gate</h1>"
                f"<p>FalkorDB at {HOST}:{PORT}, graph <b>{GRAPH}</b>. {status}</p>"
                "<p>Graph check: <a href='/api/graph-check'>/api/graph-check</a></p>",
                "text/html; charset=utf-8")
        return self._send(404, json.dumps({"ok": False, "error": "not found"}))

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
