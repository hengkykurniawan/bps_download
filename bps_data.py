#!/usr/bin/env python3
"""
BPS Dynamic Data -> CSV exporter.

Pulls the actual statistics (the numbers behind BPS "Tabel Dinamis") and writes
a tidy, one-row-per-value CSV -- no PDF-to-table conversion needed.

Three modes:

  # 0) list every website subject id (the subject=NNN on the site)
  python bps_data.py subjects
  python bps_data.py subjects --csv subjects.csv

  # 1) discover variables under a website subject id (the subject=NNN on the site)
  python bps_data.py vars --subject 531
  python bps_data.py vars --subject 531 --csv vars_531.csv

  # 2) discover which years/periods a variable has
  python bps_data.py years --var 2776

  # 3) export data to CSV
  python bps_data.py get --var 2776 --th 126
  python bps_data.py get --var 2776 --th all          # every available year
  python bps_data.py get --var 2776 --th 126 --gzip   # compact .csv.gz

CSV columns (long/tidy format):
  var_id, variable, unit, vervar_id, vervar, turvar_id, turvar,
  year_id, year, period_id, period, value

  vervar = the row entity (often region/kab-kota), turvar = sub-category,
  period = sub-year (e.g. quarter/month; blank/0 when the series is annual).

API key is read from `.bps_key` next to this script (or pass --key).
"""

import argparse
import csv
import gzip
import itertools
import json
import os
import sys
import time
import urllib.request

BASE = "https://webapi.bps.go.id/v1/api/list"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_key(cli_key):
    if cli_key:
        return cli_key.strip()
    path = os.path.join(SCRIPT_DIR, ".bps_key")
    if os.path.exists(path):
        return open(path).read().strip()
    sys.exit("No API key: put it in .bps_key next to this script or pass --key.")


def http_json(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed: {url}\n  {last}")


def paginate(model, filt, domain, lang, key):
    """Yield every row from a paginated list endpoint."""
    page = 1
    seg = f"{filt}/" if filt else ""
    while True:
        url = (f"{BASE}/model/{model}/lang/{lang}/domain/{domain}/{seg}"
               f"page/{page}/key/{key}/")
        d = http_json(url)
        data = d.get("data")
        if not (isinstance(data, list) and len(data) > 1 and data[1]):
            break
        meta, rows = data[0], data[1]
        for row in rows:
            yield row
        if page >= meta.get("pages", 1):
            break
        page += 1


# ---------------------------------------------------------------- discovery

def cmd_subjects(args, key):
    rows = list(paginate("subjectcsa", "", args.domain, args.lang, key))
    if not rows:
        print("No subjects found.")
        return
    from collections import defaultdict
    groups = defaultdict(list)
    for r in rows:
        groups[r.get("subcat") or "(uncategorized)"].append(r)
    print(f"{len(rows)} subjects (use the number as subject= / --subject):\n")
    for cat in sorted(groups):
        print(f"## {cat}")
        for r in sorted(groups[cat], key=lambda x: x["sub_id"]):
            print(f"   subject={r['sub_id']:<4} {r['title']}")
        print()
    if args.csv:
        path = args.csv if os.path.isabs(args.csv) else os.path.join(args.out, args.csv)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=["sub_id", "title", "subcat_id", "subcat"])
            w.writeheader()
            for r in rows:
                w.writerow({k: r.get(k) for k in ["sub_id", "title", "subcat_id", "subcat"]})
        print(f"-> wrote {len(rows)} rows to {path}")


def cmd_vars(args, key):
    rows = list(paginate("var", f"subjectcsa/{args.subject}", args.domain, args.lang, key))
    if not rows:
        print(f"No variables found for subject {args.subject}.")
        return
    print(f"{len(rows)} variables under subject {args.subject}:\n")
    out = []
    for r in rows:
        vid, title = r.get("var_id"), r.get("title")
        unit = r.get("unit") or ""
        out.append({"var_id": vid, "title": title, "unit": unit,
                    "subcsa_id": r.get("subcsa_id"), "subcsa_name": r.get("subcsa_name")})
        if not args.csv:
            print(f"  var={vid:<6} {title}  [{unit}]")
    if args.csv:
        path = args.csv if os.path.isabs(args.csv) else os.path.join(args.out, args.csv)
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(out[0].keys()))
            w.writeheader(); w.writerows(out)
        print(f"  -> wrote {len(out)} rows to {path}")


def years_for_var(var, domain, lang, key):
    rows = list(paginate("th", f"var/{var}", domain, lang, key))
    return rows  # each: {'th_id':.., 'th':..}


def cmd_years(args, key):
    rows = years_for_var(args.var, args.domain, args.lang, key)
    if not rows:
        print(f"No years found for var {args.var}.")
        return
    print(f"{len(rows)} periods available for var {args.var}:")
    for r in rows:
        print(f"  th={r.get('th_id'):<5} {r.get('th')}")


# ---------------------------------------------------------------- export

def fetch_data(var, th, domain, lang, key):
    url = (f"{BASE}/model/data/lang/{lang}/domain/{domain}"
           f"/var/{var}/th/{th}/key/{key}/")
    return http_json(url)


def decode_rows(d):
    """Reconstruct every cell by building keys from the dimension lists.
    key = [vervar][var][turvar][tahun][turtahun] -> value in datacontent."""
    if d.get("data-availability") != "available":
        return [], d.get("data-availability")
    var = d["var"][0]
    var_id = str(var["val"])
    unit = var.get("unit", "")
    var_label = var.get("label", "")
    vervar = d.get("vervar", [])
    turvar = d.get("turvar", []) or [{"val": "", "label": ""}]
    tahun = d.get("tahun", [])
    turtahun = d.get("turtahun", []) or [{"val": "", "label": ""}]
    dc = d.get("datacontent", {})

    rows, matched = [], 0
    for vv, tv, ty, tt in itertools.product(vervar, turvar, tahun, turtahun):
        key = f"{vv['val']}{var_id}{tv['val']}{ty['val']}{tt['val']}"
        if key in dc:
            matched += 1
            rows.append({
                "var_id": var_id, "variable": var_label, "unit": unit,
                "vervar_id": vv["val"], "vervar": vv["label"],
                "turvar_id": tv["val"], "turvar": tv["label"],
                "year_id": ty["val"], "year": ty["label"],
                "period_id": tt["val"], "period": tt["label"],
                "value": dc[key],
            })
    return rows, (matched, len(dc))


def cmd_get(args, key):
    ths = args.th
    all_rows = []
    for var in args.var:
        targets = ths
        if ths == ["all"]:
            yr = years_for_var(var, args.domain, args.lang, key)
            targets = [str(r["th_id"]) for r in yr]
            print(f"var {var}: {len(targets)} periods -> {targets}")
        for th in targets:
            d = fetch_data(var, th, args.domain, args.lang, key)
            rows, info = decode_rows(d)
            if not rows:
                print(f"  var {var} th {th}: no data ({info})")
                continue
            matched, total = info
            note = "" if matched == total else f"  (WARNING: {matched}/{total} keys decoded)"
            print(f"  var {var} th {th}: {len(rows)} rows{note}")
            all_rows.extend(rows)

    if not all_rows:
        print("Nothing to write.")
        return

    if args.out_file:
        fname = args.out_file
    elif len(args.var) == 1 and ths != ["all"] and len(ths) == 1:
        fname = f"data_var{args.var[0]}_th{ths[0]}.csv"
    else:
        fname = f"data_var{'-'.join(args.var)}.csv"
    if args.gzip and not fname.endswith(".gz"):
        fname += ".gz"
    path = fname if os.path.isabs(fname) else os.path.join(args.out, fname)

    opener = (lambda p: gzip.open(p, "wt", newline="", encoding="utf-8-sig")) if args.gzip \
        else (lambda p: open(p, "w", newline="", encoding="utf-8-sig"))
    with opener(path) as f:
        w = csv.DictWriter(f, fieldnames=list(all_rows[0].keys()))
        w.writeheader(); w.writerows(all_rows)
    size = os.path.getsize(path)
    print(f"\nWrote {len(all_rows)} rows -> {path}  ({size/1024:.0f} KB)")


def main():
    ap = argparse.ArgumentParser(description="BPS Dynamic Data -> CSV.")
    ap.add_argument("--domain", default="0000")
    ap.add_argument("--lang", default="ind")
    ap.add_argument("--out", default=SCRIPT_DIR, help="output base folder")
    ap.add_argument("--key")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("subjects", help="list all website subject ids")
    ps.add_argument("--csv", help="also write the list to this CSV")

    pv = sub.add_parser("vars", help="list variables under a subject id")
    pv.add_argument("--subject", required=True)
    pv.add_argument("--csv", help="also write the list to this CSV")

    py = sub.add_parser("years", help="list available periods for a variable")
    py.add_argument("--var", required=True)

    pg = sub.add_parser("get", help="export data to CSV")
    pg.add_argument("--var", required=True, nargs="+", help="one or more variable ids")
    pg.add_argument("--th", required=True, nargs="+", help="year id(s), or 'all'")
    pg.add_argument("--gzip", action="store_true", help="write compact .csv.gz")
    pg.add_argument("--out-file", help="explicit output filename")

    args = ap.parse_args()
    key = load_key(args.key)
    {"subjects": cmd_subjects, "vars": cmd_vars,
     "years": cmd_years, "get": cmd_get}[args.cmd](args, key)


if __name__ == "__main__":
    main()
