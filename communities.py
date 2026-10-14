#!/usr/bin/env python3
"""Community detection for the multiverse graph: weighted label
propagation (LPA) over the game<->game SIMILAR_TO projection (undirected).
Mutual similar links (both games list each other) weigh double; labels
whose community reaches SIZE_CAP stop gaining members, which prevents
the single monster community plain LPA collapses into on a dense graph.
Deterministic (fixed seed, smallest-label tie-break). Writes `community`
+ `community_size` back onto Game nodes. Honest label for the UI:
'communities detected by label propagation over IGDB similar-game links'.
"""
import os, random, time
from collections import Counter, defaultdict
from falkordb import FalkorDB

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

db = FalkorDB(host="127.0.0.1", port=6379, password=os.environ["FALKOR_PW"])
g = db.select_graph("multiverse")

# FalkorDB caps a single result set at 10,000 rows on this deployment, so
# the projection is fetched in igdb_id ranges (index-backed); a range that
# still returns a full page is split recursively so nothing truncates.
def fetch_range(lo, hi, directed):
    rows = g.ro_query(
        "MATCH (a:Game)-[:SIMILAR_TO]->(b:Game) "
        "WHERE a.igdb_id >= $lo AND a.igdb_id < $hi "
        "RETURN a.igdb_id, b.igdb_id", {"lo": lo, "hi": hi}).result_set
    if len(rows) >= 10000 and hi - lo > 1:
        mid = (lo + hi) // 2
        fetch_range(lo, mid, directed)
        fetch_range(mid, hi, directed)
        return
    for a, b in rows:
        if a != b:
            directed.add((a, b))


bounds = g.ro_query(
    "MATCH (g:Game) RETURN min(g.igdb_id), max(g.igdb_id)").result_set[0]
directed = set()
SPAN = 20000
lo = bounds[0]
while lo <= bounds[1]:
    fetch_range(lo, lo + SPAN, directed)
    lo += SPAN
log("projection fetched (paged by id range):", len(directed), "directed")

# Weighted undirected projection: mutual links weigh 2, one-way links 1.
adj = defaultdict(dict)
for a, b in directed:
    w = 2 if (b, a) in directed else 1
    adj[a][b] = w
    adj[b][a] = w
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
SIZE_CAP = 1500  # a label at the cap stops gaining members (anti-monster)
for it in range(25):
    order = core[:]
    rng.shuffle(order)
    sizes = Counter(labels.values())
    changed = 0
    for n in order:
        counts = Counter()
        for m, w in adj[n].items():
            if m in labels:
                counts[labels[m]] += w
        if not counts:
            continue
        cand = {lab: c for lab, c in counts.items()
                if sizes[lab] < SIZE_CAP or lab == labels[n]}
        if not cand:
            continue
        best = min(cand, key=lambda lab: (-cand[lab], lab))
        if best != labels[n]:
            sizes[labels[n]] -= 1
            sizes[best] += 1
            labels[n] = best
            changed += 1
    sizes = Counter(labels.values())
    big = sum(1 for s in sizes.values() if s >= 5)
    log(f"iter {it + 1}: changed {changed} | communities {len(sizes)} | size>=5: {big}")
    if changed == 0:
        break

# Post-hoc hub assignment scores labels by weighted votes normalised by
# community size — otherwise every hub lands in the biggest community it
# touches (the "famous games" soup) regardless of its specific ties.
sizes = Counter(labels.values())
for h in hubs:
    counts = Counter()
    for m, w in adj[h].items():
        if m in labels:
            counts[labels[m]] += w / max(sizes[labels[m]], 1)
    if counts:
        best = min(counts, key=lambda lab: (-counts[lab], lab))
        labels[h] = best
        sizes[best] += 1
    else:
        labels[h] = h
        sizes[h] += 1
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
