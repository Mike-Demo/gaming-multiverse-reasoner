#!/usr/bin/env python3
"""IGDB frozen-slice pull (Day 1 Block 4 / Day 2 snapshot source).

Reads credentials from ~/gmr/.env, exchanges a Twitch token, and pulls:
  - games_top.jsonl        top games by rating_count (target ~20k)
  - games_extra.jsonl      franchise/collection siblings of the demo games
  - genres/themes/platforms/game_modes/player_perspectives/franchises/
    collections .jsonl     full attribute tables (small)
  - involved_companies.jsonl  rows for every pulled game (batched)
  - companies.jsonl        companies referenced by those rows (batched)
Raw JSONL lands in ~/gmr-data/raw/; manifest.json records counts + timing.
IGDB rate limit is 4 req/s; we sleep 0.27s between calls.
"""
import json, os, subprocess, sys, time

BASE = "https://api.igdb.com/v4"
RAW = os.path.expanduser("~/gmr-data/raw")
DEMO_GAMES = [119133, 113112, 17000, 119171]  # Elden Ring, Hades, Stardew, BG3
TARGET_GAMES = 20000
GAME_FIELDS = ("id,name,slug,rating,rating_count,aggregated_rating,"
               "aggregated_rating_count,total_rating,total_rating_count,"
               "first_release_date,summary,cover,category,status,parent_game,"
               "version_parent,genres,themes,platforms,franchises,collections,"
               "game_modes,player_perspectives,similar_games,involved_companies,"
               "dlcs,expansions,remakes,remasters")

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)

def env():
    d = {}
    with open(os.path.expanduser("~/gmr/.env")) as f:
        for line in f:
            line = line.strip()
            if "=" in line:
                k, v = line.split("=", 1)
                d[k] = v
    return d

CREDS = env()
CID = CREDS["IGDB_CLIENT_ID"]

def curl(args, data=None):
    cmd = ["curl", "-s", "--max-time", "60"] + args
    if data is not None:
        cmd += ["-d", data]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    return r.stdout

def token():
    out = curl(["-X", "POST", "https://id.twitch.tv/oauth2/token",
                "-d", f"client_id={CID}&client_secret={CREDS['IGDB_CLIENT_SECRET']}"
                      "&grant_type=client_credentials"])
    return json.loads(out)["access_token"]

TOK = token()
log("token OK")

def query(endpoint, body):
    time.sleep(0.27)
    out = curl([f"{BASE}/{endpoint}", "-H", f"Client-ID: {CID}",
                "-H", f"Authorization: Bearer {TOK}"], data=body)
    rows = json.loads(out)
    if isinstance(rows, dict) or (rows and isinstance(rows[0], dict)
                                  and "title" in rows[0] and "status" in rows[0]):
        raise RuntimeError(f"{endpoint} error: {str(rows)[:200]} :: {body[:120]}")
    return rows

def paginate(endpoint, fields, where="", sort="", fname=None, cap=None):
    rows_all, offset = [], 0
    while True:
        body = f"fields {fields};"
        if where:
            body += f" where {where};"
        if sort:
            body += f" sort {sort};"
        body += f" limit 500; offset {offset};"
        rows = query(endpoint, body)
        rows_all.extend(rows)
        if fname:
            with open(os.path.join(RAW, fname), "a") as f:
                for r in rows:
                    f.write(json.dumps(r) + "\n")
        if len(rows) < 500 or (cap and len(rows_all) >= cap):
            break
        offset += 500
    return rows_all

def main():
    os.makedirs(RAW, exist_ok=True)
    for fn in ["games_top.jsonl", "games_extra.jsonl", "genres.jsonl",
               "themes.jsonl", "platforms.jsonl", "game_modes.jsonl",
               "player_perspectives.jsonl", "franchises.jsonl",
               "collections.jsonl"]:
        open(os.path.join(RAW, fn), "w").close()
    manifest = {"started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    # --- main game slice ---
    games = paginate("games", GAME_FIELDS, where="rating_count > 0",
                     sort="rating_count desc", fname="games_top.jsonl",
                     cap=TARGET_GAMES)
    if len(games) > TARGET_GAMES:
        games = games[:TARGET_GAMES]
    log("games_top:", len(games))
    manifest["games_top"] = len(games)
    by_id = {g["id"]: g for g in games}

    # --- demo-game franchise/collection expansion ---
    fr_ids, col_ids = set(), set()
    for gid in DEMO_GAMES:
        g = by_id.get(gid)
        if not g:
            rows = query("games", f"fields {GAME_FIELDS}; where id = {gid};")
            g = rows[0] if rows else {}
            if g:
                by_id[gid] = g
        for f in g.get("franchises", []) or []:
            fr_ids.add(f)
        for c in g.get("collections", []) or []:
            col_ids.add(c)
    extra_where = []
    if fr_ids:
        extra_where.append("franchises = (" + ",".join(map(str, sorted(fr_ids))) + ")")
    if col_ids:
        extra_where.append("collections = (" + ",".join(map(str, sorted(col_ids))) + ")")
    extra = []
    if extra_where:
        extra = paginate("games", GAME_FIELDS, where=" | ".join(extra_where),
                         fname="games_extra.jsonl")
    new_extra = [g for g in extra if g["id"] not in by_id]
    for g in new_extra:
        by_id[g["id"]] = g
    log("games_extra new:", len(new_extra), "| total games:", len(by_id))
    manifest["games_extra_new"] = len(new_extra)
    manifest["games_total"] = len(by_id)

    # --- attribute tables (full) ---
    for ep, fields in [
        ("genres", "id,name,slug"), ("themes", "id,name,slug"),
        ("platforms", "id,name,slug,abbreviation"),
        ("game_modes", "id,name,slug"), ("player_perspectives", "id,name,slug"),
        ("franchises", "id,name,slug"), ("collections", "id,name,slug"),
    ]:
        rows = paginate(ep, fields, fname=f"{ep}.jsonl")
        manifest[ep] = len(rows)
        log(ep, len(rows))

    # --- involved companies for every game, batched by game id ---
    game_ids = sorted(by_id)
    inv_path = os.path.join(RAW, "involved_companies.jsonl")
    open(inv_path, "w").close()
    inv_total = 0
    company_ids = set()
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
        log(f"involved_companies batch {i // 500 + 1}: total {inv_total}")
    manifest["involved_companies"] = inv_total
    manifest["companies_referenced"] = len(company_ids)

    # --- companies referenced ---
    cids = sorted(company_ids)
    open(os.path.join(RAW, "companies.jsonl"), "w").close()
    comp_total = 0
    for i in range(0, len(cids), 500):
        batch = cids[i:i + 500]
        rows = paginate("companies", "id,name,slug,country",
                        where="id = (" + ",".join(map(str, batch)) + ")",
                        fname="companies.jsonl")
        comp_total += len(rows)
    manifest["companies"] = comp_total
    log("companies:", comp_total)

    manifest["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(os.path.join(RAW, "manifest.json"), "w") as f:
        json.dump(manifest, f, indent=2)
    log("DONE", json.dumps(manifest))

if __name__ == "__main__":
    main()
