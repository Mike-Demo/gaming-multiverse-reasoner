#!/usr/bin/env python3
"""Top-up: widen the slice from the IGDB-rated population to ~50k games.

Reality check (probed live 2026-10-14): the games field `follows` is dead
on IGDB (null for every game), and the popularity universe is small —
rating_count > 0 yields 25,165 games, total_rating_count > 0 only 31,170.
So the ~50k slice is assembled in tiers, most-signal first:
  B: total_rating_count > 0 & rating_count = 0   (critic-rated, ~627)
  H: hypes > 0 & rating_count = 0                (anticipated, ~338)
  D: rating_count = 0, newest first (id desc)    (fills to the target;
     newest zero-rated games carry full modern metadata)
New games land in games_topup.jsonl; their involved_companies rows and
any newly referenced companies are appended. Manifest gains topup_* data.
"""
import importlib.util, json, os

spec = importlib.util.spec_from_file_location(
    "igdb_pull", os.path.expanduser("~/gmr/igdb_pull.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
RAW, paginate, log = mod.RAW, mod.paginate, mod.log
GAME_FIELDS = mod.GAME_FIELDS
TARGET_TOTAL = 50000


def read_ids(fname):
    ids = set()
    path = os.path.join(RAW, fname)
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if line.strip():
                    ids.add(json.loads(line)["id"])
    return ids


def main():
    existing = read_ids("games_top.jsonl") | read_ids("games_extra.jsonl")
    log("existing slice games:", len(existing))
    taken, taken_ids = [], set()

    def add_tier(name, where, sort, cap):
        want = TARGET_TOTAL - len(existing) - len(taken)
        if want <= 0:
            return
        rows = paginate("games", GAME_FIELDS, where=where, sort=sort,
                        cap=cap)
        new = []
        for r in rows:
            if r["id"] not in existing and r["id"] not in taken_ids:
                new.append(r)
                taken_ids.add(r["id"])
                if len(new) >= want:
                    break
        log(f"tier {name}: candidates {len(rows)}, taken {len(new)}")
        taken.extend(new)

    add_tier("B-total", "total_rating_count > 0 & rating_count = 0",
             "total_rating_count desc", 5000)
    add_tier("H-hypes", "hypes > 0 & rating_count = 0", "hypes desc", 5000)
    add_tier("D-newest", "rating_count = 0", "id desc", 40000)
    log("top-up games total:", len(taken))

    with open(os.path.join(RAW, "games_topup.jsonl"), "w") as f:
        for r in taken:
            f.write(json.dumps(r) + "\n")

    # involved companies for the new games (paginate appends to the file)
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
        log(f"topup involved batch {i // 500 + 1}: total {inv_total}")

    # companies not already in companies.jsonl (paginate appends)
    have_companies = read_ids("companies.jsonl")
    missing = sorted(company_ids - have_companies)
    comp_total = 0
    for i in range(0, len(missing), 500):
        batch = missing[i:i + 500]
        rows = paginate("companies", "id,name,slug,country",
                        where="id = (" + ",".join(map(str, batch)) + ")",
                        fname="companies.jsonl")
        comp_total += len(rows)
    log("topup new companies:", comp_total)

    mpath = os.path.join(RAW, "manifest.json")
    manifest = json.load(open(mpath))
    manifest["topup_games"] = len(taken)
    manifest["topup_involved_companies"] = inv_total
    manifest["topup_companies"] = comp_total
    manifest["games_total"] = len(existing) + len(taken)
    manifest["involved_companies"] = (manifest.get("involved_companies", 0)
                                      + inv_total)
    manifest["companies"] = manifest.get("companies", 0) + comp_total
    with open(mpath, "w") as f:
        json.dump(manifest, f, indent=2)
    log("DONE", json.dumps({k: manifest[k] for k in
                            ("topup_games", "games_total",
                             "involved_companies", "companies")}))


if __name__ == "__main__":
    main()
