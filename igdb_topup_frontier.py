#!/usr/bin/env python3
"""Top-up tier E: widen the slice along the similarity frontier.

After tiers B/H/D the slice holds every game with any IGDB popularity
signal (34,592 games). The remaining ~15.4k to reach ~50k are chosen as
the out-of-slice games most referenced by slice games' similar_games
lists — connectivity by construction, ordered by reference count.
Their full records are appended to games_topup.jsonl, with
involved_companies rows and newly referenced companies appended too.
Manifest gains topup_frontier_* counts and refreshed totals.
"""
import importlib.util, json, os
from collections import Counter

spec = importlib.util.spec_from_file_location(
    "igdb_pull", os.path.expanduser("~/gmr/igdb_pull.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
RAW, paginate, log = mod.RAW, mod.paginate, mod.log
GAME_FIELDS = mod.GAME_FIELDS
TARGET_TOTAL = 50000
SLICE_FILES = ["games_top.jsonl", "games_extra.jsonl", "games_topup.jsonl"]


def read_rows(fname):
    path = os.path.join(RAW, fname)
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    slice_rows = []
    for fn in SLICE_FILES:
        slice_rows.extend(read_rows(fn))
    existing = {r["id"] for r in slice_rows}
    log("slice before frontier:", len(existing))
    want = TARGET_TOTAL - len(existing)
    log("frontier wanted:", want)

    refs = Counter()
    for r in slice_rows:
        for t in r.get("similar_games") or []:
            if t not in existing:
                refs[t] += 1
    frontier = [gid for gid, _ in
                sorted(refs.items(), key=lambda kv: (-kv[1], kv[0]))]
    log("frontier candidates:", len(frontier))
    take_ids = frontier[:want]
    log("frontier ids taken:", len(take_ids))

    # pull full records in id batches, preserving frontier order
    by_id = {}
    for i in range(0, len(take_ids), 500):
        batch = take_ids[i:i + 500]
        rows = paginate("games", GAME_FIELDS,
                        where="id = (" + ",".join(map(str, batch)) + ")")
        for r in rows:
            by_id[r["id"]] = r
        log(f"frontier records batch {i // 500 + 1}: {len(by_id)}")
    taken = [by_id[gid] for gid in take_ids if gid in by_id]
    log("frontier records pulled:", len(taken))
    with open(os.path.join(RAW, "games_topup.jsonl"), "a") as f:
        for r in taken:
            f.write(json.dumps(r) + "\n")

    new_ids = sorted(r["id"] for r in taken)
    company_ids = set()
    inv_total = 0
    for i in range(0, len(new_ids), 500):
        batch = new_ids[i:i + 500]
        rows = paginate("involved_companies",
                        "id,company,game,publisher,developer,porting,supporting",
                        where="game = (" + ",".join(map(str, batch)) + ")",
                        fname="involved_companies.jsonl")
        inv_total += len(rows)
        for r in rows:
            if r.get("company"):
                company_ids.add(r["company"])
        log(f"frontier involved batch {i // 500 + 1}: total {inv_total}")

    have_companies = {r["id"] for r in read_rows("companies.jsonl")}
    missing = sorted(company_ids - have_companies)
    comp_total = 0
    for i in range(0, len(missing), 500):
        batch = missing[i:i + 500]
        rows = paginate("companies", "id,name,slug,country",
                        where="id = (" + ",".join(map(str, batch)) + ")",
                        fname="companies.jsonl")
        comp_total += len(rows)
    log("frontier new companies:", comp_total)

    mpath = os.path.join(RAW, "manifest.json")
    manifest = json.load(open(mpath))
    manifest["topup_frontier_games"] = len(taken)
    manifest["topup_games"] = manifest.get("topup_games", 0) + len(taken)
    manifest["games_total"] = len(existing) + len(taken)
    manifest["involved_companies"] = (manifest.get("involved_companies", 0)
                                      + inv_total)
    manifest["companies"] = manifest.get("companies", 0) + comp_total
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
    log("DONE", json.dumps({k: manifest[k] for k in
                            ("topup_frontier_games", "topup_games",
                             "games_total", "involved_companies",
                             "companies")}))


if __name__ == "__main__":
    main()
