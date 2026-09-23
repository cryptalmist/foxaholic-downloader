# Foxaholic Downloader — Usage Guide

Download novels from `foxaholic.com` (works for `www` and `18+` subdomains)
to **MD / TXT / EPUB / PDF**, illustrations and novel info included. The site
sits behind Cloudflare, so the script drives real Chrome — plain
`requests`/`curl`/`gallery-dl` get HTTP 403.

## 1. One-time setup

```powershell
cd C:\Users\crypt\Documents\Stuff\foxaholic-downloader
pip install -r requirements.txt   # seleniumbase + requests + fpdf2
```

Requires **Google Chrome** installed.

## 2. Your novel

```powershell
# 1) peek at the novel info + chapter list first (557 chapters, no downloading)
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --list-only

# 2) full download — everything (MD + TXT + EPUB + PDF)
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --workers 20

# 3) PDF only
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --workers 20 --format pdf

# 4) try the first 2 chapters before committing to all 557
python foxaholic.py "https://www.foxaholic.com/novel/i-got-a-new-skill-every-time-i-was-exiled-and-after-100-different-worlds-i-was-unmatched/" --workers 4 --from 1 --to 2
```

The `--list-only` run prints the novel's title, original (Japanese) title,
author, translator team, status, translation status, type, genres, tags,
a synopsis preview, and the chapter list.

Output lands in `out/<Novel Title>/`:

```text
out/I Got a New Skill Every Time I Was Exiled.../
  md/
    chapter-0001.md   # chapter-name line + body (also the resume cache)
    ...
    images/           # illustrations, referenced from the .md files
  cover.jpg           # novel cover, shown on EPUB/PDF title pages
  <Title>.txt         # info header + synopsis + whole novel ([Image: ...] notes)
  <Title>.epub        # cover page + chapters, images embedded
  <Title>.pdf         # cover + info + synopsis title page, then chapters
  metadata.json       # all novel info as JSON
  failed.json         # only if chapters still failed: re-run to retry them
```

## 3. Any other novel

Swap in any title-page URL — same commands:

```powershell
python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --workers 20 --format pdf
python foxaholic.py "https://18.foxaholic.com/novel/<slug>/" --workers 20 --format epub
```

## 4. Options reference

| Flag | Default | Meaning |
|---|---|---|
| `--workers N` | `1` | Parallel chapter fetchers, 1–100 (try `4`–`20`). Browser solves Cloudflare once; workers reuse its cookies. `1` = one-by-one through the browser |
| `--refresh-retries N` | `2` | Rounds of Cloudflare re-solving + retry for failed chapters (1 initial pass + N refreshes); leftovers go to `failed.json` |
| `--format` | `all` | Comma list from `epub,txt,md,pdf` |
| `--from N --to M` | all | Chapter range (positions in the chapter list, `1`-based) |
| `--delay S` | `1.2` | Pause between chapters (per worker). Be polite — don't hammer |
| `--headed` | off | Show the browser window. Use if the challenge won't clear headless so you can tick it manually |
| `--timeout S` | `120` | How long to wait for the Cloudflare challenge to clear |
| `--list-only` | off | Print novel info + chapters, download nothing |
| `-o DIR` | `out` | Output root folder |

## 5. Resuming

Interrupt anytime (`Ctrl+C`) and re-run the **same command** — existing
`md/chapter-*.md` files over 500 bytes are skipped, so it picks up where it left off.
Chapters that kept failing are listed in `failed.json` and retried on the next run.

## 6. Troubleshooting

| Symptom | Fix |
|---|---|
| `Cloudflare challenge did not clear` | Re-run with `--headed` and tick the checkbox manually once |
| `found 0 chapter link(s)` + `debug_novel.png` | Usually a transient Cloudflare 502 from too many rapid runs — wait ~1 min and retry (resume keeps progress). If persistent, the site changed layout |
| Cookie session blocked | Script falls back to sequential browser mode automatically; or retry with `--workers 1` |
| `only N paragraph(s)` warnings | Chapter layout changed; check the `.md` file to see what was captured |
| `Chrome not found` | Install Google Chrome |
| `fpdf2 not installed` | `pip install fpdf2` (only needed for `--format pdf`) |
