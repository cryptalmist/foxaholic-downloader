#!/usr/bin/env python3
"""
Foxaholic novel downloader
--------------------------
Downloads a full novel from www.foxaholic.com (and 18.foxaholic.com)
which is protected by Cloudflare "Just a moment" challenge.

Built with Muse Spark via OpenCode.

Why a real browser and not requests?
  Simple HTTP (requests / curl / gallery-dl) gets HTTP 403 + Cloudflare
  challenge page. This script drives real Chrome via SeleniumBase
  undetected-chromedriver (SB(uc=True) + uc_gui_handle_captcha) and reuses
  the clearance cookies for fast parallel downloads.

Usage:
  pip install -r requirements.txt   # seleniumbase + requests + fpdf2

  # peek at chapters:
  python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --list-only

  # full download (all formats):
  python foxaholic.py "https://www.foxaholic.com/novel/<slug>/" --workers 20

  # chapter range + formats:
  python foxaholic.py "URL" --from 1 --to 50 --format epub,pdf --delay 1.5

Output:
  out/<Novel Title>/
    chapter-0001.md, chapter-0002.md, ...
    <Novel Title>.txt
    <Novel Title>.epub
    metadata.json
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import threading
import time
import uuid
import zipfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

# ---------------------------------------------------------------- selectors

CHAPTER_LINK_SELECTORS = [
    "ul.main.version-chap li.wp-manga-chapter a",
    "ul.version-chap li a",
    ".listing-chapters_wrap ul li a",
    ".page-content-listing ul li a",
    "ul.row-content-chapter li a",
    ".chapter-list a",
]

CHAPTER_CONTENT_SELECTORS = [
    ".reading-content .text-left",
    ".reading-content",
    ".text-content",
    ".entry-content .manga-single-chapter",
    ".chapter-content",
    "#chapter-content",
    ".manga-single-chapter",
    "article .entry-content",
    ".post-entry .entry-content",
]

TITLE_SELECTORS = [
    ".post-title h1",
    ".manga-title h1",
    "h1.entry-title",
    ".novel-title h1",
    "h1",
]

CLOUDFLARE_TITLE_HINTS = ("just a moment", "attention required", "verifying you are human")

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                 "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


# ------------------------------------------------------- UC mode (SeleniumBase)


# ------------------------------------------------------- content + image extraction

_IMG_MD_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")

# class-name tokens that mark ads / chrome rather than story content
_AD_TOKENS = {"ad", "ads", "advertisement", "banner", "banners", "placement",
              "sponsor", "sponsored", "donation", "donate", "popup", "popups",
              "sidebar", "widget", "widgets", "related", "comments", "footer",
              "header", "breadcrumb", "breadcrumbs", "nav", "navigation",
              "share", "social", "promo", "subscribe", "newsletter", "logo",
              "avatar", "avatars", "icon", "icons"}


def _cls_tokens(attrs: str) -> set:
    m = re.search(r"class\s*=\s*[\"']([^\"']*)[\"']", attrs or "")
    if not m:
        return set()
    return set(re.split(r"[\s\-_]+", m.group(1).lower()))


def _pick_img_src(tag: str):
    for attr in ("data-src", "data-original", "data-lazy-src", "data-srcset", "src"):
        m = re.search(attr + r"\s*=\s*[\"']([^\"']+)[\"']", tag)
        if m:
            u = m.group(1).strip()
            if u and not u.startswith("data:"):
                return u
    m = re.search(r"srcset\s*=\s*[\"']([^\"']+)[\"']", tag)
    if m:
        u = m.group(1).split(",")[0].strip().split(" ")[0]
        if u and not u.startswith("data:"):
            return u
    return None


def _extract_content(frag: str, base_url: str) -> list:
    """Content blocks in document order: ('p', text) or ('img', abs_url).

    Skips ad/chrome blocks (donation banners, GTM, etc.) so only story text
    and real illustrations are returned.
    """
    frag = re.sub(r"(?is)<(script|style|iframe|noscript|template)[^>]*>.*?</\1>", " ", frag)
    blocks: list = []
    seen_imgs: set = set()
    for m in re.finditer(r"(?is)<(p|h2|h3|li|figure)(\s[^>]*)?>(.*?)</\1>", frag):
        attrs, inner = m.group(2) or "", m.group(3) or ""
        if _cls_tokens(attrs) & _AD_TOKENS:
            continue
        for im in re.finditer(r"(?is)<img\b[^>]*>", inner):
            s = _pick_img_src(im.group(0))
            if not s:
                continue
            full = urljoin(base_url, s)
            fn = urlparse(full).path.rsplit("/", 1)[-1].lower()
            if _cls_tokens(im.group(0)) & _AD_TOKENS:
                continue
            if any(k in fn for k in ("banner", "donation", "logo", "avatar", "icon", "sponsor", "popup")):
                continue
            if full not in seen_imgs:
                seen_imgs.add(full)
                blocks.append(("img", full))
        text = html.unescape(re.sub(r"(?s)<br\s*/?>", "\n", inner))
        text = re.sub(r"(?s)<[^>]+>", "", text)
        text = re.sub(r"[ \t\xa0]+", " ", text).strip()
        if text:
            # text belonging to an ad-only block (e.g. donation caption) is skipped
            blocks.append(("p", text))
    # stray illustrations outside p/figure blocks (appended last, better than lost)
    for im in re.finditer(r"(?is)<img\b[^>]*>", frag):
        s = _pick_img_src(im.group(0))
        if not s:
            continue
        full = urljoin(base_url, s)
        fn = urlparse(full).path.rsplit("/", 1)[-1].lower()
        if full in seen_imgs:
            continue
        if _cls_tokens(im.group(0)) & _AD_TOKENS:
            continue
        if any(k in fn for k in ("banner", "donation", "logo", "avatar", "icon", "sponsor", "popup")):
            continue
        seen_imgs.add(full)
        blocks.append(("img", full))
    return blocks


class _UCElement:
    """Wraps a Selenium WebElement with the Playwright-ish API used below."""

    def __init__(self, el):
        self._el = el

    def get_attribute(self, name: str):
        try:
            return self._el.get_attribute(name)
        except Exception:
            return None

    def inner_text(self) -> str:
        try:
            return self._el.text or ""
        except Exception:
            return ""

    def query_selector_all(self, sel: str) -> list:
        from selenium.webdriver.common.by import By

        try:
            return [_UCElement(e) for e in self._el.find_elements(By.CSS_SELECTOR, sel)]
        except Exception:
            return []


class _UCPage:
    """Wraps a SeleniumBase driver with the subset of the Playwright Page API used below."""

    def __init__(self, driver):
        self._d = driver

    @property
    def url(self) -> str:
        try:
            return self._d.get_current_url()
        except Exception:
            return ""

    def set_default_navigation_timeout(self, _ms: int) -> None:
        pass

    def goto(self, url: str, wait_until=None) -> None:
        self._d.open(url)
        try:
            self._d.uc_gui_handle_captcha()
        except Exception:
            pass
        try:
            self._d.sleep(1.5)
        except Exception:
            time.sleep(1.5)

    def title(self) -> str:
        try:
            return self._d.title or ""
        except Exception:
            return ""

    def content(self) -> str:
        try:
            return self._d.get_page_source()
        except Exception:
            return ""

    def query_selector(self, sel: str):
        els = self.query_selector_all(sel)
        return els[0] if els else None

    def query_selector_all(self, sel: str) -> list:
        from selenium.webdriver.common.by import By

        try:
            return [_UCElement(e) for e in self._d.find_elements(By.CSS_SELECTOR, sel)]
        except Exception:
            return []

    def wait_for_timeout(self, ms: int) -> None:
        try:
            self._d.sleep(ms / 1000)
        except Exception:
            time.sleep(ms / 1000)

    def screenshot(self, path: str) -> None:
        try:
            self._d.save_screenshot(path)
        except Exception:
            pass

    def get_cookies(self) -> list:
        try:
            return self._d.get_cookies()
        except Exception:
            return []

    def user_agent(self) -> str:
        try:
            return self._d.execute_script("return navigator.userAgent;") or ""
        except Exception:
            return ""


@contextmanager
def _open_page(args, script_dir: Path):
    """Yield (page, close_fn, cookies_fn, ua) via SeleniumBase UC mode (always)."""
    try:
        from seleniumbase import SB
    except ImportError:
        raise ImportError("ERROR: seleniumbase is not installed.\n  pip install seleniumbase")
    print(f"[browser] launching UC-mode chromium ({'headed' if args.headed else 'headless'}) ...")
    with SB(uc=True, headless=not args.headed) as driver:
        upage = _UCPage(driver)
        yield upage, lambda: None, upage.get_cookies, upage.user_agent()


# ---------------------------------------------------------------- helpers


def slug_from_url(url: str) -> str:
    m = re.search(r"/novel/([^/]+)/?", url)
    return m.group(1) if m else "novel"


def _short_chapter_name(ctitle: str, novel_title: str = "") -> str:
    """Strip a leading 'Novel Title - ' prefix so headers show just the chapter name."""
    t = (ctitle or "").strip()
    if novel_title:
        nt = novel_title.strip()
        if t.startswith(nt):
            t = t[len(nt):].lstrip(" -\u2013\u2014:|\\u00b7")
    return t.strip() or ctitle


def _best_title(h1title: str, listname: str, idx: int) -> str:
    """Pick the more informative title: h1 often lacks the subtitle the list has."""
    a = (h1title or "").strip()
    b = (listname or "").strip() or f"Chapter {idx}"
    if a and b.startswith(a):
        return b
    if b and a.startswith(b):
        return a
    return a or b


def _md_path(book_dir, idx):
    """Path for a chapter md, migrating old flat layout (book_dir/x.md) into md/."""
    newp = Path(book_dir) / "md" / f"chapter-{idx:04d}.md"
    oldp = Path(book_dir) / f"chapter-{idx:04d}.md"
    if not newp.exists() and oldp.exists():
        try:
            newp.parent.mkdir(parents=True, exist_ok=True)
            oldp.rename(newp)
        except Exception:
            return oldp
    return newp


def _read_cached_md(md_path, chapter_name: str = "") -> list[str]:
    """Paras from a cached chapter md.

    Handles all historic formats: `# title` header, plain chapter-name first
    line, or body-only. The name line is dropped; everything else is body.
    """
    raw = md_path.read_text(encoding="utf-8")
    if raw.startswith("# "):
        parts = raw.split("\n\n", 1)
        body = parts[1] if len(parts) > 1 else ""
    elif chapter_name:
        parts = raw.split("\n\n", 1)
        if parts and parts[0].strip() == chapter_name.strip() and len(parts) > 1:
            body = parts[1]
        else:
            body = raw
    else:
        body = raw
    return [p for p in body.split("\n\n") if p.strip()]
    m = re.search(r"/novel/([^/]+)/?", url)
    return m.group(1) if m else "novel"


def _clean_name(name: str) -> str:
    name = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", name)
    return re.sub(r"\s+", " ", name).strip()[:200]
    m = re.search(r"/novel/([^/]+)/?", url)
    return m.group(1) if m else "novel"


def safe_filename(name: str, maxlen: int = 120) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen] or "novel").strip()


def chapter_sort_key(url: str) -> tuple:
    m = re.search(r"chapter-(\d+)(?:-(\d+))?", url)
    if m:
        return (int(m.group(1)), int(m.group(2) or 0), url)
    return (10**9, 0, url)


def is_cloudflare_challenge(page) -> bool:
    try:
        title = (page.title() or "").lower()
        if any(h in title for h in CLOUDFLARE_TITLE_HINTS):
            return True
        body = page.content()
        return "challenges.cloudflare.com" in body and "Just a moment" in body
    except Exception:
        return False


def wait_for_cloudflare(page, timeout: int = 120, headed: bool = False) -> bool:
    """Wait until CF challenge clears. Returns True if page looks usable."""
    start = time.time()
    while time.time() - start < timeout:
        if not is_cloudflare_challenge(page):
            return True
        left = int(timeout - (time.time() - start))
        print(f"  [cf] Cloudflare challenge detected, waiting... ({left}s left)", flush=True)
        if headed:
            print("       -> Solve the checkbox in the opened browser window if shown.", flush=True)
        time.sleep(3)
        try:
            page.wait_for_timeout(2000)
        except Exception:
            pass
    return not is_cloudflare_challenge(page)


def extract_novel_title(page) -> str:
    for sel in TITLE_SELECTORS:
        try:
            el = page.query_selector(sel)
            if el:
                t = (el.inner_text() or "").strip()
                t = re.sub(r"\s*-\s*Foxaholic\s*$", "", t).strip()
                if t and len(t) > 1 and "just a moment" not in t.lower():
                    return t
        except Exception:
            continue
    try:
        t = (page.title() or "").strip()
        t = re.sub(r"\s*[-|]\s*Foxaholic.*$", "", t).strip()
        if t:
            return t
    except Exception:
        pass
    return slug_from_url(page.url)


def extract_novel_meta(page) -> dict:
    """Title, original title, author, team, genres, status, type, synopsis, cover.

    Parsed from raw HTML with regex (single snapshot, no extra roundtrips —
    live element handles go stale when the page JS re-renders).
    """
    meta = {"title": "", "original_title": "", "author": [],
            "team": [], "genres": [], "tags": [], "status": "", "translation": "",
            "type": "", "synopsis": [], "cover_url": ""}
    try:
        meta["title"] = extract_novel_title(page)
    except Exception:
        pass
    try:
        html_text = page.content()
    except Exception:
        return meta

    def _links(content: str) -> list:
        return [t for t in (_strip_tags(m) for m in re.findall(r"(?is)<a\b[^>]*>(.*?)</a>", content)) if t]

    for m in re.finditer(r"(?is)<div\s+class=\"post-content_item\">.*?<h5\b[^>]*>(.*?)</h5>"
                         r".*?<div\s+class=\"summary-content\">(.*?)</div>\s*</div>", html_text):
        head = _strip_tags(m.group(1)).strip().lower()
        content = m.group(2)
        links = _links(content)
        text = _strip_tags(content).strip()
        if "genre" in head:
            meta["genres"] = links or ([text] if text else [])
        elif "tag" in head:
            meta["tags"] = links or ([text] if text else [])
        elif "author" in head:
            meta["author"] = links or ([text] if text else [])
        elif "team" in head or "artist" in head:
            meta["team"] = links or ([text] if text else [])
        elif "translation" in head:
            meta["translation"] = links[0] if links else text.split("\n")[0].strip()
        elif head == "novel" or "status" in head:
            meta["status"] = links[0] if links else text.split("\n")[0].strip()
        elif head == "type":
            meta["type"] = links[0] if links else text.split("\n")[0].strip()
        elif head == "title" and not meta["original_title"]:
            meta["original_title"] = text.split("\n")[0].strip()

    m = re.search(r"(?is)<div\s+class=\"description-summary\">(.*?)<div\s+class=\"c-blog__heading\b[^\"]*\"",
                    html_text)
    seg = m.group(1) if m else ""
    for pm in re.finditer(r"(?is)<p\b[^>]*>(.*?)</p>", seg):
        t = _strip_tags(pm.group(1)).replace("\xa0", " ").strip()
        if t and t not in meta["synopsis"]:
            meta["synopsis"].append(t)

    m = re.search(r"(?is)summary_image.*?<img\b[^>]*?(?:data-src|src)\s*=\s*[\"']([^\"']+)[\"']", html_text)
    if m:
        meta["cover_url"] = m.group(1).strip()
    return meta


def extract_chapter_links(page, novel_url: str) -> list[dict]:
    """Return [{'url': ..., 'name': ...}] sorted oldest->newest."""
    found: dict[str, str] = {}
    for sel in CHAPTER_LINK_SELECTORS:
        try:
            els = page.query_selector_all(sel)
        except Exception:
            continue
        for el in els:
            try:
                href = el.get_attribute("href") or ""
                name = (el.inner_text() or "").strip()
            except Exception:
                continue
            if not href or "chapter" not in href.lower():
                continue
            full = urljoin(page.url, href)
            if "/novel/" not in full:
                continue
            if full not in found:
                found[full] = _clean_name(name)
        if found:
            break

    # Fallback: any anchor with /chapter- in href (Madara pattern)
    if not found:
        try:
            els = page.query_selector_all('a[href*="chapter-"]')
            for el in els:
                try:
                    href = el.get_attribute("href") or ""
                    name = (el.inner_text() or "").strip()
                except Exception:
                    continue
                full = urljoin(page.url, href)
                if "/novel/" not in full or len(full) > 500:
                    continue
                if full not in found:
                    found[full] = re.sub(r"\s+", " ", name)[:200]
        except Exception:
            pass

    items = [{"url": u, "name": n} for u, n in found.items()]
    items.sort(key=lambda d: chapter_sort_key(d["url"]))
    return items


# ---------------------------------------------------------------- epub (no deps)


def _para_to_xhtml(p: str, md_dir: Path, img_items: dict) -> str:
    """One md para -> xhtml. Registers `![..](images/..)` refs into img_items."""
    segs, last = [], 0
    for mo in _IMG_MD_RE.finditer(p):
        segs.append(html.escape(p[last:mo.start()]))
        rel = mo.group(2)
        f = Path(md_dir) / rel
        if f.exists():
            img_items[rel] = f.read_bytes()
            segs.append(f'<img src="{html.escape(rel)}" alt="illustration"/>')
        else:
            segs.append(html.escape(f"[Image: {rel}]"))
        last = mo.end()
    segs.append(html.escape(p[last:]))
    return "<p>" + "".join(segs).replace("\n", "<br/>") + "</p>"


def _epub_mime(name: str) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
            ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp"}.get(
            os.path.splitext(name)[1].lower(), "image/jpeg")


def _xhtml_page(title: str, paras: list[str], md_dir: Path, img_items: dict) -> str:
    body = "\n".join(_para_to_xhtml(p, md_dir, img_items) for p in paras) or "<p><br/></p>"
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd">
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>{html.escape(title)}</title></head>
<body><h2>{html.escape(title)}</h2>\n{body}\n</body></html>\n"""


def write_epub(path: Path, book_title: str, author: str, chapters: list[tuple[str, list[str]]],
               md_dir: Path, cover_path=None) -> None:
    book_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    manifest, spine = [], []
    cover_href = None
    if cover_path is not None and Path(cover_path).exists():
        cover_href = "cover" + os.path.splitext(str(cover_path))[1].lower()
        manifest.append(f'<item id="cover" href="{cover_href}" media-type="{_epub_mime(cover_href)}"/>')
        spine.append('<itemref idref="coverpage"/>')
    for i in range(len(chapters)):
        manifest.append(f'<item id="c{i}" href="ch{i:04d}.xhtml" media-type="application/xhtml+xml"/>')
        spine.append(f'<itemref idref="c{i}"/>')
    cover_meta = '<meta name="cover" content="cover"/>' if cover_href else ""
    guide = ('<guide><reference type="cover" title="Cover" href="cover.xhtml"/></guide>'
             if cover_href else "")
    if cover_href:
        manifest.append('<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>')
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="2.0">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>{html.escape(book_title)}</dc:title>
<dc:creator>{html.escape(author or 'Foxaholic')}</dc:creator>
<dc:language>en</dc:language><dc:identifier id="bookid">urn:uuid:{book_id}</dc:identifier>
<dc:date>{now}</dc:date>{cover_meta}</metadata>
<manifest>{''.join(manifest)}<item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/></manifest>
<spine toc="ncx">{''.join(spine)}</spine>{guide}</package>\n"""
    ncx_items = "".join(
        f'<navPoint id="np{i}" playOrder="{i+1}"><navLabel><text>{html.escape(t)}</text></navLabel>'
        f'<content src="ch{i:04d}.xhtml"/></navPoint>'
        for i, (t, _) in enumerate(chapters)
    )
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1"><head>
<meta name="dtb:uid" content="urn:uuid:{book_id}"/></head>
<docTitle><text>{html.escape(book_title)}</text></docTitle>
<navMap>{ncx_items}</navMap></ncx>\n"""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml",
                   '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
                   "</rootfiles></container>")
        z.writestr("OEBPS/toc.ncx", ncx)
        img_items: dict = {}
        pages = []
        if cover_href:
            cover_xhtml = ("<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
                           '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
                           f"<title>Cover</title></head><body><div style=\"text-align:center\">"
                           f"<img src=\"{html.escape(cover_href)}\" alt=\"cover\"/></div></body></html>\n")
            pages.append(("OEBPS/cover.xhtml", cover_xhtml))
            img_items[cover_href] = Path(cover_path).read_bytes()
        for i, (t, paras) in enumerate(chapters):
            pages.append((f"OEBPS/ch{i:04d}.xhtml", _xhtml_page(t, paras, md_dir, img_items)))
        for name, data in pages:
            z.writestr(name, data)
        # image files + manifest entries (collected while rendering pages;
        # the cover is already declared above, so skip it here)
        img_manifest = []
        for n, (rel, data) in enumerate(img_items.items()):
            z.writestr("OEBPS/" + rel, data)
            if rel == cover_href:
                continue
            img_manifest.append(
                f'<item id="img{n}" href="{html.escape(rel)}" media-type="{_epub_mime(rel)}"/>')
        opf = opf.replace("</manifest>", "".join(img_manifest) + "</manifest>")
        z.writestr("OEBPS/content.opf", opf)


def write_pdf(path: Path, book_title: str, chapters: list[tuple[str, list[str]]],
              meta: dict | None = None, cover_path=None) -> None:
    """Combined PDF via fpdf2. Uses a system TTF so smart quotes etc. survive."""
    from fpdf import FPDF

    regular = bold = None
    for cand_r, cand_b in [
        (r"C:\Windows\Fonts\arial.ttf", r"C:\Windows\Fonts\arialbd.ttf"),
        (r"C:\Windows\Fonts\calibri.ttf", r"C:\Windows\Fonts\calibrib.ttf"),
        ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
         "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ]:
        if Path(cand_r).exists() and Path(cand_b).exists():
            regular, bold = cand_r, cand_b
            break

    pdf = FPDF()
    pdf.set_auto_page_break(True, margin=20)
    if regular:
        pdf.add_font("body", "", regular)
        pdf.add_font("body", "B", bold)
        font = "body"
    else:  # last resort: latin-1 core font, strip the rest
        font = "helvetica"

    # CJK fallback (cultivation novels sprinkle in Chinese chars U+4E00..)
    for cjk_cand in (r"C:\Windows\Fonts\malgun.ttf",
                     "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        if Path(cjk_cand).exists():
            try:
                pdf.add_font("cjk", "", cjk_cand)
                pdf.set_fallback_fonts(["cjk"])
                break
            except Exception:
                pass

    def _tx(s: str) -> str:
        return s if regular else s.encode("latin-1", "replace").decode("latin-1")

    def _pdf_image(img_path: Path) -> bool:
        try:
            if not img_path.exists():
                return False
            w = pdf.epw * 0.85
            pdf.image(str(img_path), x=pdf.l_margin + (pdf.epw - w) / 2, w=w)
            pdf.ln(4)
            return True
        except Exception:
            pass
        try:  # convert (e.g. webp) via Pillow, retry as PNG
            from PIL import Image as PILImage
            im = PILImage.open(img_path).convert("RGB")
            tmp = img_path.with_suffix(img_path.suffix + ".pdf.png")
            im.save(tmp, "PNG")
            w = pdf.epw * 0.85
            pdf.image(str(tmp), x=pdf.l_margin + (pdf.epw - w) / 2, w=w)
            pdf.ln(4)
            try:
                tmp.unlink()
            except Exception:
                pass
            return True
        except Exception:
            return False

    book_dir = Path(path).parent
    md_dir = book_dir / "md"
    pdf.add_page()
    pdf.set_font(font, "B", 20)
    pdf.multi_cell(0, 10, _tx(book_title), align="C")
    if meta:
        if cover_path is not None and Path(cover_path).exists():
            try:
                w = pdf.epw * 0.5
                pdf.image(str(cover_path), x=pdf.l_margin + (pdf.epw - w) / 2, w=w)
                pdf.ln(4)
            except Exception:
                pass
        pdf.set_font(font, "", 11)
        info = []
        if meta.get("original_title"):
            info.append(f"Original title: {meta['original_title']}")
        if meta.get("author"):
            info.append(f"Author: {', '.join(meta['author'])}")
        if meta.get("team"):
            info.append(f"Team: {', '.join(meta['team'])}")
        if meta.get("status"):
            info.append(f"Status: {meta['status']}")
        if meta.get("translation"):
            info.append(f"Translation: {meta['translation']}")
        if meta.get("type"):
            info.append(f"Type: {meta['type']}")
        if meta.get("genres"):
            info.append(f"Genres: {', '.join(meta['genres'])}")
        if meta.get("tags"):
            info.append(f"Tags: {', '.join(meta['tags'][:16])}")
        info.append(f"Chapters: {len(chapters)}")
        for line in info:
            pdf.multi_cell(0, 6, _tx(line), align="C", new_x="LMARGIN")
        if meta.get("synopsis"):
            pdf.ln(4)
            pdf.set_font(font, "B", 13)
            pdf.cell(0, 8, "Synopsis")
            pdf.ln(10)
            pdf.set_font(font, "", 11)
            for p in meta["synopsis"]:
                pdf.multi_cell(0, 6, _tx(p))
                pdf.ln(2)
    pdf.ln(10)
    for t, paras in chapters:
        pdf.add_page()
        pdf.set_font(font, "B", 14)
        pdf.multi_cell(0, 8, _tx(t))
        pdf.ln(2)
        pdf.set_font(font, "", 11)
        for p in paras:
            m = _IMG_MD_RE.fullmatch(p.strip())
            if m:
                if not _pdf_image(md_dir / m.group(2)):
                    pdf.multi_cell(0, 6, _tx(f"[Image: {m.group(2)}]"))
                    pdf.ln(2)
                continue
            for line in p.split("\n"):
                pdf.multi_cell(0, 6, _tx(line) if line.strip() else " ")
            pdf.ln(2)
    pdf.output(str(path))


# ---------------------------------------------------------------- main flow


# ------------------------------------------------------- threaded fetching

_tls = threading.local()


def _strip_tags(s: str) -> str:
    return html.unescape(re.sub(r"(?s)<[^>]+>", "", s)).strip()


def _title_from_html(html_text: str) -> str:
    m = re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html_text)
    if m:
        t = _strip_tags(m.group(1))
        if t:
            return t
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html_text)
    if m:
        return re.sub(r"\s*[-|]\s*Foxaholic.*$", "", _strip_tags(m.group(1))).strip()
    return ""


def _content_frag(html_text: str) -> str:
    """Inner HTML of the chapter reading container (balanced-div scan, no deps).

    `text-left` first: on Foxaholic the outer `reading-content` div also holds
    breadcrumb/title header blocks, while `text-left` holds the story itself.
    """
    for cls in ("text-left", "reading-content", "entry-content", "chapter-content", "text-content", "post-content"):
        m = re.search(r"<div[^>]*class=[\"'][^\"']*" + cls + r"[^\"']*[\"'][^>]*>", html_text)
        if not m:
            continue
        start = m.end()
        depth = 1
        for dm in re.finditer(r"</?div\b[^>]*>", html_text[start:]):
            if dm.group(0).startswith("</"):
                depth -= 1
            else:
                depth += 1
            if depth == 0:
                return html_text[start:start + dm.start()]
    return html_text


_known_gens: list = []


def _thread_session(cookies: list, ua: str):
    import requests

    if id(cookies) not in [id(g) for g in _known_gens]:
        _known_gens.append(cookies)  # keep alive so ids aren't recycled
    s = getattr(_tls, "sess", None)
    if s is None or getattr(_tls, "gen", None) != id(cookies):
        s = requests.Session()
        s.headers.update({
            "User-Agent": ua or DEFAULT_UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.foxaholic.com/",
        })
        for c in cookies or []:
            try:
                kw = {"domain": c["domain"]} if c.get("domain") else {}
                s.cookies.set(c["name"], c["value"], **kw)
            except Exception:
                pass
        _tls.sess = s
        _tls.gen = id(cookies)
    return s


def _cf_html(body: str) -> bool:
    return "challenges.cloudflare.com" in body and "Just a moment" in body


def _download_image(sess, url: str, book_dir, idx: int, n: int):
    """Save an illustration to images/. Returns rel path or None."""
    try:
        if sess is None:
            return None
        r = sess.get(url, timeout=30)
        if r is None or r.status_code != 200:
            return None
        ctype = (r.headers.get("Content-Type", "") or "").split(";")[0].strip().lower()
        if ctype and not ctype.startswith("image/"):
            return None
        data = r.content
        if not data or len(data) < 512:
            return None
        ext = os.path.splitext(urlparse(url).path)[1].lower().lstrip(".")
        if ext not in ("jpg", "jpeg", "png", "gif", "webp", "bmp"):
            ext = {"image/jpeg": "jpg", "image/png": "png", "image/gif": "gif",
                   "image/webp": "webp", "image/bmp": "bmp"}.get(ctype, "jpg")
        imgdir = Path(book_dir) / "images"
        imgdir.mkdir(parents=True, exist_ok=True)
        rel = f"images/chapter-{idx:04d}-{n:02d}.{ext}"
        dest = Path(book_dir) / rel
        if not dest.exists() or dest.stat().st_size < 512:
            dest.write_bytes(data)
        return rel
    except Exception:
        return None


def _process_chapter_html(html_text: str, page_url: str, idx: int, list_name: str,
                           novel_title: str, book_dir, cookies, ua):
    """Parse chapter HTML -> (short title, lines). Downloads illustrations.

    `lines` mixes text paragraphs and `![illustration](images/...)` entries.
    Writes the chapter md (name line + body). Returns (None, reason) on failure.
    """
    h1short = _short_chapter_name(_clean_name(_title_from_html(html_text)), novel_title)
    ctitle = _best_title(h1short, _clean_name(list_name), idx)
    blocks = _extract_content(_content_frag(html_text), page_url)
    if sum(1 for k, _ in blocks if k == "p") < 2:
        return (None, "only %d paragraph(s)" % sum(1 for k, _ in blocks if k == "p"))
    try:
        sess = _thread_session(cookies, ua)
    except ImportError:
        sess = None
    lines: list[str] = []
    img_no = 0
    md_dir = Path(book_dir) / "md"
    md_dir.mkdir(parents=True, exist_ok=True)
    for kind, val in blocks:
        if kind == "p":
            lines.append(val)
            continue
        img_no += 1
        rel = _download_image(sess, val, md_dir, idx, img_no)
        if rel:
            lines.append(f"![illustration]({rel})")
        else:
            lines.append(f"[Image unavailable: {val}]")
    md_path = _md_path(book_dir, idx)
    md_path.write_text(f"{ctitle}\n\n" + "\n\n".join(lines) + "\n", encoding="utf-8")
    return (ctitle, lines)


def _fetch_chapter_thread(url, idx, name, book_dir_s, cookies, ua, delay, novel_title=""):
    """Fetch+parse+save one chapter. Returns (idx, title, lines) or (idx, None, reason)."""
    md_path = _md_path(book_dir_s, idx)
    if md_path.exists() and md_path.stat().st_size > 500:
        try:
            paras = _read_cached_md(md_path, name)
            if sum(1 for p in paras if not _IMG_MD_RE.search(p)) >= 2:
                return (idx, name or f"Chapter {idx}", paras)
        except Exception:
            pass
    try:
        sess = _thread_session(cookies, ua)
        r = sess.get(url, timeout=30)
        body = r.text if r is not None else ""
        if r is None or r.status_code != 200 or _cf_html(body):
            return (idx, None, f"http blocked (status={getattr(r, 'status_code', '?')})")
        res = _process_chapter_html(body, url, idx, name, novel_title, Path(book_dir_s), cookies, ua)
        if res[0] is None:
            return (idx, None, res[1])
        if delay > 0:
            time.sleep(delay)
        return (idx, res[0], res[1])
    except Exception as e:
        return (idx, None, str(e)[:160])


def _download_threaded(page, selected, lo, total, book_dir, novel_title, get_cookies, ua, args):
    """Concurrent download reusing browser Cloudflare cookies, with session-refresh retries.

    Failures are retried with a freshly solved session (up to --refresh-retries
    rounds); leftovers land in failed.json for the next run. Returns ordered
    [(title, lines)] (possibly partial), or None if nothing could be fetched at
    all (caller falls back to sequential browser mode).
    """
    import concurrent.futures

    max_rounds = 1 + max(0, getattr(args, "refresh_retries", 2))
    pending = [(i, ch) for i, ch in enumerate(selected, start=lo + 1)]
    results: dict = {}
    for rnd in range(max_rounds):
        try:
            cookies = get_cookies() or []
        except Exception:
            cookies = []
        if rnd > 0:
            print(f"[workers] refreshing Cloudflare session (round {rnd + 1}/{max_rounds}) ...")
            try:
                page.goto(args.url, wait_until="domcontentloaded")
                page.wait_for_timeout(3000)
                if not wait_for_cloudflare(page, timeout=60, headed=args.headed):
                    print("  ! challenge did not clear this round")
                try:
                    cookies = get_cookies() or []
                except Exception:
                    pass
            except Exception as e:
                print(f"  ! refresh error: {e}")
            time.sleep(2)
            print(f"[workers] retrying {len(pending)} failed chapter(s) with fresh session")
        workers = max(1, min(args.workers, 100))
        if rnd == 0:
            print(f"[workers] {workers} threads, {len(pending)} chapter(s)")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(_fetch_chapter_thread, ch["url"], i, ch["name"],
                              str(book_dir), cookies, ua, args.delay, novel_title): (i, ch)
                    for i, ch in pending}
            done = 0
            for fut in concurrent.futures.as_completed(futs):
                i, ch = futs[fut]
                try:
                    res = fut.result()
                except Exception as e:
                    res = (i, None, str(e)[:160])
                results[i] = res
                done += 1
                if res[1] is None:
                    print(f"  [{i}/{total}] ! {res[2]}")
                elif (done % 25 == 0 or done == len(pending)) and len(pending) > 5:
                    print(f"  [workers] round {rnd + 1}: {done}/{len(pending)} done")
        pending = [(i, selected[i - lo - 1]) for i in sorted(results) if results[i][1] is None]
        ok = len(selected) - len(pending)
        print(f"  [workers] round {rnd + 1} done: {ok}/{len(selected)} ok, {len(pending)} failed")
        if not pending:
            break

    fj = Path(book_dir) / "failed.json"
    if pending:
        fj.write_text(json.dumps({"failed": [i for i, _ in pending],
                                  "urls": [ch["url"] for _, ch in pending],
                                  "at": datetime.now(timezone.utc).isoformat()},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[workers] {len(pending)} chapter(s) still failing -> listed in {fj}")
    else:
        try:
            if fj.exists():
                fj.unlink()
        except Exception:
            pass

    if not results or all(r[1] is None for r in results.values()):
        return None
    ordered = []
    for i in sorted(results):
        _i, t, p = results[i]
        if t is not None:
            ordered.append((t, p if isinstance(p, list) else []))
    return ordered


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Download a Foxaholic novel to TXT/MD/EPUB (handles Cloudflare via real browser).")
    p.add_argument("url", help="Novel URL, e.g. https://www.foxaholic.com/novel/<slug>/")
    p.add_argument("-o", "--output", default="out", help="Output root folder (default: out)")
    p.add_argument("--from", dest="ch_from", type=int, default=None, help="First chapter number (1-based position in list)")
    p.add_argument("--to", dest="ch_to", type=int, default=None, help="Last chapter number inclusive")
    p.add_argument("--delay", type=float, default=1.2, help="Delay seconds between chapters (default 1.2, be polite)")
    p.add_argument("--workers", type=int, default=1, help="Parallel chapter fetchers 1-100 (default 1 = sequential browser mode; try 4). Browser still solves Cloudflare; workers reuse its cookies.")
    p.add_argument("--refresh-retries", type=int, default=2, help="How many times to re-solve Cloudflare and retry failed chapters (default 2)")
    p.add_argument("--format", default="all", help="Comma list: epub,txt,md,pdf,all (default all)")
    p.add_argument("--headed", action="store_true", help="Show browser window (solve Cloudflare manually if it won't clear headless)")
    p.add_argument("--uc", action="store_true", help=argparse.SUPPRESS)  # deprecated: SB is always used now
    p.add_argument("--user-data-dir", default=None, help=argparse.SUPPRESS)  # deprecated: SB uses a fresh profile
    p.add_argument("--list-only", action="store_true", help="Only list chapters, download nothing")
    p.add_argument("--timeout", type=int, default=120, help="Cloudflare wait timeout seconds (default 120)")
    p.add_argument("--chapter-timeout", type=int, default=45000, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    args = parse_args(argv)

    parsed = urlparse(args.url)
    if "foxaholic.com" not in parsed.netloc:
        print("ERROR: URL must be on foxaholic.com (www or 18 subdomain).", file=sys.stderr)
        return 2

    fmts = {f.strip().lower() for f in args.format.split(",")}
    if "all" in fmts:
        fmts = {"epub", "txt", "md", "pdf"}

    script_dir = Path(__file__).resolve().parent

    try:
        page_ctx = _open_page(args, script_dir)
    except ImportError as e:
        print(str(e), file=sys.stderr)
        return 2

    with page_ctx as (page, _close, _get_cookies, _ua):
        print(f"[novel] opening {args.url}")
        page.goto(args.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        if not wait_for_cloudflare(page, timeout=args.timeout, headed=args.headed):
            print("ERROR: Cloudflare challenge did not clear. Re-run with --headed and solve it manually once.", file=sys.stderr)
            _close()
            return 3

        meta = extract_novel_meta(page)
        title = meta["title"] or slug_from_url(args.url)
        print(f"[novel] title: {title}")
        if meta["original_title"]:
            print(f"[novel] original title: {meta['original_title']}")
        print(f"[novel] author: {', '.join(meta['author']) or '?'}"
              f" | team: {', '.join(meta['team']) or '?'}"
              f" | status: {meta['status'] or '?'}"
              f" | translation: {meta['translation'] or '?'}"
              f" | type: {meta['type'] or '?'}")
        if meta["genres"]:
            print(f"[novel] genres: {', '.join(meta['genres'])}")
        if meta["tags"]:
            print(f"[novel] tags: {', '.join(meta['tags'][:12])}"
                  f"{'...' if len(meta['tags']) > 12 else ''}")
        if meta["synopsis"]:
            print(f"[novel] synopsis: {meta['synopsis'][0][:240]}"
                  f"{'...' if len(meta['synopsis'][0]) > 240 else ''}")
        chapters = extract_chapter_links(page, args.url)
        print(f"[novel] found {len(chapters)} chapter link(s)")
        if not chapters:
            try:
                page.screenshot(path=str(script_dir / "debug_novel.png"))
                print("[debug] saved debug_novel.png - page structure not recognised, selectors may need updating.")
            except Exception:
                pass
            _close()
            return 4

        # chapter range (positions, 1-based)
        lo = (args.ch_from or 1) - 1
        hi = args.ch_to or len(chapters)
        selected = chapters[max(lo, 0):hi]
        print(f"[novel] selected {len(selected)} chapter(s) [{lo+1}..{hi}]")

        if args.list_only:
            for i, c in enumerate(selected, start=lo + 1):
                print(f"  {i:4d}. {c['name'] or '(no name)'}  -> {c['url']}")
            _close()
            return 0

        book_dir = Path(args.output) / safe_filename(title)
        book_dir.mkdir(parents=True, exist_ok=True)
        (book_dir / "md").mkdir(parents=True, exist_ok=True)
        # migrate old flat layout (chapter-*.md + images/ at top level) into md/
        try:
            old_img = book_dir / "images"
            new_img = book_dir / "md" / "images"
            if old_img.is_dir() and not new_img.exists():
                old_img.rename(new_img)
        except Exception:
            pass
        (book_dir / "metadata.json").write_text(
            json.dumps({**meta, "url": args.url, "chapter_count": len(selected),
                        "downloaded_at": datetime.now(timezone.utc).isoformat()},
                       ensure_ascii=False, indent=2),
            encoding="utf-8")

        # cover image for the EPUB/PDF title pages
        cover_path = None
        if meta.get("cover_url"):
            try:
                csess = _thread_session(_get_cookies() or [], _ua)
                cr = csess.get(meta["cover_url"], timeout=30)
                ctype = ((cr.headers.get("Content-Type", "") if cr else "") or "").split(";")[0].strip().lower()
                if cr is not None and cr.status_code == 200 and ctype.startswith("image/") and cr.content:
                    ext = os.path.splitext(urlparse(meta["cover_url"]).path)[1].lower()
                    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                        ext = ".jpg"
                    cover_path = book_dir / f"cover{ext}"
                    cover_path.write_bytes(cr.content)
                    print(f"[novel] cover: {cover_path.name}")
            except Exception as e:
                print(f"[novel] cover download failed: {e}")

        collected: list[tuple[str, list[str]]] = []
        thr = None
        if args.workers > 1:
            thr = _download_threaded(page, selected, lo, len(chapters), book_dir, title,
                                     _get_cookies, _ua, args)
            if thr is None:
                print("[workers] cookie session blocked, falling back to sequential browser mode")
        if thr is not None:
            collected = thr
        else:
            for idx, ch in enumerate(selected, start=lo + 1):
                md_path = _md_path(book_dir, idx)
                if md_path.exists() and md_path.stat().st_size > 500:
                    print(f"  [{idx}/{len(chapters)}] skip (cached): {ch['name']}")
                    try:
                        paras = _read_cached_md(md_path, ch["name"])
                        collected.append((ch["name"] or f"Chapter {idx}", paras))
                    except Exception:
                        pass
                    continue
                print(f"  [{idx}/{len(chapters)}] GET {ch['url']}")
                try:
                    page.goto(ch["url"], wait_until="domcontentloaded")
                    page.wait_for_timeout(2500)
                    if is_cloudflare_challenge(page):
                        if not wait_for_cloudflare(page, timeout=60, headed=args.headed):
                            print("    ! CF re-challenge, skipping chapter")
                            continue
                    try:
                        bcookies = _get_cookies() or []
                    except Exception:
                        bcookies = []
                    res = _process_chapter_html(page.content(), ch["url"], idx, ch["name"],
                                                title, book_dir, bcookies, _ua)
                    if res[0] is None:
                        print(f"    ! warning: {res[1]}, site layout may have changed")
                        continue
                    collected.append((res[0], res[1]))
                except Exception as e:
                    print(f"    ! error: {e}")
                time.sleep(max(0, args.delay))

        _close()

    if not collected:
        print("ERROR: nothing downloaded (all chapters failed).", file=sys.stderr)
        return 5

    base = safe_filename(title)
    info_lines = [title]
    if meta.get("original_title"):
        info_lines.append(f"Original title: {meta['original_title']}")
    if meta.get("author"):
        info_lines.append(f"Author: {', '.join(meta['author'])}")
    if meta.get("team"):
        info_lines.append(f"Team: {', '.join(meta['team'])}")
    if meta.get("status"):
        info_lines.append(f"Status: {meta['status']}")
    if meta.get("translation"):
        info_lines.append(f"Translation: {meta['translation']}")
    if meta.get("type"):
        info_lines.append(f"Type: {meta['type']}")
    if meta.get("genres"):
        info_lines.append(f"Genres: {', '.join(meta['genres'])}")
    if meta.get("tags"):
        info_lines.append(f"Tags: {', '.join(meta['tags'])}")
    info_lines.append(f"Chapters: {len(collected)}")
    info_lines.append(f"Source: {args.url}")
    if "txt" in fmts:
        txt_path = book_dir / f"{base}.txt"
        with txt_path.open("w", encoding="utf-8") as f:
            f.write("\n".join(info_lines) + "\n")
            if meta.get("synopsis"):
                f.write("\nSynopsis:\n" + "\n\n".join(meta["synopsis"]) + "\n")
            for t, paras in collected:
                f.write(f"\n\n{'='*20} {t} {'='*20}\n\n")
                f.write("\n\n".join(_IMG_MD_RE.sub(r"[Image: \2]", p) for p in paras))
        print(f"[done] TXT : {txt_path}")
    if "epub" in fmts:
        epub_path = book_dir / f"{base}.epub"
        author = ", ".join(meta.get("author") or []) or "Foxaholic"
        write_epub(epub_path, title, author, collected, book_dir / "md", cover_path)
        print(f"[done] EPUB: {epub_path}")
    if "pdf" in fmts:
        try:
            pdf_path = book_dir / f"{base}.pdf"
            write_pdf(pdf_path, title, collected, meta, cover_path)
            print(f"[done] PDF : {pdf_path}")
        except ImportError:
            print("[done] PDF skipped: fpdf2 not installed (pip install fpdf2)", file=sys.stderr)
        except Exception as e:
            print(f"[done] PDF failed: {e}", file=sys.stderr)
    if "md" in fmts:
        print(f"[done] MD  : {book_dir}/chapter-*.md ({len(collected)} files)")
    print(f"[done] {len(collected)} chapter(s) saved to {book_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
