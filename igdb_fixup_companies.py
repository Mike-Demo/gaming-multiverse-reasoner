#!/usr/bin/env python3
"""Fix-up: re-pull involved_companies + companies for the frozen slice.

The first pull used IGDB field names published/developed; the API calls
them publisher/developer. Games + attribute tables from the first pull are
untouched. Reads game ids from the existing games_top/games_extra files.
"""
import json, os, sys, time
sys.path.insert(0, os.path.expanduser("~/gmr"))
os.chdir(os.path.expanduser("~/gmr"))
import importlib.util
spec = importlib.util.spec_from_file_location("igdb_pull", os.path.expanduser("~/gmr/igdb_pull.py"))
mod = importlib.util.module_from_spec(spec)
# avoid running main(): the module only runs main under __main__ guard
spec.loader.exec_module(mod)

RAW = mod.RAW
paginate, log = mod.paginate, mod.log

game_ids = set()
for fn in ["games_top.jsonl", "games_extra.jsonl"]:
    with open(os.path.join(RAW, fn)) as f:
        for line in f:
            line = line.strip()
            if line:
                game_ids.add(json.loads(line)["id"])
game_ids = sorted(game_ids)
log("games in slice:", len(game_ids))

open(os.path.join(RAW, "involved_companies.jsonl"), "w").close()
inv_total, company_ids = 0, set()
for i in range(0, len(game_ids), 500):
    batch = game_ids[i:i + 500]
    rows = paginate("involved_companies",
                    "id,company,game,publisher,developer,porting,supporting",
                    where="game = (" + ",".join(map(str, batch)) + ")",
                    fname="involved_companies.jsonl")
    inv_total += len(rows)
    for r in rows:
        if r.get("company"):
            company_ids.add(r["company"])
    log(f"involved batch {i // 500 + 1}/{(len(game_ids) + 499) // 500}: total {inv_total}")
log("involved_companies:", inv_total, "| companies referenced:", len(company_ids))

cids = sorted(company_ids)
open(os.path.join(RAW, "companies.jsonl"), "w").close()
comp_total = 0
for i in range(0, len(cids), 500):
    batch = cids[i:i + 500]
    rows = paginate("companies", "id,name,slug,country",
                    where="id = (" + ",".join(map(str, batch)) + ")",
                    fname="companies.jsonl")
    comp_total += len(rows)
log("companies:", comp_total)

mpath = os.path.join(RAW, "manifest.json")
manifest = {}
if os.path.exists(mpath):
    with open(mpath) as f:
        manifest = json.load(f)
manifest["involved_companies"] = inv_total
manifest["companies_referenced"] = len(company_ids)
manifest["companies"] = comp_total
manifest["fixup_finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
with open(mpath, "w") as f:
    json.dump(manifest, f, indent=2)
log("FIXUP DONE", json.dumps(manifest))
