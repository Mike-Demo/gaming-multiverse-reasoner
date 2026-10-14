#!/usr/bin/env python3
"""Idempotent FalkorDB loader for the Gaming Multiverse Reasoner.

Rebuilds the `multiverse` graph from the frozen IGDB slice in
~/gmr-data/raw/: deletes the graph if present, recreates indexes, and
batch-loads nodes + edges with UNWIND. Re-running produces the same graph.

Master password comes from the FALKOR_PW env var (never stored on disk here).
"""
import json, os, sys, time

from falkordb import FalkorDB

RAW = os.path.expanduser("~/gmr-data/raw")
GRAPH = "multiverse"
BATCH = 1000

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def read_jsonl(fname):
    path = os.path.join(RAW, fname)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]

def chunks(rows, n=BATCH):
    for i in range(0, len(rows), n):
        yield rows[i:i + n]

def main():
    pw = os.environ["FALKOR_PW"]
    db = FalkorDB(host="127.0.0.1", port=6379, password=pw)
    g = db.select_graph(GRAPH)
    try:
        g.delete()
        log("deleted existing graph")
    except Exception:
        log("no existing graph to delete")

    for label in ["Game", "Genre", "Theme", "Platform", "Company",
                  "Franchise", "Collection", "GameMode", "PlayerPerspective",
                  "Engine"]:
        g.query(f"CREATE INDEX FOR (n:{label}) ON (n.igdb_id)")

    # ---- games (dedupe across slice files) ----
    games = {}
    for fn in ["games_top.jsonl", "games_extra.jsonl", "games_topup.jsonl",
               "games_queercade.jsonl"]:
        for row in read_jsonl(fn):
            games.setdefault(row["id"], row)
    # collections were pulled separately (IGDB renamed collection -> collections)
    for row in read_jsonl("games_collections.jsonl"):
        if row["id"] in games and row.get("collections"):
            games[row["id"]]["collections"] = row["collections"]
    games = list(games.values())
    log("games to load:", len(games))

    def game_row(r):
        out = {"igdb_id": r["id"], "name": r.get("name") or "",
               "slug": r.get("slug") or ""}
        for k in ["rating", "rating_count", "total_rating", "total_rating_count",
                  "aggregated_rating", "first_release_date"]:
            if r.get(k) is not None:
                out[k] = r[k]
        if r.get("summary"):
            out["summary"] = r["summary"][:400]
        return out

    for batch in chunks([game_row(r) for r in games]):
        g.query("UNWIND $rows AS r CREATE (g:Game) SET g = r", {"rows": batch})
    log("Game nodes done")

    # ---- attribute nodes ----
    attr_specs = [("genres.jsonl", "Genre"), ("themes.jsonl", "Theme"),
                  ("platforms.jsonl", "Platform"),
                  ("game_modes.jsonl", "GameMode"),
                  ("player_perspectives.jsonl", "PlayerPerspective"),
                  ("franchises.jsonl", "Franchise"),
                  ("collections.jsonl", "Collection"),
                  ("game_engines.jsonl", "Engine"),
                  ("companies.jsonl", "Company")]
    for fname, label in attr_specs:
        rows = [{"igdb_id": r["id"], "name": r.get("name") or "",
                 "slug": r.get("slug") or ""} for r in read_jsonl(fname)]
        for batch in chunks(rows):
            g.query(f"UNWIND $rows AS r CREATE (n:{label}) SET n = r",
                    {"rows": batch})
        log(label, "nodes:", len(rows))

    game_ids = set(games_by_id := {r["id"]: r for r in games})

    def edge_batch(pairs, cypher, label):
        total = 0
        for batch in chunks(pairs):
            g.query(cypher, {"rows": batch})
            total += len(batch)
        log(label, "edges:", total)

    # ---- game -> attribute edges from game records ----
    def collect(field, single=False):
        pairs = []
        for r in games:
            vals = [r.get(field)] if single else (r.get(field) or [])
            for v in vals:
                if v:
                    pairs.append({"g": r["id"], "t": v})
        return pairs

    edge_batch(collect("genres"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Genre {igdb_id: r.t}) CREATE (g)-[:HAS_GENRE]->(t)",
               "HAS_GENRE")
    edge_batch(collect("themes"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Theme {igdb_id: r.t}) CREATE (g)-[:HAS_THEME]->(t)",
               "HAS_THEME")
    edge_batch(collect("platforms"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Platform {igdb_id: r.t}) CREATE (g)-[:ON_PLATFORM]->(t)",
               "ON_PLATFORM")
    edge_batch(collect("game_modes"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:GameMode {igdb_id: r.t}) CREATE (g)-[:HAS_MODE]->(t)",
               "HAS_MODE")
    edge_batch(collect("player_perspectives"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:PlayerPerspective {igdb_id: r.t}) "
               "CREATE (g)-[:HAS_PERSPECTIVE]->(t)", "HAS_PERSPECTIVE")
    edge_batch(collect("franchises"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Franchise {igdb_id: r.t}) CREATE (g)-[:IN_FRANCHISE]->(t)",
               "IN_FRANCHISE")
    edge_batch(collect("collections"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Collection {igdb_id: r.t}) CREATE (g)-[:IN_COLLECTION]->(t)",
               "IN_COLLECTION")
    edge_batch(collect("game_engines"),
               "UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
               "MATCH (t:Engine {igdb_id: r.t}) CREATE (g)-[:HAS_ENGINE]->(t)",
               "HAS_ENGINE")

    # ---- lineage: Game -> Game version family edges ----
    # parent_game / version_parent sit on the child record; dlcs /
    # expansions / remakes / remasters sit on the parent record, so those
    # pairs are inverted (the listed game points back at the record owner).
    def dedupe(pairs):
        seen, out = set(), []
        for p in pairs:
            k = (p["g"], p["t"])
            if k not in seen and p["g"] != p["t"]:
                seen.add(k)
                out.append(p)
        return out

    def lineage_batch(pairs, etype):
        edge_batch(dedupe([p for p in pairs
                           if p["g"] in game_ids and p["t"] in game_ids]),
                   "UNWIND $rows AS r MATCH (a:Game {igdb_id: r.g}) "
                   f"MATCH (b:Game {{igdb_id: r.t}}) CREATE (a)-[:{etype}]->(b)",
                   etype)

    lineage_batch(collect("parent_game", single=True), "PART_OF")
    lineage_batch(collect("version_parent", single=True), "VERSION_OF")
    for field, etype in [("dlcs", "DLC_OF"), ("expansions", "EXPANSION_OF"),
                         ("remakes", "REMAKE_OF"), ("remasters", "REMASTER_OF")]:
        inv = []
        for r in games:
            for child in r.get(field) or []:
                inv.append({"g": child, "t": r["id"]})
        lineage_batch(inv, etype)

    # ---- company edges from involved_companies ----
    role_edges = [("developer", "DEVELOPED_BY"), ("publisher", "PUBLISHED_BY"),
                  ("porting", "PORTED_BY"), ("supporting", "SUPPORTED_BY")]
    inv = read_jsonl("involved_companies.jsonl")
    for flag, etype in role_edges:
        pairs = [{"g": r["game"], "t": r["company"]} for r in inv
                 if r.get(flag) and r.get("company") and r["game"] in game_ids]
        edge_batch(pairs,
                   f"UNWIND $rows AS r MATCH (g:Game {{igdb_id: r.g}}) "
                   f"MATCH (t:Company {{igdb_id: r.t}}) CREATE (g)-[:{etype}]->(t)",
                   etype)

    # ---- similar games (only inside the slice) ----
    pairs = []
    for r in games:
        for t in r.get("similar_games") or []:
            if t in game_ids and t != r["id"]:
                pairs.append({"g": r["id"], "t": t})
    edge_batch(pairs,
               "UNWIND $rows AS r MATCH (a:Game {igdb_id: r.g}) "
               "MATCH (b:Game {igdb_id: r.t}) CREATE (a)-[:SIMILAR_TO]->(b)",
               "SIMILAR_TO")

    # ---- hub degrees: precompute game-degree on attribute nodes ----
    # Query-time hub suppression filters on this property; computing degrees
    # inline per candidate is far too slow.
    for label in ["Genre", "Theme", "Platform", "Company", "Franchise",
                  "Collection", "GameMode", "PlayerPerspective", "Engine"]:
        g.query(f"MATCH (x:{label}) SET x.degree = size([(x)--(:Game) | 1])")
    log("degree pass done")

    # ---- verify ----
    res = g.ro_query("MATCH (n) RETURN count(n)").result_set
    log("total nodes:", res[0][0])
    res = g.ro_query("MATCH ()-[r]->() RETURN count(r)").result_set
    log("total relationships:", res[0][0])
    log("LOAD DONE")

if __name__ == "__main__":
    main()
