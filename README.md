# BPS Data Tools

**Interactive app:** https://hengkykurniawan.github.io/bps_download/

**Repository:** https://github.com/hengkykurniawan/bps_download

Download BPS (Badan Pusat Statistik) data from `webapi.bps.go.id` — both the
publication PDFs and the actual statistics as ready-to-use CSV.

**Same folder, same key, no installs.** Pure Python 3 standard library.
The API key is read automatically from `.bps_key` in this folder.

## Quick start

```bash
git clone https://github.com/hengkykurniawan/bps_download.git
cd bps_download
```

Create a file named `.bps_key`, paste your BPS WebAPI key into it, then run:

```bash
python bps_app.py
```

---

## 🌐 `bps_app.py` — web app (easiest; recommended)

A local, browser-based UI that wraps everything below. Zero install.

**Double-click `Start BPS App.bat`** — or from a terminal:

```
python bps_app.py
```

A browser opens at **http://127.0.0.1:8765**. You can use that local page or the
**GitHub Pages app** linked above; both connect to the same private loopback
service. Keep the terminal/window open while using either interface. Your API
key remains in `.bps_key` and is never stored in GitHub Pages. Press `Ctrl+C`
(or close the window) to stop.

**Menus:**
- **🔔 What's New** — a digest of recent + changed variables and publications across **all 37 subjects**, newest first. Click **Scan all subjects** once (a few minutes, runs in the background) and it tracks every subject — no need to open each one. Click an item to jump to its export (variables) or open the PDF (publications). The tab shows a **red count bubble** of NEW (changed-since-seen) items, refreshed on load, after each scan, and every minute.
- **Subjects** — all 37 website subjects, grouped by category, searchable. Click one to drill in.
- **Variables** — dynamic-data variables under the chosen subject, **sorted by most-recent update** (newest first), with a Last-update column. Search, unit shown. A **⬇ Download all N variables (CSV)** button beside the title bulk-exports every variable (all years) into one CSV in the background.
- **Years & Export** — periods for a variable; **preview** the data, **download CSV** to your browser, or **save CSV to the folder** (with "all years" and gzip options).
- **Publications** — publication PDFs for the subject; open one, or **bulk-download all** to the folder (resumable, with live progress).
- **Static Tables** — pre-made Excel tables; search all, or filter by the subject's accounts family. Open `.xlsx` to download.
- **Downloads** — live progress bars for bulk jobs and links to finished files.
- **Settings** — region/**domain** (national vs. any of 549 provincial/regency offices), language (ID/EN), page size, and the API key.

Everything runs locally; the backend makes the BPS calls, so the key and the
Cloudflare-safe headers never touch the browser.

### Update indicators (is the data fresh?)

The Variables, Publications, and Static Tables lists each show a bright badge
next to the title, and the **🔔 What's New** tab rolls them up across subjects:

- **`↻ Nd`** (green) — updated within the last 30 days (N = days ago).
- **`● NEW`** (pulsing amber) — changed since you last clicked **Mark all seen**.

How it knows: publications/static tables carry their update date in the list
(free). Dynamic variables don't, so the **Check updates** button fetches each
variable's `last_update` in parallel (~30s for 90 vars) and caches it in
`update_state.json`. The first check sets your baseline (no false "NEW"); after
that, anything BPS revises shows the amber badge until you Mark all seen.

### Regular / automatic checking (even when the app is closed)

Run a headless scan from the terminal — it updates `update_state.json` so the
**🔔 What's New** digest and badges are ready next time you open the app:

```
python bps_app.py --check-all          # ALL subjects + recent publications (~8 min)
python bps_app.py --check-all --force   # re-fetch everything, ignore the 6h cache
python bps_app.py --check 530 531       # just these subjects (fast)
```

`--check-all` is the same as the **Scan all subjects** button. It fetches every
variable's last-update (~1,700 vars) so it takes several minutes; results are
cached, and a re-run within 6 hours skips unchanged work (use `--force` to override).

Schedule it on Windows (e.g. daily at 6am) with Task Scheduler so you get a fresh
"what changed at BPS" view every morning without opening anything:

```
schtasks /create /tn "BPS update scan" /sc DAILY /st 06:00 ^
  /tr "python \"C:\Users\hengk\BPS\downloads\bps_app.py\" --check-all"
```

(If the task can't find `python`, use its full path, e.g.
`C:\Users\hengk\AppData\Local\Python\pythoncore-3.14-64\python.exe`.)

The command-line tools below do the same work and are handy for scripting/automation.

---

## `bps_data.py` — statistics → CSV

Pulls the numbers behind BPS "Tabel Dinamis" and writes a tidy, one-row-per-value
CSV. No PDF-to-table conversion needed.

### 0. List all available subjects

```
python bps_data.py subjects                    # all 37 subject ids (the subject= on the website)
python bps_data.py subjects --csv subjects.csv  # also save to CSV
```

### 1. Find variables under a subject (the `subject=` from the website)

```
python bps_data.py vars --subject 531          # 90 variables in Neraca Ekonomi
python bps_data.py vars --subject 530 --csv vars_530.csv
```

### 2. See which years a variable has

```
python bps_data.py years --var 2776            # → th=126 (2026)
```

### 3. Export the data to CSV

```
python bps_data.py get --var 2776 --th 126         # → data_var2776_th126.csv
python bps_data.py get --var 2776 --th all          # every available year
python bps_data.py get --var 8 9 --th all --gzip    # multiple vars, compact
```

**CSV columns:** `var_id, variable, unit, vervar_id, vervar, turvar_id, turvar,
year_id, year, period_id, period, value`
(`vervar` = row entity such as region/kab-kota, `turvar` = sub-category,
`period` = sub-year such as quarter/month.)

### The full drill-down

```
subjects ──▶ pick subject=NNN ──vars──▶ pick var ──years──▶ pick th ──get──▶ CSV
```

---

## `bps_download.py` — publication PDFs

```
python bps_download.py 530                # download all PDFs for subject 530
python bps_download.py 530 531            # several subjects at once
python bps_download.py 530 --list         # preview titles, no download
python bps_download.py 530 --limit 5      # first 5 only (test)
python bps_download.py 530 --domain 3500  # regional domain (e.g. a province)
```

Saves to `<id> - <theme>\`, is **resumable** (skips files already on disk),
writes a `manifest.csv` per folder, and skips any pub_ids listed in
`ignore_pubids.txt` (publications BPS's server cannot serve).

---

## Notes

- **`subject=NNN`** on the website (e.g. `statistics-table?subject=530`) is the
  API's `id_subject_csa`. The same number works for both tools.
- Default domain is `0000` (national). Override with `--domain`.
- **API key:** stored in `.bps_key`. If it ever needs replacing, regenerate it
  at the BPS developer portal and paste the new value into that file — the
  scripts pick it up automatically.
- Both scripts need no third-party packages (Python 3 standard library only).
