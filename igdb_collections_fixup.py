#!/usr/bin/env python3
"""Pull `collections` for every game in the slice and add IN_COLLECTION edges.

IGDB renamed the games field collection -> collections after the main pull
field list was written, so the frozen games files lack it. This writes
games_collections.jsonl (id + collections) and creates the missing edges in
the already-loaded `multiverse` graph. graph_load.py merges this file on
future full reloads, so the fix is permanent.
"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "igdb_pull", os.path.expanduser("~/gmr/igdb_pull.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
RAW, paginate, log = mod.RAW, mod.paginate, mod.log

ids = set()
for fn in ["games_top.jsonl", "games_extra.jsonl", "games_queercade.jsonl"]:
    with open(os.path.join(RAW, fn)) as f:
        for line in f:
            if line.strip():
                ids.add(json.loads(line)["id"])
ids = sorted(ids)
log("slice games:", len(ids))

open(os.path.join(RAW, "games_collections.jsonl"), "w").close()
total = 0
for i in range(0, len(ids), 500):
    batch = ids[i:i + 500]
    rows = paginate("games", "id,collections",
                    where="id = (" + ",".join(map(str, batch)) + ")",
                    fname="games_collections.jsonl")
    total += sum(1 for r in rows if r.get("collections"))
    log(f"batch {i // 500 + 1}: games with collections so far {total}")
log("games with collections:", total)

from falkordb import FalkorDB
db = FalkorDB(host="127.0.0.1", port=6379, password=os.environ["FALKOR_PW"])
g = db.select_graph("multiverse")
pairs = []
with open(os.path.join(RAW, "games_collections.jsonl")) as f:
    for line in f:
        if line.strip():
            r = json.loads(line)
            for c in r.get("collections") or []:
                pairs.append({"g": r["id"], "t": c})
for i in range(0, len(pairs), 1000):
    g.query("UNWIND $rows AS r MATCH (g:Game {igdb_id: r.g}) "
            "MATCH (t:Collection {igdb_id: r.t}) "
            "CREATE (g)-[:IN_COLLECTION]->(t)", {"rows": pairs[i:i + 1000]})
log("IN_COLLECTION edges created:", len(pairs))
