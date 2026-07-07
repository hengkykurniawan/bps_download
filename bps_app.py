#!/usr/bin/env python3
"""
BPS Data Downloader -- local web app (zero install, Python stdlib only).

Run:
    python bps_app.py
Then a browser opens at http://127.0.0.1:8765

Menus:
  - Subjects        : browse all 37 website subjects (the subject= ids), grouped & searchable
  - Variables       : dynamic-data variables under a subject (search, unit shown)
  - Years & Export  : available periods for a variable; preview + export CSV (one var, many vars, all years, gzip)
  - Publications    : publication PDFs under a subject; download one or bulk-download all (resumable)
  - Static Tables   : pre-made Excel tables; search all + filter by a subject's accounts family
  - Downloads       : live progress of bulk jobs and finished files
  - Settings        : region (domain), language, page size, and the API key

Everything is served locally; the backend talks to webapi.bps.go.id with the
required headers, so the key never reaches the browser.
"""

import csv
import datetime
import gzip
import html
import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE = "https://webapi.bps.go.id/v1/api"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PAGES_ORIGIN = "https://hengkykurniawan.github.io"
HOST, PORT = "127.0.0.1", 8765
LOCAL_ORIGINS = {f"http://{HOST}:{PORT}", f"http://localhost:{PORT}"}

SETTINGS = {"domain": "0000", "lang": "ind", "perpage": 100}
JOBS = {}            # job_id -> dict(status,total,done,current,log,error,folder,kind)
JOB_LOCK = threading.Lock()
_JOB_SEQ = [0]


# ----------------------------------------------------------------- BPS API

def load_key():
    path = os.path.join(SCRIPT_DIR, ".bps_key")
    return open(path).read().strip() if os.path.exists(path) else ""


def save_key(value):
    with open(os.path.join(SCRIPT_DIR, ".bps_key"), "w") as f:
        f.write(value.strip())


def api(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"API error: {last}")


def api_list(model, filt="", domain=None, lang=None, all_pages=True):
    key = load_key()
    if not key:
        raise RuntimeError("No API key set (Settings tab).")
    domain = domain or SETTINGS["domain"]
    lang = lang or SETTINGS["lang"]
    perpage = SETTINGS.get("perpage", 100)
    seg = f"{filt}/" if filt else ""
    out, page = [], 1
    while True:
        url = (f"{BASE}/list/model/{model}/lang/{lang}/domain/{domain}/{seg}"
               f"perpage/{perpage}/page/{page}/key/{key}/")
        d = api(url)
        data = d.get("data")
        if not (isinstance(data, list) and len(data) > 1 and data[1]):
            break
        meta, rows = data[0], data[1]
        out.extend(rows)
        if not all_pages or page >= meta.get("pages", 1):
            break
        page += 1
    return out


def get_subjects():
    rows = api_list("subjectcsa")
    return [{"id": r.get("sub_id"), "title": clean(r.get("title")),
             "subcat": clean(r.get("subcat")) or "Lainnya", "ntabel": r.get("ntabel")}
            for r in rows]


def get_vars(subject):
    rows = api_list("var", f"subjectcsa/{subject}")
    return [{"var_id": r.get("var_id"), "title": clean(r.get("title")),
             "unit": clean(r.get("unit") or ""), "sub_name": r.get("sub_name") or ""}
            for r in rows]


def get_years(var):
    rows = api_list("th", f"var/{var}")
    return [{"th_id": r.get("th_id"), "th": r.get("th")} for r in rows]


def get_domains():
    key = load_key()
    d = api(f"{BASE}/domain/type/all/key/{key}/")
    data = d.get("data")
    rows = data[1] if isinstance(data, list) and len(data) > 1 else []
    return [{"id": r.get("domain_id"), "name": r.get("domain_name")} for r in rows]


def get_publications(subject):
    rows = api_list("publication", f"subjectcsa/{subject}")
    return [{"pub_id": r.get("pub_id"), "title": clean(r.get("title")),
             "size": r.get("size") or "", "rl_date": r.get("rl_date") or "",
             "pdf": r.get("pdf") or ""} for r in rows]


def get_statictables(subject=None):
    rows = api_list("statictable")
    out = [{"table_id": r.get("table_id"), "title": clean(r.get("title")),
            "subj_id": r.get("subj_id"), "subj": r.get("subj") or "",
            "size": r.get("size") or "", "excel": r.get("excel") or ""} for r in rows]
    if subject:
        # the list-level subjectcsa filter is broken; approximate by the small
        # subj_id set that the subject's variables belong to.
        sub_ids = {v["sub_name"] for v in get_vars(subject)}
        out = [t for t in out if t["subj"] in sub_ids]
    return out


def fetch_data(var, th):
    key = load_key()
    url = (f"{BASE}/list/model/data/lang/{SETTINGS['lang']}/domain/{SETTINGS['domain']}"
           f"/var/{var}/th/{th}/key/{key}/")
    return api(url)


_LU_RE = re.compile(rb'"last_update"\s*:\s*"([^"]*)"')


def fetch_last_update(var, th):
    """Read only the first few KB of the data response to grab `last_update`
    (which sits near the top), instead of downloading the whole data cube."""
    key = load_key()
    url = (f"{BASE}/list/model/data/lang/{SETTINGS['lang']}/domain/{SETTINGS['domain']}"
           f"/var/{var}/th/{th}/key/{key}/")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            buf = b""
            for _ in range(8):                      # up to ~8 KB
                chunk = r.read(1024)
                if not chunk:
                    break
                buf += chunk
                m = _LU_RE.search(buf)
                if m:
                    return m.group(1).decode("utf-8", "replace")
    except Exception:
        return None
    m = _LU_RE.search(buf)
    return m.group(1).decode("utf-8", "replace") if m else None


def decode_rows(d):
    if d.get("data-availability") != "available":
        return []
    var = d["var"][0]
    var_id = str(var["val"])
    unit, var_label = clean(var.get("unit", "")), clean(var.get("label", ""))
    vervar = d.get("vervar", [])
    turvar = d.get("turvar") or [{"val": "", "label": ""}]
    tahun = d.get("tahun", [])
    turtahun = d.get("turtahun") or [{"val": "", "label": ""}]
    dc = d.get("datacontent", {})
    rows = []
    for vv in vervar:
        for tv in turvar:
            for ty in tahun:
                for tt in turtahun:
                    k = f"{vv['val']}{var_id}{tv['val']}{ty['val']}{tt['val']}"
                    if k in dc:
                        rows.append({
                            "var_id": var_id, "variable": var_label, "unit": unit,
                            "vervar_id": vv["val"], "vervar": clean(vv["label"]),
                            "turvar_id": tv["val"], "turvar": clean(tv["label"]),
                            "year_id": ty["val"], "year": clean(ty["label"]),
                            "period_id": tt["val"], "period": clean(tt["label"]),
                            "value": dc[k],
                        })
    return rows


CSV_COLS = ["var_id", "variable", "unit", "vervar_id", "vervar", "turvar_id",
            "turvar", "year_id", "year", "period_id", "period", "value"]


_TAG_RE = re.compile(r"<[^>]+>")


def clean(s):
    """Strip HTML tags and decode entities from BPS labels/titles
    (BPS embeds markup like '<b>A. Pintu Udara</b>' and '&amp;' in values)."""
    if s is None:
        return s
    s = _TAG_RE.sub("", str(s))
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


def sanitize(name, maxlen=120):
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', " ", str(name))
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return name[:maxlen].strip() or "untitled"


def load_ignore():
    path = os.path.join(SCRIPT_DIR, "ignore_pubids.txt")
    ids = set()
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            tok = line.split("#", 1)[0].strip()
            if tok:
                ids.add(tok)
    return ids


# ----------------------------------------------------------------- update tracking

STATE_FILE = os.path.join(SCRIPT_DIR, "update_state.json")
STATE_LOCK = threading.Lock()
RECENT_DAYS = 30  # highlight items updated within this many days


def now_str():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_state():
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            try:
                return json.load(open(STATE_FILE, encoding="utf-8"))
            except Exception:
                pass
        return {"seen": {}, "var_cache": {}, "checked": {}}


def save_state(st):
    with STATE_LOCK:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)


def days_ago(ts):
    d = str(ts or "")[:10]
    if not d:
        return None
    try:
        return (datetime.date.today() - datetime.date.fromisoformat(d)).days
    except Exception:
        return None


def add_update_flags(items, kind):
    """Annotate each row with ts / days_ago / recent / changed (vs last seen)."""
    st = load_state()
    seen, cache = st.get("seen", {}), st.get("var_cache", {})
    for it in items:
        if kind == "var":
            key, ts = f"var:{it['var_id']}", cache.get(str(it["var_id"]), {}).get("ts", "")
        elif kind == "pub":
            key = f"pub:{it['pub_id']}"
            ts = max(it.get("updt_date") or "", it.get("rl_date") or "")
        else:
            key, ts = f"stab:{it['table_id']}", (it.get("updt_date") or "")
        da = days_ago(ts)
        prev = seen.get(key)
        it["ts"] = ts
        it["days_ago"] = da
        it["recent"] = bool(da is not None and 0 <= da <= RECENT_DAYS)
        it["changed"] = bool(prev and ts and ts > prev)
    return items


def new_count():
    """Instant NEW count from cached state (no network): variables + publications
    whose cached timestamp is newer than the seen baseline."""
    st = load_state()
    seen = st.get("seen", {})
    n = 0
    for vid, info in st.get("var_cache", {}).items():
        prev, ts = seen.get(f"var:{vid}"), info.get("ts")
        if prev and ts and ts > prev:
            n += 1
    for pid, info in st.get("pub_cache", {}).items():
        prev, ts = seen.get(f"pub:{pid}"), info.get("ts")
        if prev and ts and ts > prev:
            n += 1
    return {"new": n}


def whats_new():
    """Digest of recent / changed items across ALL scanned subjects, served from
    the cache (no network). Populate it with 'Scan all subjects' or --check-all."""
    st = load_state()
    seen = st.get("seen", {})
    groups = {}

    def add(subj, subjname, item):
        g = groups.setdefault(str(subj), {"id": str(subj),
                                          "title": subjname or f"subject {subj}", "items": []})
        if not g["title"] or g["title"].startswith("subject "):
            g["title"] = subjname or g["title"]
        g["items"].append(item)

    for vid, info in st.get("var_cache", {}).items():
        ts, da = info.get("ts"), days_ago(info.get("ts"))
        recent = da is not None and 0 <= da <= RECENT_DAYS
        changed = bool(seen.get(f"var:{vid}") and ts and ts > seen[f"var:{vid}"])
        if recent or changed:
            add(info.get("subj"), info.get("subjname"),
                {"kind": "var", "id": vid, "title": info.get("title"), "ts": ts,
                 "days_ago": da, "recent": recent, "changed": changed})

    for pid, info in st.get("pub_cache", {}).items():
        ts, da = info.get("ts"), days_ago(info.get("ts"))
        recent = da is not None and 0 <= da <= RECENT_DAYS
        changed = bool(seen.get(f"pub:{pid}") and ts and ts > seen[f"pub:{pid}"])
        if recent or changed:
            for subj in (info.get("subjs") or [""]):
                add(subj, info.get("subjname"),
                    {"kind": "pub", "id": pid, "title": info.get("title"),
                     "pdf": info.get("pdf", ""), "ts": ts, "days_ago": da,
                     "recent": recent, "changed": changed})

    out = []
    for g in groups.values():
        g["items"].sort(key=lambda x: (not x["changed"],
                                       x["days_ago"] if x["days_ago"] is not None else 99999))
        g["n_new"] = sum(1 for i in g["items"] if i["changed"])
        g["n_recent"] = sum(1 for i in g["items"] if i["recent"])
        out.append(g)
    out.sort(key=lambda g: (-g["n_new"], -g["n_recent"]))
    return {"generated": st.get("scanned_all") or now_str(),
            "scanned_all": st.get("scanned_all"), "subjects": out}


def mark_seen(subject):
    st = load_state()
    seen, cache = st.setdefault("seen", {}), st.get("var_cache", {})
    for v in get_vars(subject):
        ts = cache.get(str(v["var_id"]), {}).get("ts")
        if ts:
            seen[f"var:{v['var_id']}"] = ts
    for p in get_publications(subject):
        ts = max(p.get("updt_date") or "", p.get("rl_date") or "")
        if ts:
            seen[f"pub:{p['pub_id']}"] = ts
    save_state(st)


FRESH_HOURS = 6  # skip re-fetching a variable checked within this window (unless force)


def _is_stale(entry, force):
    if force or not entry or not entry.get("at"):
        return True
    if "subj" not in entry or "title" not in entry:   # old-format -> re-fetch to enrich
        return True
    try:
        at = datetime.datetime.strptime(entry["at"], "%Y-%m-%d %H:%M:%S")
        return (datetime.datetime.now() - at).total_seconds() > FRESH_HOURS * 3600
    except Exception:
        return True


def _cache_vars(var_records, force, on_progress=None):
    """var_records: dicts with var_id, title, unit, subj, subjname.
    Fetch last_update for the stale ones (parallel) and update var_cache."""
    st = load_state()
    vc, seen = st.setdefault("var_cache", {}), st.setdefault("seen", {})
    todo = [v for v in var_records if _is_stale(vc.get(str(v["var_id"])), force)]

    def fetch_one(v):
        vid = v["var_id"]
        try:
            ys = get_years(vid)
            if not ys:
                return v, None
            return v, fetch_last_update(vid, ys[-1]["th_id"])
        except Exception:
            return v, None

    changed = dated = done = 0
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = [ex.submit(fetch_one, v) for v in todo]
        for fu in as_completed(futs):
            v, ts = fu.result()
            vid = str(v["var_id"])
            if ts:
                dated += 1
                key = f"var:{vid}"
                prev = seen.get(key)
                if prev and ts > prev:
                    changed += 1
                vc[vid] = {"ts": ts, "at": now_str(), "title": v.get("title"),
                           "unit": v.get("unit"), "subj": str(v.get("subj")),
                           "subjname": v.get("subjname")}
                if key not in seen:           # first sight = baseline (no false NEW)
                    seen[key] = ts
            done += 1
            if done % 100 == 0:               # periodic save -> resumable if interrupted
                save_state(st)
            if on_progress:
                on_progress(done, len(todo))
    save_state(st)
    return {"checked": len(todo), "dated": dated, "changed": changed,
            "skipped": len(var_records) - len(todo)}


def _cache_pubs(pub_records, subj_titles):
    """Cache recent (<=90d) publications for the What's New digest."""
    st = load_state()
    pc, seen = st.setdefault("pub_cache", {}), st.setdefault("seen", {})
    kept = 0
    for p in pub_records:
        ts = (p.get("rl_date") or p.get("updt_date") or "")
        da = days_ago(ts)
        if da is None or da > 90:
            continue
        pid = p["pub_id"]
        subjs = [str(x) for x in (p.get("subjs") or [])]
        pc[pid] = {"ts": ts, "title": p.get("title"), "pdf": p.get("pdf") or "",
                   "subjs": subjs,
                   "subjname": subj_titles.get(subjs[0], subjs[0]) if subjs else ""}
        if f"pub:{pid}" not in seen:
            seen[f"pub:{pid}"] = ts
        kept += 1
    save_state(st)
    return kept


def _recent_publications(max_age_days=90, max_pages=8):
    """Paginate the full publication list (sorted newest-first) and stop once we
    pass max_age_days (or max_pages) -- so we read only a few pages, not all 59."""
    key = load_key()
    out, page = [], 1
    while page <= max_pages:
        url = (f"{BASE}/list/model/publication/lang/{SETTINGS['lang']}/domain/"
               f"{SETTINGS['domain']}/perpage/100/page/{page}/key/{key}/")
        d = api(url)
        data = d.get("data")
        if not (isinstance(data, list) and len(data) > 1 and data[1]):
            break
        meta, rows = data[0], data[1]
        passed_old = False
        for r in rows:
            da = days_ago(r.get("rl_date") or r.get("updt_date"))
            if da is not None and da > max_age_days:
                passed_old = True
                continue
            out.append({"pub_id": r.get("pub_id"), "title": r.get("title"),
                        "pdf": r.get("pdf", ""), "rl_date": r.get("rl_date"),
                        "subjs": [str(x) for x in (r.get("id_subject_csa") or [])]})
        if passed_old or page >= meta.get("pages", 1):
            break
        page += 1
    return out


def scan_all(subjects=None, force=False, on_progress=None):
    """Scan variables (and recent publications) for the given subjects, or for
    ALL subjects when subjects is None. Populates the caches used by What's New."""
    subj_titles = {str(s["id"]): s["title"] for s in get_subjects()}

    # Gather variables PER-SUBJECT: the subjectcsa filter honors perpage (~1 page
    # each), whereas the unfiltered var list ignores it (170 pages of 10). This
    # also gives each variable its subject directly.
    var_records, pub_records = [], []
    target_subjects = subjects if subjects else list(subj_titles.keys())

    def gather(sid):
        return [{"var_id": v["var_id"], "title": v["title"], "unit": v["unit"],
                 "subj": str(sid), "subjname": subj_titles.get(str(sid), str(sid))}
                for v in get_vars(sid)]

    with ThreadPoolExecutor(max_workers=8) as ex:      # parallel list fetches
        for recs in ex.map(gather, target_subjects):
            var_records.extend(recs)
    if subjects:
        for sid in subjects:
            for p in get_publications(sid):
                pub_records.append({"pub_id": p["pub_id"], "title": p["title"],
                                    "pdf": p.get("pdf", ""), "rl_date": p.get("rl_date"),
                                    "subjs": [str(sid)]})
    else:
        pub_records = _recent_publications()

    # de-dupe variables by id (a var can appear under multiple subjects)
    seen_ids, uniq = set(), []
    for v in var_records:
        if v["var_id"] not in seen_ids:
            seen_ids.add(v["var_id"])
            uniq.append(v)

    res = _cache_vars(uniq, force, on_progress)
    res["pubs"] = _cache_pubs(pub_records, subj_titles)

    st = load_state()
    st.setdefault("checked", {})
    targets = subjects if subjects else list(subj_titles.keys())
    for sid in targets:
        st["checked"][f"subject:{sid}"] = now_str()
    if not subjects:
        st["scanned_all"] = now_str()
    save_state(st)
    res["total_vars"] = len(uniq)
    return res


def check_subject(subject, on_progress=None):
    """Per-subject scan (Variables tab 'Check updates' button / CLI --check)."""
    return scan_all([str(subject)], force=False, on_progress=on_progress)


def job_check_updates(jid, subject):
    try:
        def prog(i, total):
            with JOB_LOCK:
                JOBS[jid]["total"] = total
                JOBS[jid]["done"] = i
                JOBS[jid]["current"] = f"checked {i}/{total}"
        r = check_subject(subject, prog)
        jlog(jid, f"done: {r['dated']} dated, {r['changed']} changed since last seen")
        with JOB_LOCK:
            JOBS[jid]["status"] = "done"
            JOBS[jid]["current"] = ""
    except Exception as e:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)


def job_scan_all(jid, force):
    try:
        with JOB_LOCK:
            JOBS[jid]["current"] = "fetching variable & publication lists…"

        def prog(i, total):
            with JOB_LOCK:
                JOBS[jid]["total"] = total
                JOBS[jid]["done"] = i
                JOBS[jid]["current"] = f"fetching last-update {i}/{total} variables"
        r = scan_all(None, force=force, on_progress=prog)
        jlog(jid, f"scanned all subjects: {r['total_vars']} variables "
                  f"({r['checked']} fetched, {r['skipped']} cached), {r['pubs']} recent pubs, "
                  f"{r['changed']} changed since last seen")
        with JOB_LOCK:
            JOBS[jid]["status"] = "done"
            JOBS[jid]["current"] = ""
    except Exception as e:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)


# ----------------------------------------------------------------- jobs

def new_job(kind, total, folder=""):
    with JOB_LOCK:
        _JOB_SEQ[0] += 1
        jid = f"job{_JOB_SEQ[0]}"
        JOBS[jid] = {"id": jid, "kind": kind, "status": "running", "total": total,
                     "done": 0, "current": "", "log": [], "error": "", "folder": folder}
    return jid


def jlog(jid, msg):
    with JOB_LOCK:
        JOBS[jid]["log"].append(msg)
        JOBS[jid]["log"] = JOBS[jid]["log"][-200:]


def download_pdf(url, dest, retries=3):
    tmp = dest + ".part"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA,
                                                       "Referer": "https://www.bps.go.id/"})
            with urllib.request.urlopen(req, timeout=300) as r, open(tmp, "wb") as out:
                first = r.read(5)
                if first[:4] != b"%PDF":
                    raise ValueError("server did not return a PDF")
                out.write(first)
                while True:
                    chunk = r.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
            os.replace(tmp, dest)
            return True, ""
        except Exception as e:
            if os.path.exists(tmp):
                os.remove(tmp)
            if attempt == retries - 1:
                return False, str(e)
            time.sleep(2 * (attempt + 1))
    return False, "failed"


def job_download_pubs(jid, subject, subject_title):
    try:
        pubs = get_publications(subject)
        ignore = load_ignore()
        folder = os.path.join(SCRIPT_DIR, sanitize(f"{subject} - {subject_title}"))
        os.makedirs(folder, exist_ok=True)
        with JOB_LOCK:
            JOBS[jid]["total"] = len(pubs)
            JOBS[jid]["folder"] = folder
        for i, p in enumerate(pubs, 1):
            title, pid, pdf = p["title"], p["pub_id"], p["pdf"]
            with JOB_LOCK:
                JOBS[jid]["current"] = title
            fname = sanitize(f"{title} [{pid[:8]}]") + ".pdf"
            dest = os.path.join(folder, fname)
            if pid in ignore:
                jlog(jid, f"ignored: {title}")
            elif os.path.exists(dest) and os.path.getsize(dest) > 1000:
                jlog(jid, f"skip (exists): {title}")
            elif not pdf:
                jlog(jid, f"no PDF: {title}")
            else:
                ok, err = download_pdf(pdf, dest)
                jlog(jid, (f"ok: {title}" if ok else f"FAIL: {title} ({err})"))
            with JOB_LOCK:
                JOBS[jid]["done"] = i
        with JOB_LOCK:
            JOBS[jid]["status"] = "done"
            JOBS[jid]["current"] = ""
    except Exception as e:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)


def job_save_csv(jid, varlist, thmode, ths, gzip_out, fname):
    try:
        all_rows = []
        targets = []
        for v in varlist:
            if thmode == "all":
                targets += [(v, str(y["th_id"])) for y in get_years(v)]
            else:
                targets += [(v, t) for t in ths]
        with JOB_LOCK:
            JOBS[jid]["total"] = len(targets)
        for i, (v, t) in enumerate(targets, 1):
            with JOB_LOCK:
                JOBS[jid]["current"] = f"var {v} / th {t}"
            rows = decode_rows(fetch_data(v, t))
            all_rows.extend(rows)
            jlog(jid, f"var {v} th {t}: {len(rows)} rows")
            with JOB_LOCK:
                JOBS[jid]["done"] = i
        if not fname:
            fname = (f"data_var{varlist[0]}.csv" if len(varlist) == 1
                     else f"data_var{'-'.join(varlist)}.csv")
        if gzip_out and not fname.endswith(".gz"):
            fname += ".gz"
        path = os.path.join(SCRIPT_DIR, sanitize(fname, 150))
        opener = ((lambda p: gzip.open(p, "wt", newline="", encoding="utf-8-sig"))
                  if gzip_out else (lambda p: open(p, "w", newline="", encoding="utf-8-sig")))
        with opener(path) as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLS)
            w.writeheader()
            w.writerows(all_rows)
        jlog(jid, f"wrote {len(all_rows)} rows -> {path}")
        with JOB_LOCK:
            JOBS[jid]["status"] = "done"
            JOBS[jid]["folder"] = path
            JOBS[jid]["current"] = ""
    except Exception as e:
        with JOB_LOCK:
            JOBS[jid]["status"] = "error"
            JOBS[jid]["error"] = str(e)


# ----------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        origin = self.headers.get("Origin")
        if origin == PAGES_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        return not origin or origin == PAGES_ORIGIN or origin in LOCAL_ORIGINS

    def do_OPTIONS(self):
        """Allow the GitHub Pages UI to call this loopback-only service."""
        if not self._origin_allowed():
            return self._send(403, {"error": "origin not allowed"})
        self._send(204, b"")

    def _q(self):
        return urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

    def _body(self):
        n = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        q = self._q()
        if not self._origin_allowed():
            return self._send(403, {"error": "origin not allowed"})
        try:
            if path == "/":
                page_path = os.path.join(SCRIPT_DIR, "docs", "index.html")
                page = PAGE
                if os.path.exists(page_path):
                    with open(page_path, encoding="utf-8") as f:
                        page = f.read()
                return self._send(200, page, "text/html; charset=utf-8")
            if path == "/api/settings":
                k = load_key()
                return self._send(200, {**SETTINGS, "key_set": bool(k),
                                        "key_masked": (k[:4] + "…" + k[-4:]) if k else ""})
            if path == "/api/subjects":
                return self._send(200, get_subjects())
            if path == "/api/domains":
                return self._send(200, get_domains())
            if path == "/api/vars":
                return self._send(200, add_update_flags(get_vars(q["subject"][0]), "var"))
            if path == "/api/years":
                return self._send(200, get_years(q["var"][0]))
            if path == "/api/publications":
                return self._send(200, add_update_flags(get_publications(q["subject"][0]), "pub"))
            if path == "/api/statictables":
                items = get_statictables(q.get("subject", [None])[0])
                return self._send(200, add_update_flags(items, "stab"))
            if path == "/api/update_meta":
                st = load_state()
                subj = q["subject"][0]
                checked = st.get("checked", {}).get(f"subject:{subj}")
                return self._send(200, {"checked": checked, "recent_days": RECENT_DAYS})
            if path == "/api/whatsnew":
                return self._send(200, whats_new())
            if path == "/api/newcount":
                return self._send(200, new_count())
            if path == "/api/preview":
                rows = decode_rows(fetch_data(q["var"][0], q["th"][0]))
                return self._send(200, {"total": len(rows), "cols": CSV_COLS,
                                        "rows": rows[:200]})
            if path == "/api/data.csv":
                rows = []
                for t in q["th"]:
                    rows += decode_rows(fetch_data(q["var"][0], t))
                buf = io.StringIO()
                w = csv.DictWriter(buf, fieldnames=CSV_COLS)
                w.writeheader()
                w.writerows(rows)
                fn = f"data_var{q['var'][0]}.csv"
                return self._send(200, "﻿" + buf.getvalue(), "text/csv; charset=utf-8",
                                  {"Content-Disposition": f'attachment; filename="{fn}"'})
            if path == "/api/jobs":
                with JOB_LOCK:
                    return self._send(200, list(JOBS.values())[::-1])
            if path == "/api/open_folder":
                try:
                    os.startfile(SCRIPT_DIR)  # noqa (Windows)
                except Exception:
                    pass
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        if not self._origin_allowed():
            return self._send(403, {"error": "origin not allowed"})
        try:
            b = self._body()
            if path == "/api/settings":
                for k in ("domain", "lang", "perpage"):
                    if k in b and b[k] != "":
                        SETTINGS[k] = b[k]
                if b.get("key"):
                    save_key(b["key"])
                return self._send(200, {"ok": True})
            if path == "/api/download_pubs":
                jid = new_job("pdf", 0)
                threading.Thread(target=job_download_pubs,
                                 args=(jid, b["subject"], b.get("subject_title", "")),
                                 daemon=True).start()
                return self._send(200, {"job": jid})
            if path == "/api/save_csv":
                jid = new_job("csv", 0)
                threading.Thread(target=job_save_csv,
                                 args=(jid, b["vars"], b.get("thmode", "list"),
                                       b.get("ths", []), bool(b.get("gzip")),
                                       b.get("fname", "")), daemon=True).start()
                return self._send(200, {"job": jid})
            if path == "/api/check_updates":
                jid = new_job("check", 0)
                threading.Thread(target=job_check_updates,
                                 args=(jid, b["subject"]), daemon=True).start()
                return self._send(200, {"job": jid})
            if path == "/api/scan_all":
                jid = new_job("scan-all", 0)
                threading.Thread(target=job_scan_all,
                                 args=(jid, bool(b.get("force"))), daemon=True).start()
                return self._send(200, {"job": jid})
            if path == "/api/mark_seen":
                mark_seen(b["subject"])
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})


PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BPS Data Downloader</title>
<style>
:root{--bg:#0f172a;--card:#1e293b;--mut:#94a3b8;--bd:#334155;--ac:#38bdf8;--ok:#34d399;--bad:#f87171}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 system-ui,Segoe UI,Roboto,sans-serif;background:#0b1220;color:#e2e8f0}
header{background:var(--bg);padding:14px 20px;border-bottom:1px solid var(--bd);position:sticky;top:0;z-index:5}
h1{margin:0;font-size:18px}.sub{color:var(--mut);font-size:12px}
.state{margin-top:8px;font-size:12px;color:var(--mut)}.state b{color:var(--ac)}
nav{display:flex;gap:4px;flex-wrap:wrap;padding:10px 20px;background:var(--bg);border-bottom:1px solid var(--bd);position:sticky;top:60px;z-index:4}
nav button{background:transparent;border:1px solid var(--bd);color:var(--mut);padding:7px 13px;border-radius:8px;cursor:pointer;font-size:13px}
nav button.on{background:var(--ac);color:#04293b;border-color:var(--ac);font-weight:600}
.navbadge{background:#ef4444;color:#fff;border-radius:99px;padding:0 7px;font-size:11px;font-weight:700;margin-left:5px;vertical-align:middle;animation:pulse 1.6s infinite}
main{padding:20px;max-width:1100px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:16px;margin-bottom:16px}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
input,select{background:#0b1220;border:1px solid var(--bd);color:#e2e8f0;padding:8px 10px;border-radius:8px;font-size:13px}
input[type=text]{min-width:240px}
button.act{background:var(--ac);color:#04293b;border:none;padding:8px 14px;border-radius:8px;font-weight:600;cursor:pointer}
button.gho{background:transparent;border:1px solid var(--bd);color:#e2e8f0;padding:7px 12px;border-radius:8px;cursor:pointer}
table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--bd);vertical-align:top}
th{color:var(--mut);font-weight:600;position:sticky;top:0;background:var(--card)}
tr.clk:hover{background:#243044;cursor:pointer}
.pill{display:inline-block;background:#0b1220;border:1px solid var(--bd);border-radius:99px;padding:1px 8px;font-size:11px;color:var(--mut)}
.muted{color:var(--mut)}.h2{font-size:15px;margin:0 0 10px}
.cat{margin:14px 0 6px;color:var(--ac);font-weight:600;font-size:13px}
.scroll{max-height:62vh;overflow:auto;border:1px solid var(--bd);border-radius:10px}
.bar{height:8px;background:#0b1220;border-radius:6px;overflow:hidden;border:1px solid var(--bd)}
.bar>i{display:block;height:100%;background:var(--ac);width:0%}
.tag{font-size:11px;padding:1px 7px;border-radius:6px}.tag.ok{background:#064e3b;color:var(--ok)}.tag.run{background:#0c4a6e;color:var(--ac)}.tag.err{background:#7f1d1d;color:var(--bad)}
.upd{font-size:11px;padding:1px 7px;border-radius:6px;margin-left:7px;font-weight:700;white-space:nowrap}
.upd.new{background:#f59e0b;color:#1f2937;box-shadow:0 0 0 2px rgba(245,158,11,.35)}
.upd.fresh{background:#22c55e;color:#04210f}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.55}}.upd.new{animation:pulse 1.6s infinite}
a{color:var(--ac)}.hide{display:none}
.note{font-size:12px;color:var(--mut);margin-top:6px}
</style></head><body>
<header><h1>📊 BPS Data Downloader <span class="sub">webapi.bps.go.id · local app</span></h1>
<div class="state" id="state">No subject selected</div></header>
<nav id="nav"></nav>
<main>
<div id="v-whatsnew" class="view hide"></div>
<div id="v-subjects" class="view"></div>
<div id="v-vars" class="view hide"></div>
<div id="v-years" class="view hide"></div>
<div id="v-pubs" class="view hide"></div>
<div id="v-static" class="view hide"></div>
<div id="v-downloads" class="view hide"></div>
<div id="v-settings" class="view hide"></div>
</main>
<script>
const S={subject:null,subjectTitle:null,var:null,varTitle:null,subjects:[],vars:[],newCount:0};
const $=s=>document.querySelector(s);
const esc=s=>(s==null?'':String(s)).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function jget(u){const r=await fetch(u);if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json();}
async function jpost(u,b){const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});if(!r.ok)throw new Error((await r.json()).error||r.status);return r.json();}
const TABS=[['whatsnew','🔔 What\'s New'],['subjects','Subjects'],['vars','Variables'],['years','Years & Export'],['pubs','Publications'],['static','Static Tables'],['downloads','Downloads'],['settings','Settings']];
let cur='subjects';
function buildNav(){$('#nav').innerHTML=TABS.map(([k,l])=>{
  const bdg=(k==='whatsnew'&&S.newCount>0)?` <span class="navbadge">${S.newCount}</span>`:'';
  return `<button data-k="${k}" class="${k==cur?'on':''}">${l}${bdg}</button>`;}).join('');
 document.querySelectorAll('#nav button').forEach(b=>b.onclick=()=>show(b.dataset.k));}
async function refreshNewCount(){try{const d=await jget('/api/newcount');S.newCount=d.new;buildNav();}catch(e){}}
function show(k){cur=k;document.querySelectorAll('.view').forEach(v=>v.classList.add('hide'));$('#v-'+k).classList.remove('hide');
 buildNav();({whatsnew:loadWhatsNew,subjects:loadSubjects,vars:loadVars,years:loadYears,pubs:loadPubs,static:loadStatic,downloads:loadDownloads,settings:loadSettings}[k])();}
function stateBar(){$('#state').innerHTML=S.subject?`Subject <b>${S.subject}</b> — ${esc(S.subjectTitle)}`+(S.var?` &nbsp;|&nbsp; Variable <b>${S.var}</b> — ${esc(S.varTitle)}`:''):'No subject selected';}
function badge(it){if(!it)return'';if(it.changed)return `<span class="upd new" title="updated ${esc(it.ts)} — new since you last checked">● NEW</span>`;
 if(it.recent)return `<span class="upd fresh" title="updated ${esc(it.ts)}">↻ ${it.days_ago}d</span>`;return'';}
function updStatus(t){const el=document.getElementById('updstat');if(el)el.textContent=t;}
let checking=false;
async function startCheck(){if(checking||!S.subject)return;checking=true;updStatus('Checking updates… (fetching last-update of each variable)');
 try{const r=await jpost('/api/check_updates',{subject:S.subject});
  const poll=setInterval(async()=>{let jobs;try{jobs=await jget('/api/jobs');}catch(e){return;}
   const j=jobs.find(x=>x.id==r.job);
   if(j&&j.status!=='running'){clearInterval(poll);checking=false;refreshNewCount();if(cur==='vars')loadVars();}
   else if(j){updStatus(`Checking updates… ${j.done}/${j.total}`);}},1200);
 }catch(e){checking=false;updStatus('Check failed: '+e.message);}}

// ---- What's New (digest across ALL scanned subjects)
let scanning=false;
async function startScanAll(force){if(scanning)return;scanning=true;
 const ss=document.getElementById('scanstat');if(ss)ss.textContent='Scanning all subjects… starting';
 try{const r=await jpost('/api/scan_all',{force:!!force});
  const poll=setInterval(async()=>{let jobs;try{jobs=await jget('/api/jobs');}catch(e){return;}
   const j=jobs.find(x=>x.id==r.job);
   if(j&&j.status!=='running'){clearInterval(poll);scanning=false;refreshNewCount();if(cur==='whatsnew')loadWhatsNew();}
   else if(j){const b=document.getElementById('scanstat');if(b)b.textContent=`Scanning… ${j.done}/${j.total} — ${j.current||''}`;}
  },1500);
 }catch(e){scanning=false;alert('Scan failed: '+e.message);}}
async function loadWhatsNew(){const el=$('#v-whatsnew');
 if(!scanning)el.innerHTML='<div class="card">Loading what\'s new…</div>';
 let d;try{d=await jget('/api/whatsnew');}catch(e){el.innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';return;}
 const tn=d.subjects.reduce((a,s)=>a+s.n_new,0),tr=d.subjects.reduce((a,s)=>a+s.n_recent,0);
 const scanTxt=d.scanned_all?('all subjects scanned '+esc(d.scanned_all)):'full scan not run yet';
 let h=`<div class="card"><div class="row"><div class="h2" style="margin:0">🔔 What's New</div>
    <button class="act" id="bscan">🔄 Scan all subjects</button>
    <button class="gho" id="bref">Refresh view</button>
    <span id="scanstat" class="muted">${scanning?'Scanning…':scanTxt}</span></div>
   <div class="note"><span class="upd new">● ${tn} NEW</span> &nbsp; <span class="upd fresh">↻ ${tr} updated ≤30d</span> &nbsp; across ${d.subjects.length} subject(s) — variables + publications</div>`;
 if(!d.scanned_all)h+=`<div class="note" style="color:#fbbf24">⚠ The full scan hasn't run, so this only covers subjects you've opened. Click <b>Scan all subjects</b> (a few minutes) to track every subject — runs in the background; watch progress in the Downloads tab.</div>`;
 h+='</div>';
 if(!d.subjects.length)h+='<div class="card muted">Nothing updated in the last 30 days. Run a scan to populate.</div>';
 d.subjects.forEach(s=>{
   h+=`<div class="cat">subject ${s.id} — ${esc(s.title)} <span class="muted">· ${s.n_new} new · ${s.n_recent} recent</span></div>`;
   h+='<div class="card" style="padding:6px"><table><tbody>';
   s.items.slice(0,40).forEach(it=>{h+=`<tr class="clk" data-k="${it.kind}" data-s="${s.id}" data-st="${esc(s.title)}" data-i="${esc(it.id)}" data-t="${esc(it.title)}" data-pdf="${esc(it.pdf||'')}"><td style="width:54px"><span class="pill">${it.kind}</span></td><td>${esc(it.title)}${badge(it)}</td><td style="width:150px" class="muted">${esc(it.ts)}</td></tr>`;});
   if(s.items.length>40)h+=`<tr><td colspan="3" class="muted">…and ${s.items.length-40} more</td></tr>`;
   h+='</tbody></table></div>';});
 S.newCount=tn;buildNav();
 el.innerHTML=h;
 $('#bscan').onclick=()=>startScanAll(false);
 $('#bref').onclick=loadWhatsNew;
 el.querySelectorAll('tr.clk').forEach(r=>r.onclick=()=>{
   if(r.dataset.k==='pub'){if(r.dataset.pdf)window.open(r.dataset.pdf,'_blank');return;}
   S.subject=r.dataset.s;S.subjectTitle=r.dataset.st;S.var=r.dataset.i;S.varTitle=r.dataset.t;S.vars=[];stateBar();show('years');});}

// ---- Subjects (search input rendered ONCE; only the list updates on keystroke)
async function loadSubjects(){const el=$('#v-subjects');
 if(!S.subjects.length){el.innerHTML='<div class="card">Loading subjects…</div>';
  try{S.subjects=await jget('/api/subjects');}catch(e){el.innerHTML='<div class="card">Error: '+esc(e.message)+' — check the Settings tab (API key).</div>';return;}}
 el.innerHTML=`<div class="card"><div class="row"><input type="text" id="sq" placeholder="Search subjects…"><span class="muted" id="scount"></span></div></div><div id="subjlist"></div>`;
 $('#sq').oninput=e=>renderSubjectRows(e.target.value);
 renderSubjectRows('');$('#sq').focus();}
function renderSubjectRows(f){const q=(f||'').toLowerCase();
 const items=S.subjects.filter(s=>!q||(s.title||'').toLowerCase().includes(q)||String(s.id).includes(q));
 $('#scount').textContent=items.length+' of '+S.subjects.length;
 const cats={};items.forEach(s=>{(cats[s.subcat]=cats[s.subcat]||[]).push(s);});
 let h='';
 for(const c of Object.keys(cats).sort()){h+=`<div class="cat">${esc(c)}</div><div class="card" style="padding:6px"><table><tbody>`;
  cats[c].sort((a,b)=>a.id-b.id).forEach(s=>{h+=`<tr class="clk" data-id="${s.id}" data-t="${esc(s.title)}"><td style="width:80px"><span class="pill">${s.id}</span></td><td>${esc(s.title)}</td><td style="width:120px" class="muted">${s.ntabel!=null?s.ntabel+' tabel':''}</td></tr>`;});
  h+='</tbody></table></div>';}
 $('#subjlist').innerHTML=h;
 $('#subjlist').querySelectorAll('tr.clk').forEach(r=>r.onclick=()=>{S.subject=r.dataset.id;S.subjectTitle=r.dataset.t;S.var=null;S.vars=[];stateBar();show('vars');});}

// ---- Variables (search input rendered ONCE; rows sorted by most-recent update)
async function loadVars(){const el=$('#v-vars');if(!S.subject){el.innerHTML='<div class="card">Pick a subject first (Subjects tab).</div>';return;}
 el.innerHTML='<div class="card">Loading variables…</div>';
 try{S.vars=await jget('/api/vars?subject='+S.subject);}catch(e){el.innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';return;}
 try{S.meta=await jget('/api/update_meta?subject='+S.subject);}catch(e){S.meta={checked:null,recent_days:30};}
 // sort by most-recent update (last_update desc); undated entries last, then by var id
 S.vars.sort((a,b)=>{const ta=a.ts||'',tb=b.ts||'';if(ta&&tb)return tb.localeCompare(ta);if(ta)return -1;if(tb)return 1;return a.var_id-b.var_id;});
 const nNew=S.vars.filter(v=>v.changed).length,nFresh=S.vars.filter(v=>v.recent).length,rd=(S.meta&&S.meta.recent_days)||30;
 el.innerHTML=`<div class="card"><div class="row" style="justify-content:space-between;align-items:flex-start">
    <div class="h2" style="margin:0">Variables · subject ${S.subject} — ${esc(S.subjectTitle)}</div>
    <button class="act" id="bdall">⬇ Download all ${S.vars.length} variables (CSV)</button></div>
  <div class="row" style="margin-top:8px"><input type="text" id="vq" placeholder="Search variables…"><span class="muted" id="vcount"></span></div>
  <div class="row" style="margin-top:10px">
   <button class="gho" id="bchk">🔄 Check updates</button>
   <button class="gho" id="bseen">✓ Mark all seen</button>
   <span id="updstat" class="muted">${S.meta&&S.meta.checked?('last checked '+esc(S.meta.checked)):'not checked yet'}</span></div>
  <div class="note">Sorted by most-recent update (newest first). <span class="upd fresh">↻ Nd</span> updated within ${rd} days (${nFresh}) &nbsp; <span class="upd new">● NEW</span> changed since you last marked seen (${nNew})</div></div>
  <div class="scroll"><table><thead><tr><th style="width:70px">var</th><th>Title</th><th style="width:155px">Last update</th><th style="width:120px">Unit</th></tr></thead><tbody id="vtb"></tbody></table></div>`;
 $('#vq').oninput=e=>renderVarRows(e.target.value);
 $('#bchk').onclick=()=>startCheck();
 $('#bseen').onclick=async()=>{try{await jpost('/api/mark_seen',{subject:S.subject});await refreshNewCount();loadVars();}catch(e){alert('Error: '+e.message);}};
 $('#bdall').onclick=bulkDownloadVars;
 renderVarRows('');
 if(!S.meta.checked&&!checking)startCheck();}
function renderVarRows(f){const q=(f||'').toLowerCase();
 const items=S.vars.filter(v=>!q||(v.title||'').toLowerCase().includes(q)||String(v.var_id).includes(q));
 $('#vcount').textContent=items.length+' of '+S.vars.length;
 $('#vtb').innerHTML=items.map(v=>`<tr class="clk" data-id="${v.var_id}" data-t="${esc(v.title)}"><td><span class="pill">${v.var_id}</span></td><td>${esc(v.title)}${badge(v)}</td><td class="muted">${esc(v.ts||'—')}</td><td class="muted">${esc(v.unit)}</td></tr>`).join('');
 $('#vtb').querySelectorAll('tr.clk').forEach(r=>r.onclick=()=>{S.var=r.dataset.id;S.varTitle=r.dataset.t;stateBar();show('years');});}
async function bulkDownloadVars(){if(!S.vars.length)return;
 if(!confirm('Export all '+S.vars.length+' variables (all available years) for subject '+S.subject+' into one CSV?\n\nThis can be large and take several minutes. It runs in the background — watch the Downloads tab.'))return;
 try{const r=await jpost('/api/save_csv',{vars:S.vars.map(v=>String(v.var_id)),thmode:'all',fname:'subject_'+S.subject+'_all.csv'});
  alert('Bulk export started (job '+r.job+'). Open the Downloads tab to watch progress.');show('downloads');}catch(e){alert('Error: '+e.message);}}

// ---- Years & export
async function loadYears(){const el=$('#v-years');if(!S.var){el.innerHTML='<div class="card">Pick a variable first (Variables tab).</div>';return;}
 el.innerHTML='<div class="card">Loading periods…</div>';let ys;
 try{ys=await jget('/api/years?var='+S.var);}catch(e){el.innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';return;}
 let h=`<div class="card"><div class="h2">Years & Export · var ${S.var} — ${esc(S.varTitle)}</div>
  <div class="row" style="margin-bottom:8px"><label class="muted">Periods:</label>
   <select id="ysel" multiple size="${Math.min(8,Math.max(3,ys.length))}" style="min-width:240px">
   ${ys.map(y=>`<option value="${y.th_id}">${esc(y.th)} (th=${y.th_id})</option>`).join('')}</select></div>
  <div class="note">Ctrl/Shift-click to select multiple. Leave empty = use "All years".</div>
  <div class="row" style="margin-top:12px">
   <button class="gho" id="bprev">Preview</button>
   <button class="act" id="bcsv">Download CSV (browser)</button>
   <button class="act" id="bsave">Save CSV to folder</button>
   <label class="muted"><input type="checkbox" id="ball"> all years</label>
   <label class="muted"><input type="checkbox" id="bgz"> gzip (save)</label>
  </div></div>
  <div id="prev"></div>`;
 el.innerHTML=h;
 const sel=()=>Array.from($('#ysel').selectedOptions).map(o=>o.value);
 $('#bprev').onclick=async()=>{const th=($('#ball').checked?[ys[0].th_id]:sel());if(!th.length){alert('Select a period or use Preview on the first.');return;}
  $('#prev').innerHTML='<div class="card">Loading preview…</div>';
  try{const d=await jget(`/api/preview?var=${S.var}&th=${th[0]}`);
   let t=`<div class="card"><div class="h2">Preview — ${d.total} rows total (showing ${d.rows.length})</div><div class="scroll"><table><thead><tr>${d.cols.map(c=>`<th>${c}</th>`).join('')}</tr></thead><tbody>`;
   d.rows.forEach(r=>{t+='<tr>'+d.cols.map(c=>`<td>${esc(r[c])}</td>`).join('')+'</tr>';});
   $('#prev').innerHTML=t+'</tbody></table></div></div>';}catch(e){$('#prev').innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';}};
 $('#bcsv').onclick=()=>{let th=$('#ball').checked?ys.map(y=>y.th_id):sel();if(!th.length){alert('Select at least one period (or tick all years).');return;}
  window.location='/api/data.csv?var='+S.var+'&th='+th.join('&th=');};
 $('#bsave').onclick=async()=>{const all=$('#ball').checked;const ths=sel();if(!all&&!ths.length){alert('Select periods or tick all years.');return;}
  try{const r=await jpost('/api/save_csv',{vars:[S.var],thmode:all?'all':'list',ths:ths,gzip:$('#bgz').checked});alert('Saving started (job '+r.job+'). See Downloads tab.');show('downloads');}catch(e){alert('Error: '+e.message);}};}

// ---- Publications
async function loadPubs(){const el=$('#v-pubs');if(!S.subject){el.innerHTML='<div class="card">Pick a subject first.</div>';return;}
 el.innerHTML='<div class="card">Loading publications…</div>';let pubs;
 try{pubs=await jget('/api/publications?subject='+S.subject);}catch(e){el.innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';return;}
 let h=`<div class="card"><div class="h2">Publications (PDF) · subject ${S.subject} — ${esc(S.subjectTitle)}</div>
  <div class="row"><span class="pill">${pubs.length} PDFs</span>
   <button class="act" id="ball">⬇ Download ALL to folder</button>
   <input type="text" id="pq" placeholder="Filter titles…"></div></div>
  <div class="scroll"><table><thead><tr><th>Title</th><th style="width:90px">Size</th><th style="width:100px">Date</th><th style="width:80px"></th></tr></thead><tbody id="ptb"></tbody></table></div>`;
 el.innerHTML=h;
 const draw=f=>{const q=(f||'').toLowerCase();$('#ptb').innerHTML=pubs.filter(p=>!q||p.title.toLowerCase().includes(q)).map(p=>
   `<tr><td>${esc(p.title)}${badge(p)}</td><td class="muted">${esc(p.size)}</td><td class="muted">${esc(p.rl_date)}</td><td>${p.pdf?`<a href="${esc(p.pdf)}" target="_blank">open</a>`:'<span class="muted">—</span>'}</td></tr>`).join('');};
 draw('');$('#pq').oninput=e=>draw(e.target.value);
 $('#ball').onclick=async()=>{try{const r=await jpost('/api/download_pubs',{subject:S.subject,subject_title:S.subjectTitle});alert('Download started (job '+r.job+'). See Downloads tab.');show('downloads');}catch(e){alert('Error: '+e.message);}};}

// ---- Static tables
async function loadStatic(){const el=$('#v-static');
 el.innerHTML=`<div class="card"><div class="h2">Static Tables (Excel)</div>
  <div class="row"><button class="gho" id="ball">Load ALL static tables</button>
   ${S.subject?`<button class="act" id="bsub">Load for subject ${S.subject}</button>`:''}
   <input type="text" id="tq" placeholder="Filter titles…"></div>
  <div class="note">Note: BPS's static-table subject filter is unreliable; "for subject" approximates via the subject's accounts family. Excel files open in a new tab to download.</div></div>
  <div id="stb"></div>`;
 const draw=(rows,f)=>{const q=(f||'').toLowerCase();const r=rows.filter(t=>!q||t.title.toLowerCase().includes(q));
  $('#stb').innerHTML=`<div class="scroll"><table><thead><tr><th>Title</th><th style="width:160px">Subject</th><th style="width:80px">Size</th><th style="width:70px"></th></tr></thead><tbody>`+
   r.map(t=>`<tr><td>${esc(t.title)}${badge(t)}</td><td class="muted">${esc(t.subj)}</td><td class="muted">${esc(t.size)}</td><td>${t.excel?`<a href="${esc(t.excel)}" target="_blank">xlsx</a>`:'—'}</td></tr>`).join('')+'</tbody></table></div>';};
 const load=async u=>{$('#stb').innerHTML='<div class="card">Loading… (this fetches up to 685 tables)</div>';try{const rows=await jget(u);$('#stb').dataset.n=rows.length;window._st=rows;draw(rows,'');$('#tq').oninput=e=>draw(rows,e.target.value);}catch(e){$('#stb').innerHTML='<div class="card">Error: '+esc(e.message)+'</div>';}};
 $('#ball').onclick=()=>load('/api/statictables');
 if($('#bsub'))$('#bsub').onclick=()=>load('/api/statictables?subject='+S.subject);}

// ---- Downloads
let dpoll=null;
async function loadDownloads(){const el=$('#v-downloads');
 const render=async()=>{let jobs;try{jobs=await jget('/api/jobs');}catch(e){return;}
  if(!jobs.length){el.innerHTML='<div class="card">No download jobs yet. Start one from Publications or Years & Export.</div>';return;}
  el.innerHTML='<div class="card"><div class="row"><div class="h2" style="margin:0">Download jobs</div><button class="gho" id="ofolder">Open data folder</button></div></div>'+jobs.map(j=>{
   const pct=j.total?Math.round(j.done/j.total*100):0;const tag=j.status=='done'?'ok':(j.status=='error'?'err':'run');
   return `<div class="card"><div class="row"><span class="tag ${tag}">${j.status}</span><b>${j.kind.toUpperCase()}</b> <span class="muted">${j.id}</span>
    <span class="muted">${j.done}/${j.total}</span></div>
    <div class="bar" style="margin:8px 0"><i style="width:${pct}%"></i></div>
    <div class="muted">${esc(j.current||j.error||(j.folder||''))}</div>
    ${j.log&&j.log.length?`<details><summary class="muted">log (${j.log.length})</summary><pre style="white-space:pre-wrap;font-size:12px;color:#9fb3c8">${esc(j.log.slice(-40).join('\n'))}</pre></details>`:''}</div>`;}).join('');
  const of=$('#ofolder');if(of)of.onclick=()=>fetch('/api/open_folder');};
 await render();clearInterval(dpoll);dpoll=setInterval(()=>{if(cur=='downloads')render();else clearInterval(dpoll);},1500);}

// ---- Settings
async function loadSettings(){const el=$('#v-settings');let s,doms=[];
 try{s=await jget('/api/settings');}catch(e){s={domain:'0000',lang:'ind',perpage:100,key_set:false};}
 try{doms=await jget('/api/domains');}catch(e){}
 el.innerHTML=`<div class="card"><div class="h2">Settings</div>
  <div class="row" style="margin-bottom:10px"><label style="width:130px">Region (domain)</label>
   <select id="sd">${doms.length?doms.map(d=>`<option value="${d.id}" ${d.id==s.domain?'selected':''}>${esc(d.id)} — ${esc(d.name)}</option>`).join(''):`<option value="${esc(s.domain)}">${esc(s.domain)}</option>`}</select></div>
  <div class="row" style="margin-bottom:10px"><label style="width:130px">Language</label>
   <select id="sl"><option value="ind" ${s.lang=='ind'?'selected':''}>Indonesian</option><option value="eng" ${s.lang=='eng'?'selected':''}>English</option></select></div>
  <div class="row" style="margin-bottom:10px"><label style="width:130px">API key</label>
   <input type="text" id="sk" placeholder="${s.key_set?esc(s.key_masked)+' (leave blank to keep)':'paste key'}"></div>
  <div class="row"><button class="act" id="ssave">Save settings</button> <span id="smsg" class="muted"></span></div>
  <div class="note">Region changes which office's data you get (0000 = national). Changing it resets cached lists.</div></div>`;
 $('#ssave').onclick=async()=>{const body={domain:$('#sd').value,lang:$('#sl').value};const k=$('#sk').value.trim();if(k)body.key=k;
  try{await jpost('/api/settings',body);S.subjects=[];S.vars=[];$('#smsg').textContent='Saved ✓';}catch(e){$('#smsg').textContent='Error: '+e.message;}};}

buildNav();stateBar();show('subjects');refreshNewCount();setInterval(refreshNewCount,60000);
</script></body></html>"""


def run_checks(subjects, force=False):
    """Headless update scan (for Windows Task Scheduler / cron). Updates update_state.json."""
    def prog(i, total):
        if i == total or i % 50 == 0:
            print(f"    {i}/{total} variables")
    if subjects == ["all"]:
        print(f"[{now_str()}] scanning ALL subjects (every variable + recent publications)…")
        r = scan_all(None, force=force, on_progress=prog)
        print(f"[{now_str()}] done. {r['total_vars']} variables "
              f"({r['checked']} fetched, {r['skipped']} cached), {r['pubs']} recent pubs, "
              f"{r['changed']} changed since last seen. State -> {STATE_FILE}")
    else:
        print(f"[{now_str()}] scanning {len(subjects)} subject(s): {', '.join(subjects)}")
        r = scan_all(subjects, force=force, on_progress=prog)
        print(f"[{now_str()}] done. {r['total_vars']} variables, {r['pubs']} recent pubs, "
              f"{r['changed']} changed. State -> {STATE_FILE}")


def main():
    import sys
    argv = sys.argv[1:]
    if argv and argv[0] in ("--check", "--check-all"):
        force = "--force" in argv
        rest = [a for a in argv[1:] if a != "--force"]
        subs = ["all"] if argv[0] == "--check-all" else (rest or ["all"])
        return run_checks(subs, force=force)

    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}/"
    print(f"BPS Data Downloader running at {url}")
    print("Press Ctrl+C to stop.   (headless update check: python bps_app.py --check 530 531)")
    try:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")


if __name__ == "__main__":
    main()
