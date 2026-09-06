"""
Post-flip live smoke test for the FULL canonical cutover.

Run AFTER the Railway env flip (SNOWFLAKE_PROD_DATABASE=CAFC_DB,
SNOWFLAKE_PROD_SCHEMA=CORE, CANONICAL_DB=CAFC_DB, PLATFORM_DB_SCHEMA=APP_COMPAT,
CORE_DB_SCHEMA=CORE, WRITE_DB unset). Confirms the live prod app is on the
canonical layer, every role can read, a write lands in CAFC_DB.CORE (not
legacy), and RECRUITMENT_TEST.PUBLIC row counts stay flat.

    .venv/bin/python -m python.cutover.smoke_canonical \
        --base-url https://<prod-api> \
        --creds-file /path/creds.json     # {role: {username, password}}
        [--legacy RECRUITMENT_TEST.PUBLIC]
        [--skip-writes]

Exit 0 = all green. Any FAIL => consider rollback (plan Phase E).
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from python import _snowflake

READ_EPS = [
    ("search", "/players/search?query=a&limit=5"),
    ("analytics_timeline", "/analytics/timeline"),
    ("intel_all", "/intel_reports/all?page=1&limit=5"),
    ("internal_recs", "/internal/recommendations"),
    ("agents_recs", "/agents/recommendations"),
]
LEGACY_TABLES = ["PLAYERS", "MATCHES", "SCOUT_REPORTS", "SCOUT_REPORT_ATTRIBUTE_SCORES",
                 "PLAYER_NOTES", "PLAYER_LISTS", "PLAYER_LIST_ITEMS", "PLAYER_LIST_FLAGS",
                 "PLAYER_STAGE_HISTORY", "USERS", "PLAYER_RECOMMENDATIONS"]

res: list[tuple[str, bool, str]] = []


def chk(name, ok, detail=""):
    res.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def _req(method, url, tok=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    r = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=90) as resp:
            raw = resp.read().decode() or "null"
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return None, str(e)


def login(base, u, p):
    d = urllib.parse.urlencode({"username": u, "password": p, "grant_type": "password"}).encode()
    r = urllib.request.Request(base + "/token", data=d,
                               headers={"Content-Type": "application/x-www-form-urlencoded"})
    with urllib.request.urlopen(r, timeout=30) as resp:
        return json.load(resp)["access_token"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--creds-file", required=True)
    ap.add_argument("--legacy", default="RECRUITMENT_TEST.PUBLIC")
    ap.add_argument("--skip-writes", action="store_true")
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    creds = json.load(open(args.creds_file))

    cur = _snowflake.get_connection().cursor()
    cur.execute("USE ROLE ACCOUNTADMIN")
    cur.execute("USE WAREHOUSE DEVELOPMENT_WH")

    # ---- legacy baseline row counts (must not move) ----
    lc0 = {}
    for t in LEGACY_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {args.legacy}.{t}")
            lc0[t] = cur.fetchone()[0]
        except Exception as e:
            lc0[t] = f"ERR {e}"
    print(f"legacy baseline: {lc0}")

    # ---- 1. app is on the canonical layer ----
    admin_role = next((r for r in creds if r == "admin"), list(creds)[0])
    tok_admin = None
    try:
        tok_admin = login(base, creds[admin_role]["username"], creds[admin_role]["password"])
        st, meta = _req("GET", base + "/database/metadata", tok_admin)
        ok = st == 200 and isinstance(meta, dict) and meta.get("players_table", {}).get("count")
        # canonical players_table count should be the ~123k canonical set, not legacy ~84k
        cnt = meta.get("players_table", {}).get("count") if isinstance(meta, dict) else None
        chk("1 /database/metadata shows the canonical player count (>100k, not legacy ~84k)",
            bool(ok) and isinstance(cnt, int) and cnt > 100000, f"st={st} count={cnt}")
    except Exception as e:
        chk("1 admin login + /database/metadata", False, str(e))

    # ---- 2. every role logs in + reads ----
    role_tokens = {}
    for role, c in creds.items():
        try:
            role_tokens[role] = login(base, c["username"], c["password"])
            chk(f"2 login OK: {role}", True)
        except Exception as e:
            chk(f"2 login OK: {role}", False, str(e))
    for role, tok in role_tokens.items():
        for name, path in READ_EPS:
            st, _ = _req("GET", base + path, tok)
            chk(f"2r {role} {name} HTTP<500", st is not None and st < 500, f"st={st}")

    # ---- 3. write smoke (admin): a scout report + a note must land in CORE ----
    if not args.skip_writes and tok_admin:
        tag = f"cutlive{int(time.time())}"
        # need a real player id
        st, sr = _req("GET", base + "/players/search?query=a&limit=1", tok_admin)
        pid = None
        try:
            pid = sr["players"][0]["player_id"]
        except Exception:
            pass
        chk("3a got a player id to test against", pid is not None, f"search resp={str(sr)[:120]}")

        if pid:
            st, b = _req("POST", base + "/scout_reports", tok_admin,
                         {"player_id": pid, "playerPosition": "CB", "playerBuild": "Athletic",
                          "playerHeight": "188cm", "assessmentSummary": tag,
                          "reportType": "Player Assessment", "performanceScore": 7})
            rid = b.get("report_id") if isinstance(b, dict) else None
            cur.execute(f"SELECT COUNT(*) FROM CAFC_DB.CORE.SCOUT_REPORTS WHERE SUMMARY=%s", (tag,))
            in_core = cur.fetchone()[0]
            cur.execute(f"SELECT COUNT(*) FROM {args.legacy}.SCOUT_REPORTS WHERE SUMMARY=%s", (tag,))
            in_legacy = cur.fetchone()[0]
            chk("3b POST /scout_reports -> lands in CAFC_DB.CORE, NOT legacy",
                st == 200 and in_core == 1 and in_legacy == 0, f"st={st} core={in_core} legacy={in_legacy} b={b}")
            if rid:
                cur.execute(f"DELETE FROM CAFC_DB.CORE.SCOUT_REPORTS WHERE ID=%s", (rid,))

            st, b = _req("POST", base + f"/players/{pid}/notes", tok_admin,
                         {"player_id": pid, "note_content": tag})
            cur.execute(f"SELECT COUNT(*) FROM CAFC_DB.CORE.PLAYER_NOTES WHERE NOTE_CONTENT=%s", (tag,))
            chk("3c POST /players/{id}/notes -> lands in CAFC_DB.CORE.PLAYER_NOTES",
                st == 200 and cur.fetchone()[0] == 1, f"st={st} b={b}")
            cur.execute(f"DELETE FROM CAFC_DB.CORE.PLAYER_NOTES WHERE NOTE_CONTENT=%s", (tag,))

    # ---- 4. legacy row counts unchanged ----
    for t in LEGACY_TABLES:
        try:
            cur.execute(f"SELECT COUNT(*) FROM {args.legacy}.{t}")
            n = cur.fetchone()[0]
            chk(f"4 legacy {t} count unchanged ({lc0[t]})", n == lc0[t], f"{lc0[t]} -> {n}")
        except Exception as e:
            chk(f"4 legacy {t} count check", False, str(e))

    npass = sum(1 for _, ok, _ in res if ok)
    nfail = len(res) - npass
    print(f"\n{'='*60}\n  {npass} passed, {nfail} failed")
    for n, ok, d in res:
        if not ok:
            print(f"    FAIL {n}: {d}")
    if nfail:
        print("\n  ⚠️  One or more checks FAILED — review before leaving prod on the canonical layer.")
        print("     Rollback (plan Phase E): restore the 5 Railway vars to RECRUITMENT_TEST/PUBLIC + redeploy.")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
