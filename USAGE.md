# Foxaholic Downloader — Usage Guide

Download novels from `foxaholic.com` (works for `www` and `18+` subdomains)
to **MD / TXT / EPUB / PDF**, illustrations included. The site sits behind
Cloudflare, so the script drives a real browser — plain
`requests`/`curl`/`gallery-dl` get HTTP 403.

## 1. One-time setup

```powershell
cd C:\Users\crypt\Documents\Stuff\foxaholic-downloader

# base downloader (Playwright mode)
pip install -r requirements.txt
python -m playwright install chromium

# stronger Cloudflare bypass (recommended)
pip install seleniumbase
```

UC mode needs **Google Chrome** installed (it drives real Chrome, while
Playwright bundles its own Chromium).

## 2. Your novel

```powershell
# 1) peek at the chapter list first (557 chapters, no downloading)
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --uc --list-only

# 2) full download — everything (MD + TXT + EPUB + PDF)
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --uc --workers 20

# 3) PDF only
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --uc --workers 20 --format pdf

# 4) try the first 2 chapters before committing to all 557
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --uc --workers 4 --from 1 --to 2
```

Output lands in `out/<Novel Title>/`:

```text
out/I Got a New Skill Every Time I Was Exiled.../
  md/
    chapter-0001.md   # chapter-name line + body (also the resume cache)
    ...
    images/           # illustrations, referenced from the .md files
  <Title>.txt       # whole novel, plain text ([Image: ...] placeholders)
  <Title>.epub      # e-reader format, images embedded
  <Title>.pdf       # one PDF, images embedded, new page per chapter
  metadata.json
  failed.json       # only if chapters still failed: re-run to retry them
```

## 3. Any other novel

Swap in any title-page URL — same commands:

```powershell
python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --uc --workers 20 --format pdf
python foxaholic.py "https://18.foxaholic.com/novel/<slug>/" --uc --workers 20 --format epub
```

## 4. Options reference

| Flag | Default | Meaning |
|---|---|---|
| `--uc` | off | Undetected-chromedriver mode (stronger Cloudflare bypass). Needs `pip install seleniumbase` + Chrome |
| `--headed` | off | Show the browser window. Use if the challenge won't clear headless so you can tick it manually |
| `--workers N` | `1` | Parallel chapter fetchers, 1–100 (try `4`–`20`). Browser solves Cloudflare once; workers reuse its cookies. `1` = one-by-one through the browser |
| `--refresh-retries N` | `2` | Rounds of Cloudflare re-solving + retry for failed chapters (1 initial pass + N refreshes); leftovers go to `failed.json` |
| `--format` | `all` | Comma list from `epub,txt,md,pdf` |
| `--from N --to M` | all | Chapter range (positions in the chapter list, `1`-based) |
| `--delay S` | `1.2` | Pause between chapters (per worker). Be polite — don't hammer |
| `--timeout S` | `120` | How long to wait for the Cloudflare challenge to clear |
| `--list-only` | off | Print chapters, download nothing |
| `-o DIR` | `out` | Output root folder |
| `--user-data-dir PATH` | `.foxaholic-profile` | Playwright cookie profile (solve Cloudflare once, reuse) |

## 5. Resuming

Interrupt anytime (`Ctrl+C`) and re-run the **same command** — existing
`chapter-*.md` files over 500 bytes are skipped, so it picks up where it left off.
Chapters that kept failing are listed in `failed.json` and retried on the next run.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `Cloudflare challenge did not clear` | Re-run with `--headed` and tick the checkbox manually once |
| `found 0 chapter link(s)` + `debug_novel.png` | Usually a transient Cloudflare 502 from too many rapid runs — wait ~1 min and retry (resume keeps progress). If persistent, the site changed layout |
| `[workers] probe failed ... cookie session unusable` | Cookies didn't transfer — script auto-falls back to sequential browser mode; or retry with `--workers 1` |
| `only N paragraph(s)` warnings | Chapter layout changed; check the `.md` file to see what was captured |
| `Chrome not found` with `--uc` | Install Google Chrome, or drop `--uc` to use Playwright mode |
| `fpdf2 not installed` | `pip install fpdf2` (only needed for `--format pdf`) |
