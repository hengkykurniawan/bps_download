#!/usr/bin/env python3
"""
BPS publication PDF downloader.

Downloads all publication PDFs for one or more BPS "subject" ids (the same
`subject=` number used on https://www.bps.go.id/id/statistics-table?subject=NNN ,
which maps to the API field `id_subject_csa`).

Files are saved to:  <out>/<subject_id> - <subject name>/<title> [<pubid>].pdf
Re-running skips files already on disk (resumable). A manifest.csv is written
per subject folder.

Usage examples (from this folder):
    python bps_download.py 530
    python bps_download.py 530 531 557
    python bps_download.py 530 --list            # list only, no download
    python bps_download.py 530 --limit 5         # download first 5 (test)
    python bps_download.py 530 --domain 3500     # regional domain (e.g. Jawa Timur)

API key is read from the file `.bps_key` next to this script (or pass --key).
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error

BASE = "https://webapi.bps.go.id/v1/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))


def load_key(cli_key):
    if cli_key:
        return cli_key.strip()
    path = os.path.join(SCRIPT_DIR, ".bps_key")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    sys.exit("No API key. Put it in .bps_key next to this script or pass --key.")


def http_json(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"GET failed after {retries} tries: {url}\n  {last}")


def load_ignore():
    """pub_ids the BPS server can't serve; skipped on every run."""
    path = os.path.join(SCRIPT_DIR, "ignore_pubids.txt")
    ids = set()
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                tok = line.split("#", 1)[0].strip()
                if tok:
                    ids.add(tok)
    return ids


def sanitize(name, maxlen=120):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", str(name))
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:maxlen].strip() or "untitled"


def subject_listing(subject, domain, lang, perpage, key):
    """Yield every publication record for a subject, across all pages."""
    page = 1
    name = None
    while True:
        url = (f"{BASE}/list/model/publication/lang/{lang}/domain/{domain}"
               f"/subjectcsa/{subject}/perpage/{perpage}/page/{page}/key/{key}/")
        d = http_json(url)
        data = d.get("data")
        if not (isinstance(data, list) and len(data) > 1 and data[1]):
            break
        meta, rows = data[0], data[1]
        if name is None:
            csa = rows[0].get("subject_csa")
            name = csa[0] if isinstance(csa, list) and csa else (csa or f"subject-{subject}")
        for rec in rows:
            yield meta, name, rec
        if page >= meta.get("pages", 1):
            break
        page += 1


def download(url, dest, retries=4):
    tmp = dest + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as out:
                first = r.read(5)
                if first[:4] != b"%PDF":
                    raise ValueError(f"not a PDF (starts with {first!r})")
                out.write(first)
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(tmp, dest)
            return True
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == retries - 1:
                print(f"      FAILED: {e}")
                return False
            time.sleep(3 * (attempt + 1))
    return False


def run_subject(subject, args, key):
    print(f"\n=== subject {subject} ===")
    records = []
    subject_name = None
    for meta, name, rec in subject_listing(subject, args.domain, args.lang, args.perpage, key):
        subject_name = name
        records.append(rec)
        if args.limit and len(records) >= args.limit:
            break

    if not records:
        print("  no publications found (check the subject id / domain).")
        return

    total = records[0] if False else len(records)
    folder = os.path.join(args.out, sanitize(f"{subject} - {subject_name}"))
    os.makedirs(folder, exist_ok=True)
    print(f"  theme  : {subject_name}")
    print(f"  found  : {len(records)} publications")
    print(f"  folder : {folder}")

    if args.list:
        for i, rec in enumerate(records, 1):
            print(f"   {i:3}. {rec.get('title')}  ({rec.get('size','?')})")
        return

    manifest_path = os.path.join(folder, "manifest.csv")
    ignore = load_ignore()
    new_rows = []
    ok = skip = fail = nopdf = ignored = 0
    for i, rec in enumerate(records, 1):
        title = rec.get("title") or rec.get("pub_id")
        pdf = rec.get("pdf")
        short = (rec.get("pub_id") or "")[:8]
        fname = sanitize(f"{title} [{short}]") + ".pdf"
        dest = os.path.join(folder, fname)
        status = ""
        if rec.get("pub_id") in ignore and not (os.path.exists(dest) and os.path.getsize(dest) > 1000):
            ignored += 1
            status = "ignored"
        elif not pdf:
            print(f"  [{i}/{len(records)}] (no PDF) {title}")
            nopdf += 1
            status = "no-pdf"
        elif os.path.exists(dest) and os.path.getsize(dest) > 1000:
            skip += 1
            status = "skipped"
        else:
            print(f"  [{i}/{len(records)}] {title}  ({rec.get('size','?')})")
            if download(pdf, dest):
                ok += 1
                status = "downloaded"
            else:
                fail += 1
                status = "failed"
            time.sleep(args.delay)
        new_rows.append({
            "pub_id": rec.get("pub_id"), "title": title,
            "size": rec.get("size"), "rl_date": rec.get("rl_date"),
            "file": fname if pdf else "", "status": status, "pdf_url": pdf or "",
        })

    with open(manifest_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(new_rows[0].keys()))
        w.writeheader()
        w.writerows(new_rows)

    print(f"  done: {ok} downloaded, {skip} already present, {ignored} ignored, "
          f"{nopdf} without PDF, {fail} failed. manifest -> {manifest_path}")


def main():
    ap = argparse.ArgumentParser(description="Download BPS publication PDFs by subject id.")
    ap.add_argument("subjects", nargs="+", help="subject id(s), e.g. 530 531")
    ap.add_argument("--domain", default="0000", help="BPS domain code (default 0000 = national)")
    ap.add_argument("--lang", default="ind", help="ind or eng (default ind)")
    ap.add_argument("--out", default=SCRIPT_DIR, help="output base folder (default: this folder)")
    ap.add_argument("--perpage", type=int, default=50, help="API page size (default 50)")
    ap.add_argument("--limit", type=int, default=0, help="only first N pubs (testing)")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between downloads")
    ap.add_argument("--list", action="store_true", help="list publications, do not download")
    ap.add_argument("--key", help="API key (else read from .bps_key)")
    args = ap.parse_args()

    key = load_key(args.key)
    for s in args.subjects:
        run_subject(s.strip(), args, key)


if __name__ == "__main__":
    main()
