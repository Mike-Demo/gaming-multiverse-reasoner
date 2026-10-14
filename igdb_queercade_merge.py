#!/usr/bin/env python3
"""Ensure every QueerCade (Sanity) game is in the frozen IGDB slice.

Reads ~/gmr-data/raw/queercade_ids.json (IGDB ids parsed from Sanity
_id values of the form game-igdb-<id>). Games missing from the slice are
pulled with the same fields as the main pull into games_queercade.jsonl;
their involved_companies rows are appended to involved_companies.jsonl and
any newly referenced companies are appended to companies.jsonl. Manifest
gains queercade_* counts and refreshed totals.
"""
import importlib.util, json, os, time

spec = importlib.util.spec_from_file_location(
    "igdb_pull", os.path.expanduser("~/gmr/igdb_pull.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
RAW, paginate, log = mod.RAW, mod.paginate, mod.log

def read_ids(fname, key="id"):
    ids = set()
    path = os.path.join(RAW, fname)
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    ids.add(json.loads(line)[key])
    return ids

qc_ids = set(json.load(open(os.path.join(RAW, "queercade_ids.json"))))
slice_ids = (read_ids("games_top.jsonl") | read_ids("games_extra.jsonl")
             | read_ids("games_topup.jsonl"))
missing = sorted(qc_ids - slice_ids)
log(f"queercade total {len(qc_ids)} | already in slice {len(qc_ids & slice_ids)}"
    f" | missing {len(missing)}")

open(os.path.join(RAW, "games_queercade.jsonl"), "w").close()
added = 0
for i in range(0, len(missing), 500):
    batch = missing[i:i + 500]
    rows = paginate("games", mod.GAME_FIELDS,
                    where="id = (" + ",".join(map(str, batch)) + ")",
                    fname="games_queercade.jsonl")
    added += len(rows)
    log(f"games batch {i // 500 + 1}: +{len(rows)}")
found_ids = read_ids("games_queercade.jsonl")
not_on_igdb = [g for g in missing if g not in found_ids]
log("games added:", added, "| ids IGDB did not return:", len(not_on_igdb), not_on_igdb[:10])

inv_added, company_ids = 0, set()
for i in range(0, len(missing), 500):
    batch = missing[i:i + 500]
    rows = paginate("involved_companies",
                    "id,company,game,publisher,developer,porting,supporting",
                    where="game = (" + ",".join(map(str, batch)) + ")",
                    fname="involved_companies.jsonl")
    inv_added += len(rows)
    for r in rows:
        if r.get("company"):
            company_ids.add(r["company"])
log("involved_companies appended:", inv_added)

existing_companies = read_ids("companies.jsonl")
new_companies = sorted(company_ids - existing_companies)
comp_added = 0
for i in range(0, len(new_companies), 500):
    batch = new_companies[i:i + 500]
    rows = paginate("companies", "id,name,slug,country",
                    where="id = (" + ",".join(map(str, batch)) + ")",
                    fname="companies.jsonl")
    comp_added += len(rows)
log("companies appended:", comp_added)

mpath = os.path.join(RAW, "manifest.json")
with open(mpath) as f:
    manifest = json.load(f)
manifest["queercade_total"] = len(qc_ids)
manifest["queercade_already_in_slice"] = len(qc_ids & slice_ids)
manifest["queercade_added"] = added
manifest["queercade_not_on_igdb"] = not_on_igdb
manifest["games_total"] = manifest.get("games_total", 0) + added
manifest["involved_companies"] = manifest.get("involved_companies", 0) + inv_added
manifest["companies"] = manifest.get("companies", 0) + comp_added
manifest["companies_referenced"] = len(existing_companies | company_ids)
manifest["queercade_merged_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
with open(mpath, "w") as f:
    json.dump(manifest, f, indent=2)
log("MERGE DONE", json.dumps({k: manifest[k] for k in
    ["queercade_total", "queercade_already_in_slice", "queercade_added",
     "games_total", "involved_companies", "companies"]}))
