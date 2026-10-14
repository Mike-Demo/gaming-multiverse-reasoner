#!/usr/bin/env python3
"""Community detection for the multiverse graph: label propagation (LPA)
over the game<->game SIMILAR_TO projection (undirected). Deterministic
(fixed seed, smallest-label tie-break). Writes `community` + `community_size`
back onto Game nodes. Honest label for the UI: 'communities detected by
label propagation over IGDB similar-game links'.
"""
import os, random, time
from collections import Counter, defaultdict
from falkordb import FalkorDB

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

db = FalkorDB(host="127.0.0.1", port=6379, password=os.environ["FALKOR_PW"])
g = db.select_graph("multiverse")

rows = g.ro_query(
    "MATCH (a:Game)-[:SIMILAR_TO]-(b:Game) RETURN DISTINCT a.igdb_id, b.igdb_id"
).result_set
adj = defaultdict(set)
for a, b in rows:
    if a != b:
        adj[a].add(b)
        adj[b].add(a)
nodes = list(adj)
log("projection: nodes", len(nodes), "| undirected edges", sum(len(v) for v in adj.values()) // 2)

# Mega-hubs (similar to everything) collapse LPA into one monster community.
# Exclude the highest-degree games from voting; assign them afterwards to
# their strongest neighbour community.
HUB_DEGREE = 50
hubs = {n for n in nodes if len(adj[n]) > HUB_DEGREE}
core = [n for n in nodes if n not in hubs]
log(f"hubs excluded from voting (degree>{HUB_DEGREE}):", len(hubs))

labels = {n: n for n in core}
rng = random.Random(42)
for it in range(25):
    order = core[:]
    rng.shuffle(order)
    changed = 0
    for n in order:
        counts = Counter(labels[m] for m in adj[n] if m in labels)
        if not counts:
            continue
        best = min(counts, key=lambda lab: (-counts[lab], lab))
        if best != labels[n]:
            labels[n] = best
            changed += 1
    sizes = Counter(labels.values())
    big = sum(1 for s in sizes.values() if s >= 5)
    log(f"iter {it + 1}: changed {changed} | communities {len(sizes)} | size>=5: {big}")
    if changed == 0:
        break

for h in hubs:
    counts = Counter(labels[m] for m in adj[h] if m in labels)
    labels[h] = min(counts, key=lambda lab: (-counts[lab], lab)) if counts else h
log("hubs assigned post-hoc")

sizes = Counter(labels.values())
items = list(labels.items())
for i in range(0, len(items), 1000):
    batch = [{"id": n, "c": labels[n], "s": sizes[labels[n]]} for n, _ in
             [(k, v) for k, v in items[i:i + 1000]]]
    g.query("UNWIND $rows AS r MATCH (g:Game {igdb_id: r.id}) "
            "SET g.community = r.c, g.community_size = r.s", {"rows": batch})
log("community properties written")

top = sizes.most_common(10)
log("top community sizes:", top)
for name, gid in [("Elden Ring", 119133), ("Hades", 113112),
                  ("Stardew Valley", 17000), ("Baldur's Gate III", 119171)]:
    c = labels.get(gid)
    members = g.ro_query(
        "MATCH (g:Game {community: $c}) RETURN g.name, g.rating_count "
        "ORDER BY coalesce(g.rating_count, 0) DESC LIMIT 6", {"c": c}).result_set
    log(f"{name}: community {c} (size {sizes[c]}) top: {[m[0] for m in members]}")
log("COMMUNITIES DONE")
