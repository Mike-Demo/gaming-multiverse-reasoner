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
                 "GameMode": "game mode", "Engine": "engine"}
GENRE_HUB_CAP = int(os.environ.get("GENRE_HUB_CAP", "12000"))
# Lineage edges (Game -> Game): the child/version points at its parent.
LINEAGE_TYPES = ("PART_OF", "VERSION_OF", "DLC_OF", "EXPANSION_OF",
                 "REMAKE_OF", "REMASTER_OF")
LIN_PHRASE = {"PART_OF": "is a version of", "VERSION_OF": "is an edition of",
              "DLC_OF": "is DLC for", "EXPANSION_OF": "is an expansion of",
              "REMAKE_OF": "is a remake of", "REMASTER_OF": "is a remaster of"}

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
                  "Engine": "HAS_ENGINE",
                  "Company": "LINKED_COMPANY"}
ATTR_UNION = ("HAS_GENRE|HAS_THEME|HAS_MODE|HAS_PERSPECTIVE|ON_PLATFORM|"
              "IN_FRANCHISE|IN_COLLECTION")
COMP_UNION = "DEVELOPED_BY|PUBLISHED_BY|PORTED_BY|SUPPORTED_BY"
CY_NEIGHBOURS = (
    "MATCH (g:Game {igdb_id:$g})-[r]-(x) "
    "RETURN labels(x)[0], x.igdb_id, x.name, x.degree, "
    "coalesce(x.rating_count, 0), type(r) LIMIT 9999")
CY_SHARED_DISPLAY = (
    CY_NEIGHBOURS + "  -- run for both games; shared neighbours "
    "intersected in code (a single (a)-[r1]-(x)-[r2]-(b) pattern "
    "measured ~500 ms/pair on FalkorDB; anchored expansions are ~5 ms)")
CY_SIMILAR = ("MATCH (a:Game {igdb_id:$a}), (b:Game {igdb_id:$b}) "
              "WITH shortestPath((a)-[:SIMILAR_TO*..4]-(b)) AS p "
              "UNWIND nodes(p) AS n RETURN n.igdb_id, n.name")
LIN_UNION = "|".join(LINEAGE_TYPES)
CY_LINEAGE_DIRECT = (
    f"MATCH (a:Game {{igdb_id:$a}})-[r:{LIN_UNION}]-(b:Game {{igdb_id:$b}}) "
    "RETURN type(r), startNode(r).igdb_id")
CY_LINEAGE_COMMON = (
    f"MATCH (a:Game {{igdb_id:$a}})-[r1:{LIN_UNION}]-(p:Game)"
    f"-[r2:{LIN_UNION}]-(b:Game {{igdb_id:$b}}) "
    "WHERE p.igdb_id <> $a AND p.igdb_id <> $b "
    "RETURN p.igdb_id, p.name, type(r1), startNode(r1).igdb_id, "
    "type(r2), startNode(r2).igdb_id LIMIT 3")
COMP_TYPES = set(COMP_UNION.split("|"))
# NOTE: typed-union patterns ([r:TYPE1|TYPE2|...]) measure pathologically
# slow on this FalkorDB build (~140 ms for an 8-row anchored expansion),
# so hot paths use untyped expansions and filter type(r) in Python.
CY_GAME_COMPANIES = (
    "MATCH (g:Game {igdb_id:$g})-[r]->(c:Company) "
    "WHERE c.degree <= $cap "
    "RETURN c.igdb_id, c.name, c.degree, type(r)")
CY_COMPANY_GAMES = (
    "MATCH (c:Company {igdb_id:$c})<-[r]-(m:Game) "
    "WHERE m.igdb_id <> $a AND m.igdb_id <> $b "
    "RETURN m.igdb_id, m.name, coalesce(m.rating_count, 0), type(r) "
    "LIMIT 999")
CY_BRIDGE_GAMES = CY_COMPANY_GAMES  # display: bridge = intersect in code


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

    # --- lineage: direct version-family links + shared lineage nodes ---
    def lin_clause(subj, rtype, obj):
        return f"{subj} {LIN_PHRASE.get(rtype, 'is related to')} {obj}"

    try:
        direct = ro_query(fill(CY_LINEAGE_DIRECT, a=a_id, b=b_id))
        if direct:
            # One direct path per pair: keep the most specific relation
            # (a pair can be PART_OF and REMAKE_OF at once in IGDB data).
            spec_rank = {"REMAKE_OF": 0, "REMASTER_OF": 0, "DLC_OF": 1,
                         "EXPANSION_OF": 1, "VERSION_OF": 2, "PART_OF": 3}
            rtype, start_id = min(
                direct, key=lambda r: spec_rank.get(r[0], 9))
            subj, obj = ((a_name, b_name) if start_id == a_id
                         else (b_name, a_name))
            paths.append({
                "kind": "lineage", "nodes": [A, B],
                "edges": [edge(A, B, rtype)],
                "via": obj, "via_type": "Game", "degree": 0,
                "description": lin_clause(subj, rtype, obj) + ".",
                "cypher": CY_LINEAGE_DIRECT})
        seen_p = set()
        for pid_, pname, t1, s1, t2, s2 in ro_query(
                fill(CY_LINEAGE_COMMON, a=a_id, b=b_id)):
            if pid_ in seen_p:
                continue
            seen_p.add(pid_)
            P = node("Game", pid_, pname)
            c1 = (lin_clause(a_name, t1, pname) if s1 == a_id
                  else lin_clause(pname, t1, a_name))
            c2 = (lin_clause(b_name, t2, pname) if s2 == b_id
                  else lin_clause(pname, t2, b_name))
            paths.append({
                "kind": "lineage", "nodes": [A, P, B],
                "edges": [edge(A, P, t1), edge(P, B, t2)],
                "via": pname, "via_type": "Game", "degree": 1,
                "description": c1 + "; " + c2 + ".",
                "cypher": CY_LINEAGE_COMMON})
    except Exception as e:
        print("lineage query failed:", type(e).__name__, e, flush=True)

    # --- shared neighbours: attributes and common similar games ---
    # Two anchored single-hop expansions, intersected in code (see
    # CY_SHARED_DISPLAY for why the single pattern is too slow here).
    def neighbours(gid):
        out = {}
        for label, xid, name, degree, rc, t in ro_query(
                fill(CY_NEIGHBOURS, g=gid)):
            key = f"{label}:{xid}"
            if key not in out:
                out[key] = (label, xid, name, degree, rc, t)
        return out

    na, nb = neighbours(a_id), neighbours(b_id)
    shared_rows = []
    for key in na.keys() & nb.keys():
        label, xid, name, degree, rc, t1 = na[key]
        shared_rows.append(((label, xid, name, degree, rc, t1, nb[key][5]),
                            CY_SHARED_DISPLAY))
    attrs, games_x = [], []
    for (label, xid, name, degree, rc, t1, t2), qtext in shared_rows:
        if label == "Game":
            if t1 in LINEAGE_TYPES and t2 in LINEAGE_TYPES:
                continue  # the lineage paths above tell this story properly
            games_x.append((xid, name, rc, t1, t2, qtext))
        elif label in EXCLUDED_SHARED_LABELS:
            continue  # hub suppression: platforms / modes never route paths
        elif label in ("Genre", "Theme", "Engine") and (degree or 0) > GENRE_HUB_CAP:
            continue  # broad hub genre/theme/engine ("Adventure", "Fantasy")
        else:
            attrs.append((label, xid, name, degree or 0, t1, t2, qtext))
    attrs.sort(key=lambda r: r[3])
    seen_types = set()
    best_attr = None
    for label, xid, name, degree, t1, t2, qtext in attrs:
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
            "cypher": qtext})
    games_x.sort(key=lambda r: -r[2])
    for xid, name, rc, t1, t2, qtext in games_x[:2]:
        X = node("Game", xid, name)
        if t1 == "SIMILAR_TO" and t2 == "SIMILAR_TO":
            desc = (f"IGDB lists {name} as similar to both "
                    f"{a_name} and {b_name}.")
        else:
            desc = f"{name} links to both {a_name} and {b_name} in the graph."
        paths.append({
            "kind": "common-neighbor", "nodes": [A, X, B],
            "edges": [edge(A, X, t1), edge(X, B, t2)],
            "via": name, "via_type": "Game", "degree": None,
            "description": desc,
            "cypher": qtext})

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
    # Orchestrated: each game's capped companies first (anchored, ~ms),
    # then one anchored bridge query per company pair. The single 4-hop
    # chain pattern measured ~540 ms/pair on FalkorDB.
    def capped_companies(gid):
        out = {}
        for cid, cname, cdeg, t in ro_query(
                fill(CY_GAME_COMPANIES, g=gid, cap=DEGREE_CAP)):
            if t in COMP_TYPES and cid not in out:
                out[cid] = (cid, cname, cdeg, t)
        return out

    def company_games(cid):
        out = {}
        for mid, mname, mrc, t in ro_query(
                fill(CY_COMPANY_GAMES, c=cid, a=a_id, b=b_id)):
            if t in COMP_TYPES and mid not in out:
                out[mid] = (mname, mrc, t)
        return out

    ca_map, cb_map = capped_companies(a_id), capped_companies(b_id)
    games_by_company = {}
    comp_rows = []
    for xid, (_, xn, xd, t1) in ca_map.items():
        if xid not in games_by_company:
            games_by_company[xid] = company_games(xid)
        gx = games_by_company[xid]
        for yid, (_, yn, yd, t4) in cb_map.items():
            if yid not in games_by_company:
                games_by_company[yid] = company_games(yid)
            gy = games_by_company[yid]
            for mid in gx.keys() & gy.keys():
                mname, mrc, t2 = gx[mid]
                t3 = gy[mid][2]
                comp_rows.append((mid, mname, mrc, xid, xn, xd, t1, t2,
                                  yid, yn, yd, t3, t4))
    comp_rows.sort(key=lambda r: ((r[5] or 0) + (r[10] or 0),
                                  -(r[2] or 0)))
    seen_bridges = set()
    emitted = 0
    for (mid, mname, mrc, xid, xn, xd, t1, t2,
         yid, yn, yd, t3, t4) in comp_rows:
        if emitted >= 3:
            break  # global top-3, as the single-query version did
        bkey = (mid, xid, yid)
        if bkey in seen_bridges:
            continue  # same bridge via a different role combination
        seen_bridges.add(bkey)
        emitted += 1
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
            "description": desc, "cypher": CY_BRIDGE_GAMES})

    kind_rank = {"lineage": 0, "company-bridge": 1, "similar-chain": 2,
                 "common-neighbor": 2, "shared": 3}
    paths.sort(key=lambda p: (kind_rank.get(p["kind"], 9),
                              p["degree"] if p["degree"] is not None else 10**9))
    return paths


def game_info(gid):
    rows = ro_query(fill(
        "MATCH (g:Game {igdb_id:$i}) RETURN g.name, "
        "coalesce(g.rating_count, 0), g.community, g.community_size, "
        "g.summary, coalesce(g.total_rating, 0), g.first_release_date",
        i=gid))
    if not rows:
        return None
    name, rc, community, csize, summary, trate, frd = rows[0]
    year = time.gmtime(frd).tm_year if frd else None
    mates = []
    if community is not None:
        # Peers = same-community games directly linked to this one by a
        # similar-game edge (its local cluster), not just the community's
        # most-rated members — large LPA communities mix famous titles.
        raw = ro_query(fill(
            "MATCH (g:Game {igdb_id: $i})-[r:SIMILAR_TO]-(m:Game) "
            "WHERE m.community = $c "
            "RETURN m.igdb_id, m.name, coalesce(m.rating_count, 0), "
            "startNode(r).igdb_id", c=community, i=gid))
        by_m = {}
        for mid, mname, mrc, start in raw:
            e = by_m.setdefault(mid, {"name": mname, "rc": mrc,
                                      "dirs": set()})
            e["dirs"].add(1 if start == gid else 0)
        ranked = sorted(by_m.values(),
                        key=lambda e: (0 if len(e["dirs"]) > 1 else 1,
                                       -e["rc"]))
        mates = [e["name"] for e in ranked[:6]]
        if len(mates) < 3:
            mrows = ro_query(fill(
                "MATCH (m:Game {community: $c}) WHERE m.igdb_id <> $i "
                "RETURN m.name, coalesce(m.rating_count, 0) "
                "ORDER BY coalesce(m.rating_count, 0) DESC LIMIT 6",
                c=community, i=gid))
            mates = [r[0] for r in mrows]
    return {"igdb_id": gid, "name": name, "rating_count": rc,
            "community": community, "community_size": csize,
            "community_top": mates, "summary": summary or "",
            "total_rating": trate, "year": year}


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


def _picks_connected(picks):
    """True when pair_paths finds >= 1 path for every pair in the set.

    A Surprise-me set should never contain a dead pair (the first live
    draw paired The Last of Us Part II with Hearthstone: no shared
    attribute under the hub caps, no chain, no company bridge).
    """
    jobs = [(picks[i], picks[j]) for i in range(len(picks))
            for j in range(i + 1, len(picks))]

    def one(ab):
        try:
            return bool(pair_paths(ab[0][0], ab[0][1], ab[1][0], ab[1][1]))
        except Exception as e:
            print("random validation:", type(e).__name__, e, flush=True)
            return False

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        return all(ex.map(one, jobs))


def api_random():
    """Four different games for the Surprise-me button.

    Pool = the 600 most-rated games (well connected, so bridges exist);
    shuffled server-side, preferring four distinct detected communities
    so the paths between them are interesting rather than same-cluster.
    Candidate sets are validated with the real path queries so every
    pair in the returned set actually connects; up to 5 draws, then the
    last set is returned as-is.
    """
    import random
    rows = ro_query(
        "MATCH (g:Game) WHERE coalesce(g.rating_count, 0) >= 250 "
        "RETURN g.igdb_id, g.name, coalesce(g.rating_count, 0), g.community "
        "ORDER BY coalesce(g.rating_count, 0) DESC LIMIT 600")
    pool = [list(r) for r in rows]
    picks = []
    for _attempt in range(5):
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
        if len(picks) == 4 and _picks_connected(picks):
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


# CO2.js per-visit estimate assets (@tgwf/co2 v0.19.0, SWD v4), served
# locally so the estimate makes no external calls. See co2-footer.js.
def _read_asset(name):
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               name), "rb") as f:
            return f.read()
    except OSError:
        return b""


CO2_MIN_JS = _read_asset("co2.min.js")
CO2_FOOTER_JS = _read_asset("co2-footer.js")


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _text(self, body, ctype="text/plain; charset=utf-8", status=200):
        data = body.encode() if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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
            elif self.path.startswith("/docs"):
                self._text(LICENSES_PAGE, "text/html; charset=utf-8")
            elif self.path.startswith("/licenses"):
                self._text(LICENSES_PAGE, "text/html; charset=utf-8")
            elif self.path.startswith("/llms.txt"):
                self._text(LLMS_TXT)
            elif self.path.startswith("/carbon.txt"):
                self._text(CARBON_TXT)
            elif self.path.startswith("/robots.txt"):
                self._text(ROBOTS_TXT)
            elif self.path.startswith("/sitemap.xml"):
                self._text(SITEMAP_XML, "application/xml; charset=utf-8")
            elif self.path.startswith("/.well-known/agent.json"):
                self._json(AGENT_CARD)
            elif self.path.startswith("/openapi.json"):
                self._json(OPENAPI)
            elif self.path.startswith("/co2.min.js"):
                self._text(CO2_MIN_JS, "application/javascript; charset=utf-8")
            elif self.path.startswith("/co2-footer.js"):
                self._text(CO2_FOOTER_JS,
                           "application/javascript; charset=utf-8")
            elif self.path.startswith("/mcp"):
                self._json({"name": "gaming-multiverse-reasoner",
                            "version": "1.0.0",
                            "transport": "MCP streamable HTTP (JSON responses)",
                            "endpoint": "POST /mcp",
                            "tools": [t["name"] for t in MCP_TOOLS],
                            "docs": "/llms.txt"})
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
            elif self.path.startswith("/mcp"):
                n = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(n) or b"{}"
                try:
                    msg = json.loads(raw)
                except Exception:
                    self._json({"jsonrpc": "2.0", "id": None, "error":
                                {"code": -32700, "message": "parse error"}}, 400)
                    return
                if isinstance(msg, list):
                    rs = [r for r in (mcp_handle(m) for m in msg) if r]
                    if rs:
                        self._json(rs)
                    else:
                        self.send_response(202); self.end_headers()
                else:
                    resp = mcp_handle(msg)
                    if resp is None:
                        self.send_response(202); self.end_headers()
                    else:
                        self._json(resp)
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)


PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Gaming Multiverse Reasoner</title>
<meta name="description" content="Pick any set of games and see the hidden paths, bridge games and communities connecting them — computed live in FalkorDB over a frozen snapshot of 50,217 IGDB games, narrated by IBM watsonx.">
<meta name="author" content="Mike “Demo” Demopoulos">
<meta name="theme-color" content="#0d1117">
<meta property="og:title" content="Gaming Multiverse Reasoner">
<meta property="og:description" content="Hidden paths between any games, computed live in FalkorDB and narrated by IBM watsonx.">
<meta property="og:type" content="website">
<meta name="twitter:card" content="summary">
<meta name="twitter:title" content="Gaming Multiverse Reasoner">
<meta name="twitter:description" content="Hidden paths between any games, computed live in FalkorDB and narrated by IBM watsonx.">
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
{"@type":"WebApplication","name":"Gaming Multiverse Reasoner",
"applicationCategory":"EntertainmentApplication","operatingSystem":"Web",
"description":"Finds hidden paths, bridge games and communities between any set of games, computed live in FalkorDB from a frozen IGDB snapshot of 50,217 games.",
"author":{"@type":"Person","name":"Mike Demopoulos","url":"https://mikedemo.dev",
"sameAs":["https://github.com/Mike-Demo","https://x.com/Mike_Demo",
"https://facebook.com/mikedemo42","https://bsky.app/profile/mikedemo.bsky.social"]},
"offers":{"@type":"Offer","price":"0","priceCurrency":"USD"}}]}
</script>
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
padding:11px 18px;min-height:44px;font-weight:700;cursor:pointer;font-size:15px}
button.secondary{background:#26314a;color:var(--txt)}
button:disabled{opacity:.5}
#go{margin-top:12px}
.share{margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.shbtn{background:#26314a;color:var(--txt);border:0;border-radius:8px;
padding:10px 14px;min-height:44px;font-weight:600;cursor:pointer;font-size:14px;
text-decoration:none;display:inline-flex;align-items:center}
svg{width:100%;height:560px;background:#0a0e14;border-radius:10px}
.edge{stroke:#3d4a5f;stroke-width:1.4;stroke-dasharray:3 6;
animation:flow 1.4s linear infinite}
@keyframes flow{to{stroke-dashoffset:-9}}
.edge.hot{stroke:var(--acc);stroke-width:3;stroke-dasharray:9 5;
animation-duration:.45s;filter:drop-shadow(0 0 4px var(--acc))}
.edge.dim{opacity:.12}.node.dim{opacity:.15}
.node{cursor:pointer;transform-box:fill-box;transform-origin:center;
animation:pop .45s cubic-bezier(.2,1.6,.4,1) both}
@keyframes pop{0%{transform:scale(.2);opacity:0}70%{transform:scale(1.12)}
100%{transform:scale(1);opacity:1}}
.node .hit{fill:#fff;fill-opacity:0}
.node .pulse{fill:none;stroke:var(--acc);stroke-width:2;pointer-events:none;
transform-box:fill-box;transform-origin:center;animation:ring 1.8s ease-out infinite}
@keyframes ring{0%{transform:scale(1);opacity:.75}100%{transform:scale(2.1);opacity:0}}
.slot input{transition:border-color .25s,box-shadow .25s}
.slot input.spinning{border-color:var(--warn);color:var(--warn)}
.slot input.landed{border-color:var(--acc);box-shadow:0 0 0 3px rgba(126,231,135,.35)}
@media (max-width:720px){
main{padding:10px 10px 22px;gap:10px}
header{padding:16px 14px 2px}h1{font-size:22px}
#left{min-width:0;flex:1 1 100%}#right{min-width:0;flex:1 1 100%;
max-height:none;overflow:visible}
svg{height:490px}button{padding:13px 16px}.panel{padding:13px}
.nlabel{font-size:14px}#status{display:block;margin-top:8px}
#go,#rand{width:100%;display:block}#rand{margin-top:10px}
.share .small{flex:1 1 100%}
.share .shbtn{flex:1 1 40%;justify-content:center;text-align:center}}
.nlabel{fill:var(--txt);font-size:11px;text-anchor:middle;pointer-events:none}
.pairtitle{font-weight:700;margin:10px 0 6px}
.path{border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin:7px 0;
cursor:pointer;background:#10151d}.path:hover{border-color:var(--acc2)}
.path.active{border-color:var(--acc);background:#12241a}
.kind{font-size:11px;color:var(--acc2);text-transform:uppercase;letter-spacing:.06em}
.path details{margin-top:5px}.path summary{min-height:24px;padding:5px 0;cursor:pointer}.path code{font-size:11px;color:var(--dim);
white-space:pre-wrap;word-break:break-word}
.claim{border-left:3px solid var(--acc2);padding:6px 10px;margin:8px 0;
background:#10151d;cursor:pointer}.claim:hover{border-color:var(--acc)}
.badge{font-size:11px;background:#26314a;border-radius:20px;padding:2px 8px;color:var(--acc2)}
.comm{font-size:13px;color:var(--dim)}.comm b{color:var(--txt)}
.err{color:#ff7b72}.small{font-size:12px;color:var(--dim)}
footer{border-top:1px solid var(--line);margin-top:8px;padding:18px 24px 26px;
color:var(--dim);font-size:13px;text-align:center}
footer a{color:var(--acc2)}footer p{margin:4px 0}
:focus-visible{outline:3px solid var(--acc2);outline-offset:2px}
.skip{position:absolute;left:-999px;top:0;background:var(--acc);color:#06280f;
padding:10px 16px;font-weight:700;z-index:20;border-radius:0 0 8px 0}
.skip:focus{left:0}
.node:focus{outline:none}.node:focus .hit{stroke:var(--acc2);stroke-width:2;fill-opacity:.08}
@media (prefers-reduced-motion: reduce){
.edge{animation:none}.node{animation:none}.node .pulse{animation:none}
.slot input{transition:none}}
</style></head><body>
<a class="skip" href="#paths">Skip to results</a>
<header><h1>🕹️ Gaming Multiverse Reasoner</h1>
<p class="sub">Pick any set of games and see the <b>hidden paths</b>, <b>bridge
games</b> and <b>communities</b> that connect them. Every path is computed live
in <b>FalkorDB</b> from a frozen IGDB snapshot (50,217 games, 14 Oct 2026).
Paths include direct lineage (remakes, remasters, DLC, expansions), shared
game engines, company bridges and similar-game chains — with hub
suppression: platforms and game modes never appear in paths, broad genres,
themes and engines (connecting 12,000+ games, like “Adventure”) are suppressed, and
company bridges only route through companies connecting 800 games or fewer — so “PC”
and “Single player” can never win. Narration by <b>watsonx.ai</b> may only
cite the graph’s own path IDs — citations are validated in code.</p></header>
<main>
<div id="left">
 <div class="panel"><div class="slots" id="slots"></div>
 <button id="go">Find the hidden paths</button>
 <button class="secondary" id="rand">🎲 Surprise me — 4 random games</button>
 <span class="small" id="status" role="status" aria-live="polite"></span>
 <div class="share" id="share" style="display:none">
 <span class="small">Share these connections:</span>
 <a class="shbtn" id="sh-x" target="_blank" rel="noopener">Post to X</a>
 <a class="shbtn" id="sh-threads" target="_blank" rel="noopener">Threads</a>
 <a class="shbtn" id="sh-bsky" target="_blank" rel="noopener">Bluesky</a>
 <button class="shbtn" id="sh-copy" type="button">Copy link</button></div></div>
 <div class="panel"><svg id="graph" viewBox="0 0 900 560" role="img" aria-label="Graph of the paths connecting the selected games. Tab to a node and press Enter to highlight its paths."></svg>
 <div class="small">Tap or click any node, path or claim to highlight its connections.</div></div>
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
<footer>
<p>Built by <a href="https://mikedemo.dev">Mike “Demo” Demopoulos</a> for the
WeMakeDevs × FalkorDB Graph Hacks hackathon.</p>
<p><a href="https://github.com/Mike-Demo">GitHub</a> ·
<a href="https://x.com/Mike_Demo">X</a> ·
<a href="https://facebook.com/mikedemo42">Facebook</a> ·
<a href="https://bsky.app/profile/mikedemo.bsky.social">Bluesky</a></p>
<p><a href="https://github.com/Mike-Demo/gaming-multiverse-reasoner">Source on GitHub</a> ·
<a href="/licenses">Licenses &amp; tech stack</a> ·
<a href="/llms.txt">llms.txt</a> · <a href="/carbon.txt">carbon.txt</a> · MCP server: <code>POST /mcp</code> (tools listed in llms.txt)</p>
<p>MIT licensed. Game data from IGDB (Twitch), snapshot frozen 14 Oct 2026 ·
Communities are precomputed (weighted label propagation) and labelled as such.</p>
</footer>
<script>
const DEMO=[["Elden Ring",119133],["Hades",113112],["Stardew Valley",17000],
["Baldur’s Gate III",119171]];
const slotsEl=document.getElementById('slots');
let selected=DEMO.map(([name,id])=>({name,igdb_id:id}));
DEMO.forEach(([name,id],i)=>{
  const d=document.createElement('div');d.className='slot';
  d.innerHTML=`<label for="in${i}">Game ${i+1}</label><div class="results">
  <input id="in${i}" value="${name}" autocomplete="off" role="combobox"
   aria-autocomplete="list" aria-expanded="false" aria-controls="dd${i}">
  <div class="dropdown" id="dd${i}" role="listbox" style="display:none"></div></div>`;
  slotsEl.appendChild(d);
  const inp=d.querySelector('input'),dd=d.querySelector('.dropdown');
  let timer,items=[],hi=-1;
  const closeDD=()=>{dd.style.display='none';hi=-1;
    inp.setAttribute('aria-expanded','false');
    inp.removeAttribute('aria-activedescendant')};
  const paint=()=>{[...dd.children].forEach((c,k)=>{
    c.classList.toggle('sel',k===hi);
    c.setAttribute('aria-selected',k===hi?'true':'false')});
    if(hi>=0&&dd.children[hi])inp.setAttribute('aria-activedescendant',dd.children[hi].id);
    else inp.removeAttribute('aria-activedescendant')};
  const pick=g=>{if(!g)return;selected[i]={name:g.name,igdb_id:g.igdb_id};
    inp.value=g.name;closeDD()};
  inp.addEventListener('input',()=>{clearTimeout(timer);
    timer=setTimeout(async()=>{
      const q=inp.value.trim();if(q.length<2){closeDD();return}
      const r=await fetch('/api/resolve?q='+encodeURIComponent(q));
      const j=await r.json();items=j.results||[];hi=items.length?0:-1;
      dd.innerHTML='';
      if(!items.length){const o=document.createElement('div');
        o.setAttribute('role','option');o.setAttribute('aria-disabled','true');
        o.innerHTML='<small>No matches — try another title</small>';
        dd.appendChild(o)}
      items.forEach((g,k)=>{
        const o=document.createElement('div');
        o.setAttribute('role','option');o.id='dd'+i+'o'+k;
        o.setAttribute('aria-selected','false');
        o.innerHTML=`${g.name} <small>${g.year||''} · ★${g.rating_count}</small>`;
        o.onmouseenter=()=>{hi=k;paint()};
        o.onclick=()=>pick(g);
        dd.appendChild(o)});
      paint();inp.setAttribute('aria-expanded','true');
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
const KIND_LABEL={lineage:'direct lineage',shared:'shared thread',
'common-neighbor':'common neighbour','similar-chain':'similar-game chain',
'company-bridge':'company bridge'};
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
    updateShare();
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
      d.tabIndex=0;d.setAttribute('role','button');
      d.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){
        e.preventDefault();setActive([p.path_id],d)}};
      el.appendChild(d)});
  });
}
function renderComms(){
  const el=document.getElementById('comms');el.innerHTML='';
  DATA.games.forEach(g=>{
    const d=document.createElement('div');d.className='comm';
    const meta=[g.year,g.total_rating?('★ '+Math.round(g.total_rating)):''].filter(Boolean).join(' · ');
    const esc=s=>s.replace(/&/g,'&amp;').replace(/</g,'&lt;');
    const sum=g.summary?`<div class="small">${esc(g.summary.slice(0,180))}${g.summary.length>180?'…':''}</div>`:'';
    d.innerHTML=(g.community==null?`<b>${g.name}</b> — no detected community`
      :`<b>${g.name}</b> — community of ${g.community_size} games
        (label propagation over similar-game links). Peers: ${g.community_top.join(', ')}`)
      +(meta?` <span class="small">${meta}</span>`:'')+sum;
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
      d.tabIndex=0;d.setAttribute('role','button');
      d.setAttribute('aria-label','Highlight paths '+c.path_ids.join(', '));
      d.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){
        e.preventDefault();setActive(c.path_ids,null)}};
      box.appendChild(d)});
    if(j.note){const n=document.createElement('p');n.className='small';
      n.textContent=j.note;box.appendChild(n)}
  }catch(e){box.innerHTML='<p class="err">'+e.message+'</p>'}
  btn.disabled=false;
};
const FILLER=['Minecraft','Tetris','Portal 2','Celeste','Undertale','Doom',
'Pac-Man','Chrono Trigger','Half-Life 2','Dark Souls','Persona 5','Journey',
'Cuphead','Hollow Knight','Stardew Valley','The Witcher 3: Wild Hunt',
'Super Mario Odyssey','Final Fantasy VII','Inside','Tetris Effect'];
document.getElementById('rand').onclick=async()=>{
  const btn=document.getElementById('rand');btn.disabled=true;
  const goBtn=document.getElementById('go');goBtn.disabled=true;
  const st=document.getElementById('status');
  st.textContent=' 🎰 rolling the multiverse…';
  const inputs=[0,1,2,3].map(i=>document.getElementById('in'+i));
  inputs.forEach(inp=>{inp.classList.add('spinning');
    inp.classList.remove('landed')});
  const REDUCED=window.matchMedia&&
    matchMedia('(prefers-reduced-motion: reduce)').matches;
  const spin=REDUCED?null:setInterval(()=>{inputs.forEach(inp=>{
    if(inp.classList.contains('spinning'))
      inp.value=FILLER[Math.floor(Math.random()*FILLER.length)]})},70);
  const stopSpin=()=>{if(spin)clearInterval(spin);
    inputs.forEach(inp=>inp.classList.remove('spinning'))};
  try{
    const r=await fetch('/api/random');const j=await r.json();
    if(j.error)throw new Error(j.error);
    const games=j.games||[];
    await Promise.all(games.map((g,i)=>new Promise(res=>{
      setTimeout(()=>{selected[i]={name:g.name,igdb_id:g.igdb_id};
        const inp=inputs[i];inp.value=g.name;
        inp.classList.remove('spinning');inp.classList.add('landed');
        setTimeout(()=>inp.classList.remove('landed'),800);res()},
        450+i*380)})));
    stopSpin();
    goBtn.disabled=false;
    if(games.length>=2)document.getElementById('go').click();
  }catch(e){stopSpin();
    inputs.forEach((inp,i)=>{if(selected[i])inp.value=selected[i].name});
    st.innerHTML=' <span class="err">'+e.message+'</span>';
    goBtn.disabled=false}
  btn.disabled=false;
};
// --- tiny force layout + SVG render ---
function renderGraph(){
  const svg=document.getElementById('graph');svg.innerHTML='';
  // Portrait layout space on narrow screens: the landscape 900x560 space
  // letterboxes into a tall phone viewport and shrinks labels to ~5px.
  const portrait=window.matchMedia('(max-width:720px)').matches;
  const W=portrait?540:900,H=portrait?740:560,CX=W/2,CY=H/2;
  const MX=portrait?66:70,MY=portrait?40:45;
  svg.setAttribute('viewBox','0 0 '+W+' '+H);
  const nodes=new Map(),edges=[];
  DATA.pairs.forEach(pr=>pr.paths.forEach(p=>{
    p.nodes.forEach(n=>{if(!nodes.has(n.key))nodes.set(n.key,
      {...n,x:CX+(Math.random()-.5)*(W*.55),y:CY+(Math.random()-.5)*(H*.6),vx:0,vy:0})});
    p.edges.forEach(e=>edges.push({...e,pid:p.path_id}))}));
  const sel=new Set(DATA.games.map(g=>'Game:'+g.igdb_id));
  const nodePids={};
  edges.forEach(e=>{if(e.pid){
    (nodePids[e.a]=nodePids[e.a]||new Set()).add(e.pid);
    (nodePids[e.b]=nodePids[e.b]||new Set()).add(e.pid)}});
  Object.keys(nodePids).forEach(k=>nodePids[k]=[...nodePids[k]]);
  const arr=[...nodes.values()];
  // Deterministic ellipse start + capped forces: uncapped repulsion in the
  // first version flung every node into the corners and pinned them there.
  arr.forEach((n,i)=>{const a=2*Math.PI*i/Math.max(arr.length,1);
    n.x=CX+(portrait?150:210)*Math.cos(a);n.y=CY+(portrait?290:150)*Math.sin(a);n.vx=0;n.vy=0});
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
    arr.forEach(n=>{n.vx+=(CX-n.x)*0.006;n.vy+=(CY-n.y)*0.006;
      const sp=Math.hypot(n.vx,n.vy);
      if(sp>6){n.vx*=6/sp;n.vy*=6/sp}
      n.x+=n.vx;n.y+=n.vy;n.vx*=0.78;n.vy*=0.78;
      n.x=Math.max(MX,Math.min(W-MX,n.x));n.y=Math.max(MY,Math.min(H-MY,n.y))});
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
  Franchise:'#ff7b72',Collection:'#56d4dd',Engine:'#db61a2'};
  arr.forEach((n,idx)=>{
    const gEl=document.createElementNS(NS,'g');
    gEl.setAttribute('class','node');gEl.dataset.key=n.key;
    gEl.setAttribute('tabindex','0');gEl.setAttribute('role','button');
    gEl.setAttribute('aria-label',
      (n.name.length>22?n.name.slice(0,21)+'…':n.name)+' — highlight paths');
    gEl.style.animationDelay=(idx*35)+'ms';
    const r=sel.has(n.key)?17:(n.label==='Game'?13:9);
    if(sel.has(n.key)){
      const p=document.createElementNS(NS,'circle');
      p.setAttribute('cx',n.x);p.setAttribute('cy',n.y);p.setAttribute('r',r);
      p.setAttribute('class','pulse');gEl.appendChild(p)}
    const c=document.createElementNS(NS,'circle');
    c.setAttribute('cx',n.x);c.setAttribute('cy',n.y);c.setAttribute('r',r);
    c.setAttribute('fill',COL[n.label]||'#ccc');
    c.setAttribute('stroke',sel.has(n.key)?'#fff':'none');
    c.setAttribute('stroke-width',sel.has(n.key)?2:0);
    const t=document.createElementNS(NS,'text');
    t.setAttribute('x',n.x);t.setAttribute('y',n.y+r+13);
    t.setAttribute('class','nlabel');
    t.textContent=n.name.length>22?n.name.slice(0,21)+'…':n.name;
    const hit=document.createElementNS(NS,'circle');
    hit.setAttribute('cx',n.x);hit.setAttribute('cy',n.y);
    hit.setAttribute('r',Math.max(r+7,portrait?26:20));hit.setAttribute('class','hit');
    gEl.appendChild(c);gEl.appendChild(t);gEl.appendChild(hit);
    gEl.onclick=()=>{const pids=nodePids[n.key]||[];
      if(pids.length)setActive(pids,null)};
    gEl.onkeydown=e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();
      const pids=nodePids[n.key]||[];if(pids.length)setActive(pids,null)}};
    svg.appendChild(gEl)});
}
// Experimental WebMCP: expose the same tools to AI agents in supporting
// browsers. The hosted MCP server at POST /mcp is the canonical interface.
if(navigator.modelContext&&navigator.modelContext.registerTool){
  const wmcp=(name,description,inputSchema,fn)=>{
    try{
      navigator.modelContext.registerTool({
        name:name, description:description, inputSchema:inputSchema,
        execute:async(args)=>{
          const r=await fn(args||{});
          return {content:[{type:'text', text:JSON.stringify(r)}]};
        }
      });
    }catch(e){}
  };
  wmcp('resolve_game','Find IGDB games matching a title fragment.',
    {type:'object',properties:{query:{type:'string'}},required:['query']},
    async a=>{const r=await fetch('/api/resolve?q='+encodeURIComponent(a.query));
      return await r.json()});
  wmcp('find_paths','Find the hidden paths connecting 2-4 games by IGDB id.',
    {type:'object',properties:{ids:{type:'array',items:{type:'number'},
      minItems:2,maxItems:4}},required:['ids']},
    async a=>{const r=await fetch('/api/bridges?ids='+a.ids.join(','));
      return await r.json()});
  wmcp('random_games','Draw 4 random games whose pairs all connect.',
    {type:'object',properties:{}},
    async()=>{const r=await fetch('/api/random');return await r.json()});
}
// Shareable links: the current set lives in ?games=id,id,... — post it to
// X / Threads / Bluesky or copy the URL. Opening such a link runs that set.
function shareUrl(){const ids=selected.filter(Boolean).map(s=>s.igdb_id);
  return location.origin+'/?games='+ids.join(',')}
function updateShare(){if(!DATA)return;
  const ids=selected.filter(Boolean).map(s=>s.igdb_id);
  if(ids.length<2)return;
  const names=DATA.games.map(g=>g.name);
  const total=DATA.pairs.reduce((n,p)=>n+p.paths.length,0);
  const text=names.join(' \u2194 ')+' \u2014 '+total+
    ' hidden paths connect them in the Gaming Multiverse Reasoner \uD83D\uDD79\uFE0F';
  const url=shareUrl();
  document.getElementById('sh-x').href=
    'https://twitter.com/intent/tweet?text='+encodeURIComponent(text)+
    '&url='+encodeURIComponent(url);
  document.getElementById('sh-threads').href=
    'https://www.threads.net/intent/post?text='+
    encodeURIComponent(text+' '+url);
  document.getElementById('sh-bsky').href=
    'https://bsky.social/intent/compose?text='+
    encodeURIComponent(text+' '+url);
  document.getElementById('share').style.display='flex';
  history.replaceState(null,'','?games='+ids.join(','))}
document.getElementById('sh-copy').onclick=async()=>{
  const st=document.getElementById('status');
  try{await navigator.clipboard.writeText(shareUrl());
    st.textContent=' link copied \u2014 share it anywhere'}
  catch(e){window.prompt('Copy your share link:',shareUrl())}};
// Open on the demo graph, or on the shared set when the URL carries one.
(async()=>{const m=new URLSearchParams(location.search).get('games');
  if(m){const ids=m.split(',').map(Number).filter(n=>n>0).slice(0,4);
    if(ids.length>=2){try{
      const r=await fetch('/api/bridges?ids='+ids.join(','));
      const d=await r.json();
      if(!d.error&&d.games&&d.games.length>=2){DATA=d;
        d.games.forEach((g,i)=>{selected[i]={name:g.name,igdb_id:g.igdb_id};
          const inp=document.getElementById('in'+i);if(inp)inp.value=g.name});
        for(let i=d.games.length;i<4;i++){selected[i]=null;
          const inp=document.getElementById('in'+i);if(inp)inp.value=''}
        document.getElementById('status').textContent=
          ` done in ${d.stats.took_ms} ms \u00B7 ${d.pairs.reduce((n,p)=>n+p.paths.length,0)} paths`;
        renderGraph();renderPaths();renderComms();
        document.getElementById('narrate').disabled=false;
        updateShare();return}
    }catch(e){}}}
  document.getElementById('go').click()})();
</script>
<script src="/co2.min.js" defer></script>
<script src="/co2-footer.js" defer></script>
</body></html>
"""



# ---------------------------------------------------------------------------
# Machine-readable surfaces: licenses page, llms.txt, robots, sitemap,
# A2A agent card, OpenAPI, and an MCP server (streamable HTTP, JSON replies).
# Added 2026-10-14 at Demo's request (footer + licenses + llms.txt + MCP,
# Silicon Friendly surfaces, accessibility pass shipped in the same build).
# ---------------------------------------------------------------------------

PUBLIC_URL = "https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud"

LICENSES_PAGE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Licenses &amp; tech stack — Gaming Multiverse Reasoner</title>
<meta name="description" content="Every open-source library, technology and hosted service the Gaming Multiverse Reasoner uses, top to bottom.">
<style>
body{margin:0;background:#0d1117;color:#e6edf3;
font:15px/1.6 -apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:860px;margin:0 auto;padding:28px 20px 10px}
h1{font-size:24px}h2{font-size:18px;margin-top:30px;color:#7ee787}
a{color:#79c0ff}table{border-collapse:collapse;width:100%;margin:10px 0}
td,th{border:1px solid #30363d;padding:7px 10px;text-align:left;vertical-align:top}
th{background:#161b22}.dim{color:#8b949e}
footer{border-top:1px solid #30363d;margin-top:26px;padding:18px 24px 26px;
color:#8b949e;font-size:13px;text-align:center}footer p{margin:4px 0}
:focus-visible{outline:3px solid #79c0ff;outline-offset:2px}
</style></head><body>
<main>
<h1>Licenses &amp; tech stack</h1>
<p>The Gaming Multiverse Reasoner, top to bottom. Two honesty notes first:
the application and its data pipeline use <b>zero third-party packages</b> —
everything is the Python standard library — and the browser UI is hand-written
HTML/CSS/JavaScript with an inline SVG force layout. No frameworks, no CDN
assets, no trackers.</p>

<h2>This project's code</h2>
<p><b>MIT License</b> — Copyright (c) 2026 Mike Demopoulos.
Source: <a href="https://github.com/Mike-Demo/gaming-multiverse-reasoner">github.com/Mike-Demo/gaming-multiverse-reasoner</a>.
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files, to deal in the software
without restriction, including without limitation the rights to use, copy,
modify, merge, publish, distribute, sublicense, and/or sell copies, subject to
including the copyright notice and this permission notice in all copies.</p>

<h2>Open-source components</h2>
<table>
<tr><th>Component</th><th>Role</th><th>License</th></tr>
<tr><td>FalkorDB</td><td>Graph database holding the 50,217-game multiverse graph; all path queries are Cypher run live against it</td><td>SSPL-1.0</td></tr>
<tr><td>Redis</td><td>Storage engine FalkorDB is built on (RESP protocol, ACL reader user)</td><td>Tri-license: RSALv2 / SSPLv1 / AGPLv3</td></tr>
<tr><td>Python 3.12</td><td>Application runtime — standard library only (http.server), including the MCP server and the RESP client</td><td>PSF License 2.0</td></tr>
<tr><td>IBM Granite 4 H Small</td><td>The narration model served by watsonx.ai (ibm/granite-4-h-small)</td><td>Apache-2.0 (model)</td></tr>
<tr><td>Docker Engine</td><td>Runs FalkorDB on the VPC virtual server and builds the app image</td><td>Apache-2.0</td></tr>
<tr><td>Ubuntu 24.04 LTS</td><td>Operating system of the FalkorDB virtual server</td><td>Various free/open-source licenses</td></tr>
<tr><td>Debian (python:3.12-slim)</td><td>Base image of the application container</td><td>DFSG-free licenses</td></tr>
<tr><td>openCypher</td><td>The graph query language every path query is written in</td><td>Apache-2.0 (open specification)</td></tr>
<tr><td>Model Context Protocol</td><td>Agent interface exposed at POST /mcp</td><td>MIT (open specification)</td></tr>
<tr><td>JSON-RPC 2.0</td><td>Wire format for the MCP server</td><td>Open specification</td></tr>
</table>

<h2>Hosted services (not open source — stated plainly)</h2>
<table>
<tr><th>Service</th><th>Role</th></tr>
<tr><td>IBM Code Engine</td><td>Hosts this web app (min-scale 1, always warm)</td></tr>
<tr><td>IBM VPC (bx2-4x16 VSI, us-south)</td><td>Hosts the FalkorDB container</td></tr>
<tr><td>IBM Cloud Object Storage</td><td>Frozen data snapshots (igdb-raw-2026-10-14-v2.tar.gz + manifest)</td></tr>
<tr><td>IBM Container Registry</td><td>Application container images</td></tr>
<tr><td>IBM watsonx.ai</td><td>Serves the Granite model; narration runs in a deployment space</td></tr>
<tr><td>IGDB API (Twitch)</td><td>Source of all game data — see attribution below</td></tr>
<tr><td>GitHub</td><td>Source hosting for the project repository</td></tr>
</table>

<h2>Data &amp; attribution</h2>
<p>Game data comes from the <b>IGDB API</b> (Twitch Entertainment, Inc.) —
titles, ratings, companies, genres, themes, engines, franchises, collections
and similarity links for 50,217 games, pulled 14 Oct 2026 and frozen in IBM
Cloud Object Storage. The <b>QueerCade</b> catalogue (a project by the same
author) is merged in so all 753 of its games are present. Communities are
computed by this project's own weighted label-propagation implementation
(communities.py, MIT as part of this project) — no third-party graph
libraries. Precomputed and inferred material is labelled as such in the UI;
every path shown is computed live in FalkorDB at request time.</p>
<p class="dim">Built for the WeMakeDevs × FalkorDB “Graph Hacks” hackathon,
14–18 Oct 2026.</p>
</main>
<footer>
<p>Built by <a href="https://mikedemo.dev">Mike “Demo” Demopoulos</a> ·
<a href="/">Back to the Multiverse Reasoner</a></p>
<p><a href="https://github.com/Mike-Demo">GitHub</a> ·
<a href="https://x.com/Mike_Demo">X</a> ·
<a href="https://facebook.com/mikedemo42">Facebook</a> ·
<a href="https://bsky.app/profile/mikedemo.bsky.social">Bluesky</a> ·
<a href="https://github.com/Mike-Demo/gaming-multiverse-reasoner">Source</a> ·
<a href="/llms.txt">llms.txt</a> · <a href="/carbon.txt">carbon.txt</a></p>
<p>MIT licensed. Game data from IGDB (Twitch), snapshot frozen 14 Oct 2026.</p>
</footer>
<script src="/co2.min.js" defer></script>
<script src="/co2-footer.js" defer></script>
</body></html>
"""

LLMS_TXT = """# Gaming Multiverse Reasoner

> Pick any set of games and see the hidden paths, bridge games and communities
> that connect them. Every path is computed live in FalkorDB from a frozen
> IGDB snapshot; IBM watsonx (Granite) narrates the results, and every claim it
> makes is validated in code against real path ids.

Live app: https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud
Source: https://github.com/Mike-Demo/gaming-multiverse-reasoner
Licenses and full tech stack: https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/licenses

## Data

- 50,217 games from the IGDB API (Twitch), pulled and frozen 14 Oct 2026,
  including the full QueerCade catalogue (753 games).
- Graph "multiverse" in FalkorDB: 84,296 nodes / 1,023,292 relationships.
  Node labels: Game, Company, Genre, Theme, GameMode, Platform,
  PlayerPerspective, Franchise, Collection, Engine.
- Snapshot frozen in IBM Cloud Object Storage as
  snapshot/igdb-raw-2026-10-14-v2.tar.gz with snapshot/manifest-v2.json.
- Communities are PRECOMPUTED (weighted label propagation over similar-game
  links, 6,234 communities) and labelled as such wherever they appear.

## Method (honesty rules)

- Paths are computed live at request time with Cypher; each path response
  includes the Cypher used.
- Hub suppression: Platform and GameMode nodes never appear in paths; Genre,
  Theme and Engine nodes shared by more than 12,000 games are suppressed;
  company bridges cap company degree at 800.
- Path kinds: lineage (remake/remaster/DLC/expansion/version/part-of),
  company-bridge, similar-chain (shortest path over SIMILAR_TO),
  common-neighbor, shared (shared attribute neighbour).
- Narration claims must cite path_ids from the computed result; claims are
  validated in code and dropped if they cite anything else. If watsonx is
  unavailable, template claims are served and labelled source: "template".

## REST API (JSON, no auth, read-only)

- GET /api/health — graph name, game count, connected user.
- GET /api/resolve?q=<title fragment> — matching games (igdb_id, name, year,
  rating_count).
- GET /api/bridges?ids=<id,id[,id[,id]]> — 2 to 4 IGDB ids; returns per-pair
  paths with path_id, kind, templated description and the Cypher used, plus
  per-game community info and stats.took_ms.
- GET /api/random — 4 random games, validated so every pair connects.
- POST /api/narrate — body: a /api/bridges response; returns claims citing
  path_ids, with source "watsonx" or "template".
- GET /openapi.json — OpenAPI 3 description of this API.
- Errors are JSON: {"error": "..."} with an HTTP status code.

## MCP server (for AI agents)

- Endpoint: POST https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/mcp
- Transport: Model Context Protocol over streamable HTTP, JSON responses.
  JSON-RPC 2.0. Send initialize, then tools/list / tools/call.
- Tools:
  - resolve_game {query: string} — find games by title fragment.
  - find_paths {games: [name-or-id, ...]} — 2-4 games by name or IGDB id;
    names are resolved to their top match; returns the full paths payload.
  - random_games {} — a validated random set of 4 connected games.
  - game_community {igdb_id: number} — one game's community, peers, summary.
  - narrate {ids: [number, ...]} — paths plus watsonx narration claims.
- GET /mcp returns server info as JSON. GET /.well-known/agent.json is an
  A2A-style agent card describing the same capabilities.

## Limits

- Read-only demo: no authentication, no writes, no state changes.
- Graph queries are capped by a 5,000 ms FalkorDB timeout; the demo
  four-game query typically answers in under 2 seconds.
- Please be gentle: this is a hackathon demo on shared infrastructure.
"""

ROBOTS_TXT = """User-agent: *
Allow: /

Sitemap: https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/sitemap.xml
"""

CARBON_TXT = """version = "0.6"
last_updated = 2026-10-15

[[org.disclosures]]
doc_type = "web-page"
url = "https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/"
title = "Per-visit carbon estimate for this site"
description = "Every page footer shows a per-visit CO2e estimate computed locally in the browser with CO2.js (The Green Web Foundation, SWD model v4) from transferred bytes. No external calls are made to produce it."

[[upstream.services]]
domain = "ibm.com"
service_type = "cloud-hosting"

[[upstream.services]]
domain = "github.com"
service_type = "code-hosting"
"""

SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/</loc></url>
  <url><loc>https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/licenses</loc></url>
  <url><loc>https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/llms.txt</loc></url>
  <url><loc>https://multiverse.2fy1rnikhcll.us-south.codeengine.appdomain.cloud/carbon.txt</loc></url>
</urlset>
"""

AGENT_CARD = {
    "name": "Gaming Multiverse Reasoner",
    "description": ("Finds hidden paths, bridge games and communities between "
                    "any set of games, computed live in FalkorDB over a "
                    "frozen snapshot of 50,217 IGDB games. The agent "
                    "interface is the MCP server at POST /mcp (see "
                    "documentationUrl); this card is descriptive."),
    "url": PUBLIC_URL + "/",
    "version": "1.0.0",
    "provider": {"organization": "Mike Demopoulos",
                 "url": "https://mikedemo.dev"},
    "documentationUrl": PUBLIC_URL + "/llms.txt",
    "capabilities": {"streaming": False, "pushNotifications": False},
    "defaultInputModes": ["application/json", "text/plain"],
    "defaultOutputModes": ["application/json"],
    "skills": [
        {"id": "find-paths", "name": "Find hidden paths between games",
         "description": "MCP tool find_paths: 2-4 game names or IGDB ids -> per-pair paths with Cypher.",
         "tags": ["games", "graph", "falkordb", "cypher"],
         "examples": ["What connects Elden Ring and Hades?"]},
        {"id": "resolve-game", "name": "Resolve a game title",
         "description": "MCP tool resolve_game / GET /api/resolve: title fragment -> IGDB ids.",
         "tags": ["games", "search", "igdb"],
         "examples": ["Find games matching 'zelda'"]},
        {"id": "random-games", "name": "Random connected games",
         "description": "MCP tool random_games / GET /api/random: 4 random games whose pairs all connect.",
         "tags": ["games", "random"],
         "examples": ["Give me a surprise set of games"]},
    ],
}

OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "Gaming Multiverse Reasoner API",
        "version": "1.0.0",
        "description": ("Read-only JSON API over a FalkorDB graph of 50,217 "
                        "IGDB games (frozen 14 Oct 2026). No authentication. "
                        "Graph queries are capped by a 5,000 ms timeout; "
                        "please be gentle — hackathon demo infrastructure. "
                        "An MCP server with the same capabilities lives at "
                        "POST /mcp; see /llms.txt."),
    },
    "servers": [{"url": PUBLIC_URL}],
    "paths": {
        "/api/health": {"get": {"summary": "Graph status",
            "responses": {"200": {"description": "ok, graph, games, connected_as"}}}},
        "/api/resolve": {"get": {"summary": "Find games by title fragment",
            "parameters": [{"name": "q", "in": "query", "required": True,
                            "schema": {"type": "string"}}],
            "responses": {"200": {"description": "{results: [{igdb_id, name, year, rating_count}]}"}}}},
        "/api/bridges": {"get": {"summary": "Hidden paths between 2-4 games",
            "parameters": [{"name": "ids", "in": "query", "required": True,
                            "schema": {"type": "string"},
                            "description": "Comma-separated IGDB ids"}],
            "responses": {"200": {"description": "{games, pairs: [{a, b, paths: [{path_id, kind, description, cypher}]}], stats: {took_ms}}"}}}},
        "/api/random": {"get": {"summary": "Four random games whose pairs all connect",
            "responses": {"200": {"description": "{games: [{igdb_id, name, rating_count}]}"}}}},
        "/api/narrate": {"post": {"summary": "watsonx narration over a bridges result",
            "requestBody": {"content": {"application/json": {"schema": {"type": "object"}}}},
            "responses": {"200": {"description": "{source, model?, claims: [{text, path_ids}], note?}"}}}},
        "/mcp": {"post": {"summary": "MCP server (streamable HTTP, JSON-RPC 2.0)",
            "responses": {"200": {"description": "JSON-RPC response"}}},
                 "get": {"summary": "MCP server info",
            "responses": {"200": {"description": "{name, version, transport, tools}"}}}},
    },
}

MCP_TOOLS = [
    {"name": "resolve_game",
     "description": "Find games in the 50,217-game IGDB snapshot by title fragment. Returns matches with IGDB ids.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string",
                                               "description": "Title fragment, e.g. 'zelda'"}},
                     "required": ["query"]}},
    {"name": "find_paths",
     "description": ("Find the hidden paths connecting 2-4 games: direct lineage "
                     "(remakes, DLC, expansions), company bridges, similar-game "
                     "chains, common neighbours and shared attributes. Games may "
                     "be names (resolved to their top match) or IGDB ids. Every "
                     "path includes the Cypher that computed it."),
     "inputSchema": {"type": "object",
                     "properties": {"games": {"type": "array", "minItems": 2,
                                               "maxItems": 4,
                                               "items": {"type": ["string", "number"]},
                                               "description": "Game names or IGDB ids"}},
                     "required": ["games"]}},
    {"name": "random_games",
     "description": "Draw 4 random games, validated so every pair connects in the graph.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "game_community",
     "description": "One game's precomputed community (weighted label propagation): size, closest peers, year, rating and summary.",
     "inputSchema": {"type": "object",
                     "properties": {"igdb_id": {"type": "number",
                                                 "description": "IGDB game id"}},
                     "required": ["igdb_id"]}},
    {"name": "narrate",
     "description": ("Compute paths for 2-4 IGDB ids and narrate them with IBM "
                     "watsonx. Claims cite path_ids and are validated in code; "
                     "falls back to labelled template claims if watsonx is down."),
     "inputSchema": {"type": "object",
                     "properties": {"ids": {"type": "array", "minItems": 2,
                                              "maxItems": 4,
                                              "items": {"type": "number"}}},
                     "required": ["ids"]}},
]


def mcp_tool_call(name, args):
    args = args or {}
    if name == "resolve_game":
        return api_resolve(str(args.get("query", "")))
    if name == "random_games":
        return api_random()
    if name == "game_community":
        return game_info(int(args["igdb_id"]))
    if name == "find_paths":
        ids, resolved = [], []
        for g in args.get("games", []):
            if isinstance(g, (int, float)) or (isinstance(g, str) and g.isdigit()):
                ids.append(int(g))
            else:
                hits = api_resolve(str(g)).get("results", [])
                if hits:
                    ids.append(hits[0]["igdb_id"])
                    resolved.append({"query": g, "matched": hits[0]["name"],
                                     "igdb_id": hits[0]["igdb_id"]})
        ids = list(dict.fromkeys(ids))[:4]
        if len(ids) < 2:
            return {"error": "provide 2-4 games (names or IGDB ids); fewer than 2 resolved"}
        out = api_bridges(ids)
        if resolved:
            out["resolved_names"] = resolved
        return out
    if name == "narrate":
        ids = [int(x) for x in (args.get("ids") or [])][:4]
        if len(ids) < 2:
            return {"error": "provide 2-4 IGDB ids"}
        return api_narrate(api_bridges(ids))
    return {"error": f"unknown tool: {name}"}


def mcp_handle(msg):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(msg, dict):
        return None
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None

    def ok(result):
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    def err(code, message):
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": code, "message": message}}

    if method == "initialize":
        pv = (msg.get("params") or {}).get("protocolVersion") or "2025-03-26"
        return ok({"protocolVersion": pv,
                   "capabilities": {"tools": {}},
                   "serverInfo": {"name": "gaming-multiverse-reasoner",
                                  "version": "1.0.0"}})
    if method == "ping":
        return ok({})
    if method == "tools/list":
        return ok({"tools": MCP_TOOLS})
    if method == "tools/call":
        p = msg.get("params") or {}
        try:
            result = mcp_tool_call(p.get("name"), p.get("arguments"))
            is_err = isinstance(result, dict) and "error" in result
            return ok({"content": [{"type": "text",
                                    "text": json.dumps(result)}],
                       "isError": is_err})
        except Exception as e:
            return ok({"content": [{"type": "text",
                                    "text": f"{type(e).__name__}: {e}"}],
                       "isError": True})
    return err(-32601, f"method not found: {method}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8080"))
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()
