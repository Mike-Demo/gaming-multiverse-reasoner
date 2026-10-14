"""Gaming Multiverse Reasoner — FalkorDB x watsonx.ai x IGDB.

Serves the live demo: pick 2-4 games, get the hidden paths, bridge games and
communities connecting them. Every path is computed live in FalkorDB via
GRAPH.RO_QUERY as a read-only ACL user; hub nodes (degree-capped) are
suppressed so paths route through specific connections, not mega-hubs like
"PC" or "Single player". Narration is grounded: watsonx may only cite the
path_ids returned by the graph layer, and citations are validated in code.
Stdlib only.
"""
import concurrent.futures
import json
import os
import re
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FALKOR_HOST = os.environ.get("FALKOR_HOST", "52.116.140.13")
FALKOR_PORT = int(os.environ.get("FALKOR_PORT", "6379"))
MASTER_PW = os.environ.get("FALKOR_MASTER_PASSWORD", "")
READER = os.environ.get("FALKOR_READER_USER", "app_reader")
READER_PW = os.environ.get("FALKOR_READER_PASSWORD", "")
GRAPH = os.environ.get("GRAPH_NAME", "multiverse")
IBM_API_KEY = os.environ.get("IBM_API_KEY", "")
WX_PROJECT = os.environ.get("WATSONX_PROJECT_ID",
                            "4c482c74-ee57-41a9-a9cb-6dca1719b591")
WX_MODEL = os.environ.get("WATSONX_MODEL", "ibm/granite-4-h-small")
WX_SPACE = os.environ.get("WATSONX_SPACE_ID",
                          "ca2bbc22-83e2-4f74-9454-473383f3114b")
WX_URL = ("https://us-south.ml.cloud.ibm.com/ml/v1/text/chat"
          "?version=2023-05-29")
DEGREE_CAP = int(os.environ.get("DEGREE_CAP", "800"))
# Hub suppression for shared-attribute paths (per the execution plan):
# platforms and game modes NEVER appear in paths; genres connecting more
# than GENRE_HUB_CAP games are treated as hubs ("Adventure", "Action").
EXCLUDED_SHARED_LABELS = {"Platform", "GameMode"}
LABEL_DISPLAY = {"PlayerPerspective": "perspective",
                 "GameMode": "game mode"}
GENRE_HUB_CAP = int(os.environ.get("GENRE_HUB_CAP", "6000"))

_boot_lock = threading.Lock()
_boot_done = False


# ---------- minimal RESP client (from the Day 1 gate app) ----------
class Resp:
    def __init__(self, host, port, timeout=30):
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
            return self._readn(n).decode()
        if t == b"*":
            n = int(rest)
            if n == -1:
                return None
            return [self._read_reply() for _ in range(n)]
        raise RuntimeError("bad RESP type: " + t.decode(errors="replace"))

    def cmd(self, *args):
        parts = [f"*{len(args)}\r\n".encode()]
        for a in args:
            b = str(a).encode()
            parts.append(f"${len(b)}\r\n".encode() + b + b"\r\n")
        self.sock.sendall(b"".join(parts))
        return self._read_reply()

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


def connect(user, pw):
    c = Resp(FALKOR_HOST, FALKOR_PORT)
    c.cmd("AUTH", user, pw)
    try:
        c.cmd("CLIENT", "SETINFO", "LIB-NAME", "multiverse-reasoner")
    except Exception:
        pass
    return c


def bootstrap():
    """Ensure the read-only ACL user exists (it does not survive FalkorDB
    restarts). No seeding here — the `multiverse` graph is loader-owned."""
    global _boot_done
    with _boot_lock:
        if _boot_done:
            try:
                rc = connect(READER, READER_PW)
                rc.cmd("PING")
                rc.close()
                return
            except Exception:
                _boot_done = False
        c = connect("default", MASTER_PW)
        c.cmd("ACL", "SETUSER", READER, "reset", "on", ">" + READER_PW,
              "~*", "-@all", "+ping", "+graph.ro_query", "+graph.list",
              "+info", "+auth", "+select", "+client|setinfo")
        c.close()
        _boot_done = True


def ro_query(cypher):
    bootstrap()
    c = connect(READER, READER_PW)
    try:
        reply = c.cmd("GRAPH.RO_QUERY", GRAPH, cypher)
    finally:
        c.close()
    # reply = [headers, rows, stats]
    return reply[1] if len(reply) > 1 else []


def as_map(flat):
    if isinstance(flat, list) and flat and all(isinstance(flat[i], str)
                                               for i in range(0, len(flat), 2)):
        return {flat[i]: flat[i + 1] for i in range(0, len(flat) - 1, 2)}
    return flat


# ---------- graph queries ----------
EDGE_FOR_LABEL = {"Genre": "HAS_GENRE", "Theme": "HAS_THEME",
                  "Platform": "ON_PLATFORM", "GameMode": "HAS_MODE",
                  "PlayerPerspective": "HAS_PERSPECTIVE",
                  "Franchise": "IN_FRANCHISE", "Collection": "IN_COLLECTION",
                  "Company": "LINKED_COMPANY"}
ATTR_UNION = ("HAS_GENRE|HAS_THEME|HAS_MODE|HAS_PERSPECTIVE|ON_PLATFORM|"
              "IN_FRANCHISE|IN_COLLECTION")
COMP_UNION = "DEVELOPED_BY|PUBLISHED_BY|PORTED_BY|SUPPORTED_BY"
CY_SHARED = ("MATCH (a:Game {igdb_id:$a})-[r1]-(x)-[r2]-(b:Game {igdb_id:$b}) "
             "RETURN labels(x)[0], x.igdb_id, x.name, x.degree, "
             "coalesce(x.rating_count, 0), type(r1), type(r2)")
CY_SIMILAR = ("MATCH (a:Game {igdb_id:$a}), (b:Game {igdb_id:$b}) "
              "WITH shortestPath((a)-[:SIMILAR_TO*..4]-(b)) AS p "
              "UNWIND nodes(p) AS n RETURN n.igdb_id, n.name")
CY_COMPANY = (
    f"MATCH (a:Game {{igdb_id:$a}})-[r1:{COMP_UNION}]->(x:Company)"
    f"<-[r2:{COMP_UNION}]-(m:Game)-[r3:{COMP_UNION}]->(y:Company)"
    f"<-[r4:{COMP_UNION}]-(b:Game {{igdb_id:$b}}) "
    "WHERE x.degree <= $cap AND y.degree <= $cap "
    "AND m.igdb_id <> $a AND m.igdb_id <> $b "
    "RETURN m.igdb_id, m.name, coalesce(m.rating_count, 0), "
    "x.igdb_id, x.name, x.degree, type(r1), type(r2), "
    "y.igdb_id, y.name, y.degree, type(r3), type(r4) "
    "ORDER BY x.degree + y.degree ASC, coalesce(m.rating_count, 0) DESC LIMIT 3")


def fill(cypher, **kw):
    out = cypher
    for k, v in kw.items():
        out = out.replace("$" + k, str(int(v)))
    return out


def node(label, igdb_id, name):
    return {"key": f"{label}:{igdb_id}", "label": label,
            "igdb_id": igdb_id, "name": name}


def pair_paths(a_id, a_name, b_id, b_name):
    paths = []
    A = node("Game", a_id, a_name)
    B = node("Game", b_id, b_name)

    def edge(n1, n2, etype):
        return {"a": n1["key"], "b": n2["key"], "type": etype}

    # --- shared neighbours: attributes and common similar games ---
    shared_rows = ro_query(fill(CY_SHARED + " LIMIT 25", a=a_id, b=b_id))
    attrs, games_x = [], []
    for label, xid, name, degree, rc, t1, t2 in shared_rows:
        if label == "Game":
            games_x.append((xid, name, rc))
        elif label in EXCLUDED_SHARED_LABELS:
            continue  # hub suppression: platforms / modes never route paths
        elif label == "Genre" and (degree or 0) > GENRE_HUB_CAP:
            continue  # broad-genre hub ("Adventure", "Action", ...)
        else:
            attrs.append((label, xid, name, degree or 0, t1, t2))
    attrs.sort(key=lambda r: r[3])
    seen_types = set()
    best_attr = None
    for label, xid, name, degree, t1, t2 in attrs:
        X = node(label, xid, name)
        if best_attr is None or degree < best_attr[3]:
            best_attr = (label, xid, name, degree)
        if label in seen_types:
            continue
        seen_types.add(label)
        paths.append({
            "kind": "shared", "nodes": [A, X, B],
            "edges": [edge(A, X, t1), edge(X, B, t2)],
            "via": name, "via_type": label, "degree": degree,
            "description": f"{a_name} and {b_name} share "
                           f"{LABEL_DISPLAY.get(label, label.lower())} "
                           f"“{name}” (connects {degree} games in the slice).",
            "cypher": CY_SHARED})
    games_x.sort(key=lambda r: -r[2])
    for xid, name, rc in games_x[:2]:
        X = node("Game", xid, name)
        paths.append({
            "kind": "common-neighbor", "nodes": [A, X, B],
            "edges": [edge(A, X, "SIMILAR_TO"), edge(X, B, "SIMILAR_TO")],
            "via": name, "via_type": "Game", "degree": None,
            "description": f"IGDB lists {name} as similar to both "
                           f"{a_name} and {b_name}.",
            "cypher": CY_SHARED})

    # --- community mates via the most specific shared attribute ---
    if best_attr:
        label, xid, name, degree = best_attr
        mate_rows = ro_query(fill(
            f"MATCH (x:{label} {{igdb_id:$x}})<--(m:Game) "
            "WHERE m.igdb_id <> $a AND m.igdb_id <> $b "
            "RETURN DISTINCT m.igdb_id, m.name, coalesce(m.rating_count, 0) "
            "ORDER BY coalesce(m.rating_count, 0) DESC LIMIT 5",
            x=xid, a=a_id, b=b_id))
        mates = [r[1] for r in mate_rows]
        for p in paths:
            if p["kind"] == "shared" and p["nodes"][1]["igdb_id"] == xid:
                p["mates"] = mates
                p["description"] += (" Other notable games in that "
                                      "neighbourhood: " + ", ".join(mates[:3]) + ".")
                break

    # --- similar-game chain ---
    sim_rows = ro_query(fill(CY_SIMILAR, a=a_id, b=b_id))
    if sim_rows and len(sim_rows) >= 3:
        seq = [{"id": r[0], "name": r[1]} for r in sim_rows]
        if len(seq) >= 3:
            nodes = [node("Game", s["id"], s["name"]) for s in seq]
            dup = any(p["kind"] == "common-neighbor" and len(p["nodes"]) == 3
                      and p["nodes"][1]["igdb_id"] == nodes[1]["igdb_id"]
                      for p in paths)
            if not dup:
                paths.append({
                    "kind": "similar-chain", "nodes": nodes,
                    "edges": [edge(nodes[i], nodes[i + 1], "SIMILAR_TO")
                              for i in range(len(nodes) - 1)],
                    "via": nodes[1]["name"], "via_type": "Game",
                    "degree": None,
                    "description": "Similar-game chain: " +
                                   " → ".join(n["name"] for n in nodes) + ".",
                    "cypher": CY_SIMILAR})

    # --- company bridges (degree-capped) ---
    comp_rows = ro_query(fill(CY_COMPANY, a=a_id, b=b_id, cap=DEGREE_CAP))
    seen_bridges = set()
    for (mid, mname, mrc, xid, xn, xd, t1, t2,
         yid, yn, yd, t3, t4) in comp_rows:
        bkey = (mid, xid, yid)
        if bkey in seen_bridges:
            continue  # same bridge via a different role combination
        seen_bridges.add(bkey)
        M = node("Game", mid, mname)
        X = node("Company", xid, xn)
        nodes = [A, X, M]
        edges = [edge(A, X, t1), edge(X, M, t2)]
        if yid != xid:
            Y = node("Company", yid, yn)
            nodes.append(Y)
            edges += [edge(M, Y, t3), edge(Y, B, t4)]
            desc = (f"{mname} bridges the pair: {a_name} connects to "
                    f"{xn}, {b_name} to {yn}, and {mname} links to both "
                    f"companies ({xn} connects {xd} games, {yn} {yd}).")
        else:
            edges.append(edge(X, B, t4))
            desc = (f"Both games tie to {xn} (connects {xd} games), and "
                    f"{mname} is another game linked to {xn}.")
        nodes.append(B)
        paths.append({
            "kind": "company-bridge", "nodes": nodes, "edges": edges,
            "via": mname, "via_type": "Game", "degree": (xd or 0) + (yd or 0),
            "description": desc, "cypher": CY_COMPANY})

    kind_rank = {"company-bridge": 0, "similar-chain": 1,
                 "common-neighbor": 1, "shared": 2}
    paths.sort(key=lambda p: (kind_rank.get(p["kind"], 9),
                              p["degree"] if p["degree"] is not None else 10**9))
    return paths


def game_info(gid):
    rows = ro_query(fill(
        "MATCH (g:Game {igdb_id:$i}) RETURN g.name, "
        "coalesce(g.rating_count, 0), g.community, g.community_size", i=gid))
    if not rows:
        return None
    name, rc, community, csize = rows[0]
    mates = []
    if community is not None:
        mrows = ro_query(fill(
            "MATCH (m:Game {community: $c}) WHERE m.igdb_id <> $i "
            "RETURN m.name, coalesce(m.rating_count, 0) "
            "ORDER BY coalesce(m.rating_count, 0) DESC LIMIT 6",
            c=community, i=gid))
        mates = [r[0] for r in mrows]
    return {"igdb_id": gid, "name": name, "rating_count": rc,
            "community": community, "community_size": csize,
            "community_top": mates}


def api_resolve(qstr):
    q = (qstr or "").strip()
    if len(q) < 2:
        return {"results": []}
    esc = q.lower().replace("\\", "\\\\").replace("'", "\\'")
    slug = re.sub(r"[^a-z0-9]+", "-", q.lower()).strip("-")
    rows = ro_query(
        "MATCH (g:Game) WHERE toLower(g.name) CONTAINS '" + esc +
        "' OR g.slug CONTAINS '" + slug + "' "
        "RETURN g.igdb_id, g.name, coalesce(g.rating_count, 0), "
        "g.first_release_date LIMIT 40")
    ql = q.lower()

    def rank(r):
        name = (r[1] or "").lower()
        tier = 0 if name == ql else (1 if name.startswith(ql) else 2)
        return (tier, -(r[2] or 0))

    rows.sort(key=rank)
    out = []
    for gid, name, rc, frd in rows[:8]:
        year = None
        if frd:
            year = time.gmtime(frd).tm_year
        out.append({"igdb_id": gid, "name": name, "rating_count": rc,
                    "year": year})
    return {"results": out}


def api_random():
    """Four different games for the Surprise-me button.

    Pool = the 600 most-rated games (well connected, so bridges exist);
    shuffled server-side, preferring four distinct detected communities
    so the paths between them are interesting rather than same-cluster.
    """
    import random
    rows = ro_query(
        "MATCH (g:Game) WHERE coalesce(g.rating_count, 0) >= 250 "
        "RETURN g.igdb_id, g.name, coalesce(g.rating_count, 0), g.community "
        "ORDER BY coalesce(g.rating_count, 0) DESC LIMIT 600")
    pool = [list(r) for r in rows]
    random.shuffle(pool)
    picks, seen_comms = [], set()
    for r in pool:
        if r[3] is not None and r[3] in seen_comms:
            continue
        picks.append(r)
        if r[3] is not None:
            seen_comms.add(r[3])
        if len(picks) == 4:
            break
    if len(picks) < 4:
        for r in pool:
            if r not in picks:
                picks.append(r)
            if len(picks) == 4:
                break
    return {"games": [{"igdb_id": r[0], "name": r[1], "rating_count": r[2]}
                      for r in picks]}


def api_bridges(ids):
    ids = [int(i) for i in ids][:4]
    if len(ids) < 2:
        raise ValueError("pick at least two games")
    t0 = time.time()
    games = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        for info in ex.map(game_info, ids):
            if info:
                games[info["igdb_id"]] = info
    ids = [i for i in ids if i in games]
    pairs = []
    jobs = [(ids[i], ids[j]) for i in range(len(ids))
            for j in range(i + 1, len(ids))]
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(pair_paths, a, games[a]["name"], b,
                          games[b]["name"]): (a, b) for a, b in jobs}
        results = {}
        for fut in concurrent.futures.as_completed(futs):
            results[futs[fut]] = fut.result()
    counter = 0
    for a, b in jobs:
        plist = results[(a, b)]
        for p in plist:
            counter += 1
            p["path_id"] = f"p{counter}"
        pairs.append({"a": a, "b": b, "a_name": games[a]["name"],
                      "b_name": games[b]["name"], "paths": plist})
    return {"games": [games[i] for i in ids], "pairs": pairs,
            "stats": {"took_ms": int((time.time() - t0) * 1000),
                      "graph": GRAPH, "degree_cap": DEGREE_CAP}}


# ---------- narration: watsonx, grounded + validated ----------
_iam_cache = {"token": None, "exp": 0}


def iam_token():
    if _iam_cache["token"] and time.time() < _iam_cache["exp"] - 60:
        return _iam_cache["token"]
    data = ("grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey="
            + IBM_API_KEY).encode()
    req = urllib.request.Request(
        "https://iam.cloud.ibm.com/identity/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = json.loads(r.read())
    _iam_cache["token"] = body["access_token"]
    _iam_cache["exp"] = time.time() + int(body.get("expires_in", 3600))
    return _iam_cache["token"]


def watsonx_claims(facts):
    prompt = (
        "You explain hidden connections between video games using ONLY the "
        "graph facts below. Each fact has a path_id. Write 3 to 5 short, "
        "specific claims (one or two sentences each). Every claim must cite "
        "the path_id(s) it is based on. Never invent games, companies, "
        "people, or facts that are not in the list. Respond with ONLY a "
        "JSON object, no other text:\n"
        '{"claims": [{"text": "...", "path_ids": ["p1"]}]}\n\nFacts:\n'
        + json.dumps(facts, indent=1))
    body = {"model_id": WX_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 700, "temperature": 0.2}
    if WX_SPACE:
        body["space_id"] = WX_SPACE
    else:
        body["project_id"] = WX_PROJECT
    req = urllib.request.Request(
        WX_URL, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + iam_token()})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    text = out["choices"][0]["message"]["content"].strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    # Models sometimes add prose around the JSON; take the first object.
    start = text.find("{")
    if start > 0:
        text = text[start:]
    obj, _ = json.JSONDecoder().raw_decode(text)
    return obj["claims"]


def api_narrate(payload):
    pairs = payload.get("pairs") or []
    facts, by_id = [], {}
    # Round-robin across pairs so narration covers every pair, not just
    # the first ones in the list.
    ordered = []
    maxlen = max((len(p.get("paths", [])) for p in pairs), default=0)
    for i in range(maxlen):
        for pair in pairs:
            ps = pair.get("paths", [])
            if i < len(ps):
                ordered.append((pair, ps[i]))
    for pair, p in ordered:
        facts.append({"path_id": p["path_id"], "pair": [pair.get("a_name"),
                      pair.get("b_name")], "kind": p["kind"],
                      "fact": p["description"]})
        by_id[p["path_id"]] = p
    facts = facts[:14]
    known = {f["path_id"] for f in facts}
    claims = None
    if IBM_API_KEY and known:
        try:
            raw = watsonx_claims(facts)
            claims = []
            for c in raw:
                txt = str(c.get("text", "")).strip()
                pids = [p for p in c.get("path_ids", []) if p in known]
                if txt and pids and len(txt) <= 400:
                    claims.append({"text": txt, "path_ids": pids})
            if not claims:
                claims = None
        except Exception as e:
            print(f"narrate: watsonx failed: {type(e).__name__}: {e}",
                  flush=True)
            claims = None
    if claims:
        return {"source": "watsonx", "model": WX_MODEL, "claims": claims}
    return {"source": "template",
            "note": "Graph-derived statements (watsonx narration unavailable "
                    "or failed validation).",
            "claims": [{"text": f["fact"], "path_ids": [f["path_id"]]}
                       for f in facts[:6]]}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/" or self.path.startswith("/?"):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/api/resolve"):
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                self._json(api_resolve((qs.get("q") or [""])[0]))
            elif self.path.startswith("/api/random"):
                self._json(api_random())
            elif self.path.startswith("/api/bridges"):
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(self.path).query)
                raw = (qs.get("ids") or [""])[0]
                ids = [x for x in raw.split(",") if x.strip()]
                self._json(api_bridges(ids))
            elif self.path.startswith("/api/health"):
                rows = ro_query("MATCH (g:Game) RETURN count(g)")
                self._json({"ok": True, "graph": GRAPH,
                            "games": rows[0][0] if rows else 0,
                            "connected_as": READER})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)

    def do_POST(self):
        try:
            if self.path.startswith("/api/narrate"):
                n = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(n) or b"{}")
                self._json(api_narrate(payload))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaming Multiverse Reasoner</title>
<style>
:root{--bg:#0d1117;--panel:#161b22;--line:#30363d;--txt:#e6edf3;--dim:#8b949e;
--acc:#7ee787;--acc2:#79c0ff;--warn:#f0b429}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif}
header{padding:20px 24px 8px}h1{margin:0;font-size:26px}
.sub{color:var(--dim);max-width:900px}
main{display:flex;gap:16px;padding:12px 24px 24px;flex-wrap:wrap}
#left{flex:2 1 560px;min-width:340px}#right{flex:1 1 360px;min-width:320px;
max-height:calc(100vh - 170px);overflow:auto}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:14px;margin-bottom:14px}
.slots{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:10px}
.slot label{font-size:12px;color:var(--dim);display:block;margin-bottom:4px}
.slot input{width:100%;padding:9px;border-radius:8px;border:1px solid var(--line);
background:#0d1117;color:var(--txt)}
.results{position:relative}.dropdown{position:absolute;z-index:5;background:#1c2330;
border:1px solid var(--line);border-radius:8px;width:100%;max-height:230px;overflow:auto}
.dropdown div{padding:7px 10px;cursor:pointer}.dropdown div:hover{background:#26314a}
.dropdown div.sel{background:#26314a}
.dropdown small{color:var(--dim)}
button{background:var(--acc);color:#06280f;border:0;border-radius:8px;
padding:11px 18px;font-weight:700;cursor:pointer;font-size:15px}
button.secondary{background:#26314a;color:var(--txt)}
button:disabled{opacity:.5}
#go{margin-top:12px}
svg{width:100%;height:560px;background:#0a0e14;border-radius:10px}
.edge{stroke:#3d4a5f;stroke-width:1.4}.edge.hot{stroke:var(--acc);stroke-width:3}
.edge.dim{opacity:.12}.node.dim{opacity:.15}
.nlabel{fill:var(--txt);font-size:11px;text-anchor:middle;pointer-events:none}
.pairtitle{font-weight:700;margin:10px 0 6px}
.path{border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin:7px 0;
cursor:pointer;background:#10151d}.path:hover{border-color:var(--acc2)}
.path.active{border-color:var(--acc);background:#12241a}
.kind{font-size:11px;color:var(--acc2);text-transform:uppercase;letter-spacing:.06em}
.path details{margin-top:5px}.path code{font-size:11px;color:var(--dim);
white-space:pre-wrap;word-break:break-word}
.claim{border-left:3px solid var(--acc2);padding:6px 10px;margin:8px 0;
background:#10151d;cursor:pointer}.claim:hover{border-color:var(--acc)}
.badge{font-size:11px;background:#26314a;border-radius:20px;padding:2px 8px;color:var(--acc2)}
.comm{font-size:13px;color:var(--dim)}.comm b{color:var(--txt)}
.err{color:#ff7b72}.small{font-size:12px;color:var(--dim)}
</style></head><body>
<header><h1>🕹️ Gaming Multiverse Reasoner</h1>
<p class="sub">Pick any set of games and see the <b>hidden paths</b>, <b>bridge
games</b> and <b>communities</b> that connect them. Every path is computed live
in <b>FalkorDB</b> from a frozen IGDB snapshot (20,371 games, 14 Oct 2026) with
hub suppression: platforms and game modes never appear in paths, broad genres
(connecting 6,000+ games, like “Adventure”) are suppressed, and company
bridges only route through companies connecting 800 games or fewer — so “PC”
and “Single player” can never win. Narration by <b>watsonx.ai</b> may only
cite the graph’s own path IDs — citations are validated in code.</p></header>
<main>
<div id="left">
 <div class="panel"><div class="slots" id="slots"></div>
 <button id="go">Find the hidden paths</button>
 <button class="secondary" id="rand">🎲 Surprise me — 4 random games</button>
 <span class="small" id="status"></span></div>
 <div class="panel"><svg id="graph" viewBox="0 0 900 560"></svg>
 <div class="small">Click any path or claim to highlight it in the graph.</div></div>
</div>
<div id="right">
 <div class="panel"><b>Why are they connected?</b>
 <span class="badge" id="narr-src" style="display:none"></span>
 <div id="claims"><p class="small">Run a search, then ask for the story.</p></div>
 <button class="secondary" id="narrate" disabled>Explain with watsonx</button></div>
 <div class="panel"><b>Paths</b><div id="paths"><p class="small">No search yet.</p></div></div>
 <div class="panel"><b>Communities</b><div id="comms"><p class="small">—</p></div></div>
</div>
</main>
<script>
const DEMO=[["Elden Ring",119133],["Hades",113112],["Stardew Valley",17000],
["Baldur’s Gate III",119171]];
const slotsEl=document.getElementById('slots');
let selected=DEMO.map(([name,id])=>({name,igdb_id:id}));
DEMO.forEach(([name,id],i)=>{
  const d=document.createElement('div');d.className='slot';
  d.innerHTML=`<label>Game ${i+1}</label><div class="results">
  <input id="in${i}" value="${name}" autocomplete="off">
  <div class="dropdown" id="dd${i}" style="display:none"></div></div>`;
  slotsEl.appendChild(d);
  const inp=d.querySelector('input'),dd=d.querySelector('.dropdown');
  let timer,items=[],hi=-1;
  const closeDD=()=>{dd.style.display='none';hi=-1};
  const paint=()=>{[...dd.children].forEach((c,k)=>c.classList.toggle('sel',k===hi))};
  const pick=g=>{if(!g)return;selected[i]={name:g.name,igdb_id:g.igdb_id};
    inp.value=g.name;closeDD()};
  inp.addEventListener('input',()=>{clearTimeout(timer);
    timer=setTimeout(async()=>{
      const q=inp.value.trim();if(q.length<2){closeDD();return}
      const r=await fetch('/api/resolve?q='+encodeURIComponent(q));
      const j=await r.json();items=j.results||[];hi=items.length?0:-1;
      dd.innerHTML='';
      if(!items.length){const o=document.createElement('div');
        o.innerHTML='<small>No matches — try another title</small>';
        dd.appendChild(o)}
      items.forEach((g,k)=>{
        const o=document.createElement('div');
        o.innerHTML=`${g.name} <small>${g.year||''} · ★${g.rating_count}</small>`;
        o.onmouseenter=()=>{hi=k;paint()};
        o.onclick=()=>pick(g);
        dd.appendChild(o)});
      paint();
      dd.style.display='block';
    },220)});
  inp.addEventListener('keydown',e=>{
    if(dd.style.display==='none')return;
    if(e.key==='ArrowDown'){e.preventDefault();hi=Math.min(hi+1,items.length-1);paint()}
    else if(e.key==='ArrowUp'){e.preventDefault();hi=Math.max(hi-1,0);paint()}
    else if(e.key==='Enter'){if(items.length){e.preventDefault();pick(items[Math.max(hi,0)])}}
    else if(e.key==='Escape'){closeDD()}
  });
  inp.addEventListener('blur',()=>setTimeout(closeDD,150));
});
let DATA=null,activePaths=new Set();
const KIND_LABEL={shared:'shared thread','common-neighbor':'common neighbour',
'similar-chain':'similar-game chain','company-bridge':'company bridge'};
document.getElementById('go').onclick=async()=>{
  const ids=selected.filter(Boolean).map(s=>s.igdb_id);
  const st=document.getElementById('status');st.textContent=' querying FalkorDB…';
  document.getElementById('go').disabled=true;
  try{
    const r=await fetch('/api/bridges?ids='+ids.join(','));
    DATA=await r.json();
    if(DATA.error)throw new Error(DATA.error);
    st.textContent=` done in ${DATA.stats.took_ms} ms · ${DATA.pairs.reduce((n,p)=>n+p.paths.length,0)} paths`;
    renderGraph();renderPaths();renderComms();
    document.getElementById('narrate').disabled=false;
  }catch(e){st.innerHTML=' <span class="err">'+e.message+'</span>'}
  document.getElementById('go').disabled=false;
};
function renderPaths(){
  const el=document.getElementById('paths');el.innerHTML='';
  DATA.pairs.forEach(pr=>{
    const t=document.createElement('div');t.className='pairtitle';
    t.textContent=pr.a_name+' ↔ '+pr.b_name;el.appendChild(t);
    if(!pr.paths.length){const p=document.createElement('p');p.className='small';
      p.textContent='No paths under the hub cap for this pair.';el.appendChild(p)}
    pr.paths.forEach(p=>{
      const d=document.createElement('div');d.className='path';d.dataset.pid=p.path_id;
      d.innerHTML=`<span class="kind">${KIND_LABEL[p.kind]||p.kind}</span>
      <span class="badge">${p.path_id}</span><br>${p.description}
      <details><summary class="small">Cypher</summary><code>${p.cypher}</code></details>`;
      d.onclick=()=>setActive([p.path_id],d);
      el.appendChild(d)});
  });
}
function renderComms(){
  const el=document.getElementById('comms');el.innerHTML='';
  DATA.games.forEach(g=>{
    const d=document.createElement('div');d.className='comm';
    d.innerHTML=g.community==null?`<b>${g.name}</b> — no detected community`
      :`<b>${g.name}</b> — community of ${g.community_size} games
        (label propagation over similar-game links). Peers: ${g.community_top.join(', ')}`;
    el.appendChild(d)});
}
function setActive(pids,el){
  activePaths=new Set(pids);
  document.querySelectorAll('.path').forEach(x=>x.classList.toggle('active',
    pids.includes(x.dataset.pid)));
  document.querySelectorAll('.edge').forEach(e=>{
    const on=pids.includes(e.dataset.pid);
    e.classList.toggle('hot',on);e.classList.toggle('dim',!on&&pids.length)});
  const used=new Set();
  document.querySelectorAll('.edge').forEach(e=>{if(pids.includes(e.dataset.pid)){
    used.add(e.dataset.a);used.add(e.dataset.b)}});
  document.querySelectorAll('.node').forEach(n=>{
    n.classList.toggle('dim',pids.length&&!used.has(n.dataset.key))});
  if(el)el.scrollIntoView({block:'nearest',behavior:'smooth'});
}
document.getElementById('narrate').onclick=async()=>{
  const btn=document.getElementById('narrate');btn.disabled=true;
  const box=document.getElementById('claims');box.innerHTML='<p class="small">asking watsonx…</p>';
  try{
    const r=await fetch('/api/narrate',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(DATA)});
    const j=await r.json();
    const src=document.getElementById('narr-src');src.style.display='inline-block';
    src.textContent=j.source==='watsonx'?('watsonx · '+j.model):'template fallback';
    box.innerHTML='';
    (j.claims||[]).forEach(c=>{
      const d=document.createElement('div');d.className='claim';
      d.innerHTML=c.text+' <span class="badge">'+c.path_ids.join(', ')+'</span>';
      d.onclick=()=>setActive(c.path_ids,null);
      box.appendChild(d)});
    if(j.note){const n=document.createElement('p');n.className='small';
      n.textContent=j.note;box.appendChild(n)}
  }catch(e){box.innerHTML='<p class="err">'+e.message+'</p>'}
  btn.disabled=false;
};
document.getElementById('rand').onclick=async()=>{
  const btn=document.getElementById('rand');btn.disabled=true;
  const st=document.getElementById('status');
  st.textContent=' picking four random games…';
  try{
    const r=await fetch('/api/random');const j=await r.json();
    if(j.error)throw new Error(j.error);
    (j.games||[]).forEach((g,i)=>{
      selected[i]={name:g.name,igdb_id:g.igdb_id};
      const inp=document.getElementById('in'+i);if(inp)inp.value=g.name});
    if((j.games||[]).length>=2)document.getElementById('go').click();
  }catch(e){st.innerHTML=' <span class="err">'+e.message+'</span>'}
  btn.disabled=false;
};
// --- tiny force layout + SVG render ---
function renderGraph(){
  const svg=document.getElementById('graph');svg.innerHTML='';
  const nodes=new Map(),edges=[];
  DATA.pairs.forEach(pr=>pr.paths.forEach(p=>{
    p.nodes.forEach(n=>{if(!nodes.has(n.key))nodes.set(n.key,
      {...n,x:450+(Math.random()-.5)*500,y:280+(Math.random()-.5)*340,vx:0,vy:0})});
    p.edges.forEach(e=>edges.push({...e,pid:p.path_id}))}));
  const sel=new Set(DATA.games.map(g=>'Game:'+g.igdb_id));
  const arr=[...nodes.values()];
  // Deterministic ellipse start + capped forces: uncapped repulsion in the
  // first version flung every node into the corners and pinned them there.
  arr.forEach((n,i)=>{const a=2*Math.PI*i/Math.max(arr.length,1);
    n.x=450+210*Math.cos(a);n.y=280+150*Math.sin(a);n.vx=0;n.vy=0});
  for(let t=0;t<300;t++){
    for(let i=0;i<arr.length;i++)for(let j=i+1;j<arr.length;j++){
      const a=arr[i],b=arr[j];let dx=a.x-b.x,dy=a.y-b.y;
      let d2=Math.max(dx*dx+dy*dy,25),d=Math.sqrt(d2);
      let f=Math.min(5000/d2,2.5),ux=dx/d,uy=dy/d;
      a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f}
    edges.forEach(e=>{
      const a=nodes.get(e.a),b=nodes.get(e.b);if(!a||!b)return;
      let dx=b.x-a.x,dy=b.y-a.y,d=Math.sqrt(dx*dx+dy*dy)+0.01;
      let f=(d-135)*0.018,ux=dx/d,uy=dy/d;
      a.vx+=ux*f;a.vy+=uy*f;b.vx-=ux*f;b.vy-=uy*f});
    arr.forEach(n=>{n.vx+=(450-n.x)*0.006;n.vy+=(280-n.y)*0.006;
      const sp=Math.hypot(n.vx,n.vy);
      if(sp>6){n.vx*=6/sp;n.vy*=6/sp}
      n.x+=n.vx;n.y+=n.vy;n.vx*=0.78;n.vy*=0.78;
      n.x=Math.max(70,Math.min(830,n.x));n.y=Math.max(45,Math.min(515,n.y))});
  }
  const NS='http://www.w3.org/2000/svg';
  edges.forEach(e=>{
    const a=nodes.get(e.a),b=nodes.get(e.b);if(!a||!b)return;
    const l=document.createElementNS(NS,'line');
    l.setAttribute('x1',a.x);l.setAttribute('y1',a.y);
    l.setAttribute('x2',b.x);l.setAttribute('y2',b.y);
    l.setAttribute('class','edge');l.dataset.pid=e.pid;
    l.dataset.a=e.a;l.dataset.b=e.b;svg.appendChild(l)});
  const COL={Game:'#7ee787',Company:'#f0b429',Genre:'#79c0ff',Theme:'#d2a8ff',
  Platform:'#8b949e',GameMode:'#ffa657',PlayerPerspective:'#a5d6ff',
  Franchise:'#ff7b72',Collection:'#56d4dd'};
  arr.forEach(n=>{
    const gEl=document.createElementNS(NS,'g');
    gEl.setAttribute('class','node');gEl.dataset.key=n.key;
    const c=document.createElementNS(NS,'circle');
    const r=sel.has(n.key)?17:(n.label==='Game'?13:9);
    c.setAttribute('cx',n.x);c.setAttribute('cy',n.y);c.setAttribute('r',r);
    c.setAttribute('fill',COL[n.label]||'#ccc');
    c.setAttribute('stroke',sel.has(n.key)?'#fff':'none');
    c.setAttribute('stroke-width',sel.has(n.key)?2:0);
    const t=document.createElementNS(NS,'text');
    t.setAttribute('x',n.x);t.setAttribute('y',n.y+r+13);
    t.setAttribute('class','nlabel');
    t.textContent=n.name.length>22?n.name.slice(0,21)+'…':n.name;
    gEl.appendChild(c);gEl.appendChild(t);svg.appendChild(gEl)});
}
// Open on the demo graph: run the default four-game query immediately.
document.getElementById('go').click();
</script></body></html>
"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
