# Foxaholic novel downloader

Downloads a full novel from `foxaholic.com` to `MD` / `TXT` / `EPUB` / `PDF`,
**including illustrations**. Works for both `www.foxaholic.com` and
`18.foxaholic.com`.

`requests` / `curl` / `gallery-dl` do **not** work here — the site sits behind
a Cloudflare "Just a moment" challenge (HTTP 403). This script drives a real
browser to clear it, then downloads chapters in parallel by reusing the
browser's clearance cookies. See [USAGE.md](USAGE.md) for the full guide.

## Install (once)

```powershell
cd C:\Users\crypt\Documents\Stuff\foxaholic-downloader
pip install -r requirements.txt
python -m playwright install chromium

# recommended: stronger Cloudflare bypass (needs Google Chrome installed)
pip install seleniumbase
```

## Quick start

```powershell
# full novel, all formats, 20 parallel fetchers
python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --uc --workers 20

# PDF only
python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --uc --workers 20 --format pdf

# just list chapters, download nothing
python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --uc --list-only
```

Output (`out/<Novel Title>/`):

```text
md/
  chapter-0001.md   # chapter-name line + body (also the resume cache)
  ...
  images/           # illustrations, referenced from the .md files
<Title>.txt         # whole novel, plain text ([Image: ...] placeholders)
<Title>.epub        # e-reader format, images embedded
<Title>.pdf         # one PDF, images embedded, new page per chapter
metadata.json
failed.json         # only if chapters still failed: re-run to retry them
```

## How it works

1. **Browser clears Cloudflare** — Playwright (persistent `.foxaholic-profile/`,
   solve once and reuse) or `--uc` SeleniumBase undetected-chromedriver
   (`SB(uc=True)` + `uc_gui_handle_captcha()`, auto-solves when possible).
2. **Workers download chapters in parallel** (`--workers 1–100`) with plain
   HTTP reusing the browser's cookies — much faster than driving the browser
   per chapter.
3. **Failures refresh the session** — up to `--refresh-retries` rounds of
   re-solving Cloudflare with fresh cookies; leftovers go to `failed.json`.
4. **Resume is automatic** — existing `chapter-*.md` files are skipped, so
   re-running the same command picks up where it stopped.

## Options

| Flag | Default | Meaning |
|---|---|---|
| `--uc` | off | Undetected-chromedriver mode (recommended; needs `seleniumbase` + Chrome) |
| `--headed` | off | Show the browser window (tick the challenge manually if it won't clear) |
| `--workers N` | `1` | Parallel fetchers, 1–100. `1` = one-by-one through the browser |
| `--refresh-retries N` | `2` | Cloudflare re-solve rounds for failed chapters (1 initial pass + N refreshes) |
| `--format` | `all` | Comma list from `epub,txt,md,pdf` |
| `--from N --to M` | all | Chapter range (positions in chapter list, 1-based) |
| `--delay S` | `1.2` | Pause between chapters (per worker) |
| `--timeout S` | `120` | Seconds to wait for the Cloudflare challenge to clear |
| `--list-only` | off | Print chapters, download nothing |
| `-o DIR` | `out` | Output root folder |
| `--user-data-dir PATH` | `.foxaholic-profile` | Playwright cookie profile dir |

## Troubleshooting

- `Cloudflare challenge did not clear` → re-run with `--headed` and tick the box manually.
- `found 0 chapter link(s)` → usually a transient Cloudflare 502 from rapid repeat runs; wait ~1 min and retry (resume keeps progress).
- `[workers] probe failed` → cookie session blocked; script falls back to sequential browser mode automatically.
- `Chrome not found` with `--uc` → install Google Chrome, or drop `--uc`.

---
Built with Muse Spark via OpenCode.
