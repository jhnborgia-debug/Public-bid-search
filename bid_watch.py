#!/usr/bin/env python3
"""
Bid Watcher: scans government bid pages for security-system solicitations
and emails you only the NEW ones each run.

Usage:
    python bid_watch.py             # scan + email new matches
    python bid_watch.py --dry-run   # scan + print, no email, nothing saved
    python bid_watch.py --baseline  # first run: record everything, send no email

Email settings come from environment variables:
    SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS
    EMAIL_TO (optional; defaults to John.borgia@cyberhomessystems.com)
"""
import argparse
import json
import os
import re
import smtplib
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO
from datetime import datetime
from email.message import EmailMessage
from html import escape
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
SOURCES_FILE = HERE / "sources.json"
DB_FILE = HERE / "seen_bids.db"

# Edit freely. Case-insensitive regex fragments.
KEYWORDS = [
    r"security system", r"access control", r"cctv", r"video surveillance",
    r"surveillance", r"security camera", r"camera system", r"intrusion",
    r"alarm system", r"alarm monitoring", r"door entry", r"intercom",
    r"visitor management", r"badge", r"card reader", r"electronic security",
    r"physical security", r"security upgrade", r"video management",
]
KEYWORD_RE = re.compile("|".join(KEYWORDS), re.I)

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; BidWatcher/1.0)"}


def fetch(url, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last = e
            time.sleep(2 * (i + 1))
    raise last


def clean(text, limit=220):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


PDF_SCAN = True  # set False to skip reading PDF text (faster)
BID_HINT_RE = re.compile(r"bid|proposal|rfp|rfq|advertis|solicitation|notice", re.I)
_pdf_lock = None


def pdf_matches(url):
    """Download a PDF once, check its first pages for keywords. Cached in the DB."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return False
    con = sqlite3.connect(DB_FILE, timeout=30)
    row = con.execute("SELECT hit FROM pdf_checked WHERE url=?", (url,)).fetchone()
    if row is not None:
        con.close()
        return bool(row[0])
    hit = 0
    try:
        r = requests.get(url, headers=HEADERS, timeout=40)
        r.raise_for_status()
        if len(r.content) < 15_000_000:
            reader = PdfReader(BytesIO(r.content))
            text = " ".join((p.extract_text() or "") for p in reader.pages[:6])
            hit = 1 if KEYWORD_RE.search(text) else 0
    except Exception:
        con.close()
        return False  # don't cache failures; retry next run
    con.execute("INSERT OR REPLACE INTO pdf_checked VALUES (?,?)", (url, hit))
    con.commit()
    con.close()
    return bool(hit)


def scan_html(src):
    """Find links whose own text or table row/list item matches a keyword."""
    soup = BeautifulSoup(fetch(src["url"]), "html.parser")
    scope = soup.select_one(src["selector"]) if src.get("selector") else soup
    found = {}
    for a in (scope or soup).find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        row = a.find_parent(["tr", "li", "article"])
        context = row.get_text(" ", strip=True) if row else a.get_text(" ", strip=True)
        if KEYWORD_RE.search(context) or KEYWORD_RE.search(href):
            url = urljoin(src["url"], href)
            found.setdefault(url, clean(context))
        elif PDF_SCAN and href.lower().split("?")[0].endswith(".pdf") and BID_HINT_RE.search(context):
            # Many NJ towns post the bid ad as a PDF with a generic link title.
            url = urljoin(src["url"], href)
            if url not in found and pdf_matches(url):
                found[url] = clean(context) + " (matched inside PDF)"
    return [{"url": u, "title": t} for u, t in found.items()]


def scan_rss(src):
    root = ET.fromstring(fetch(src["url"]))
    out = []
    for item in root.iter("item"):
        title = item.findtext("title") or ""
        desc = item.findtext("description") or ""
        link = (item.findtext("link") or "").strip()
        if link and KEYWORD_RE.search(f"{title} {desc}"):
            out.append({"url": link, "title": clean(title)})
    return out


SCANNERS = {"html": scan_html, "rss": scan_rss}


def init_db():
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "CREATE TABLE IF NOT EXISTS seen (url TEXT PRIMARY KEY, title TEXT, "
        "source TEXT, first_seen TEXT)"
    )
    con.execute("CREATE TABLE IF NOT EXISTS pdf_checked (url TEXT PRIMARY KEY, hit INTEGER)")
    con.commit()
    return con


def build_email(new_by_source, errors):
    total = sum(len(v) for v in new_by_source.values())
    text, html = [], [f"<h2>{total} new security-system bid(s)</h2>"]
    for name, items in new_by_source.items():
        text.append(f"\n== {name} ==")
        html.append(f"<h3>{escape(name)}</h3><ul>")
        for it in items:
            text.append(f"- {it['title']}\n  {it['url']}")
            html.append(
                f'<li>{escape(it["title"])}<br>'
                f'<a href="{escape(it["url"])}">{escape(it["url"])}</a></li>'
            )
        html.append("</ul>")
    if errors:
        text.append("\nSources that failed (check manually):")
        html.append("<h3>Sources that failed</h3><ul>")
        for name, err in errors[:25]:
            text.append(f"- {name}: {err}")
            html.append(f"<li>{escape(name)}: {escape(str(err))}</li>")
        if len(errors) > 25:
            text.append(f"...and {len(errors) - 25} more")
            html.append(f"<li>...and {len(errors) - 25} more</li>")
        html.append("</ul>")
    return "\n".join(text), "\n".join(html), total


DEFAULT_RECIPIENT = "John.borgia@cyberhomessystems.com"


def send_email(subject, text, html):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ["SMTP_USER"]
    msg["To"] = os.environ.get("EMAIL_TO", DEFAULT_RECIPIENT)
    msg.set_content(text)
    msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(os.environ["SMTP_HOST"], int(os.environ.get("SMTP_PORT", 587))) as s:
        s.starttls()
        s.login(os.environ["SMTP_USER"], os.environ["SMTP_PASS"])
        s.send_message(msg)


def scan_source(src):
    return src, SCANNERS[src.get("type", "html")](src)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--baseline", action="store_true",
                    help="record current matches as seen without emailing")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    sources = [s for s in json.loads(SOURCES_FILE.read_text()) if not s.get("disabled")]
    con = init_db()
    new_by_source, errors, pending = {}, [], []

    def mark_seen():
        con.executemany("INSERT OR IGNORE INTO seen VALUES (?,?,?,?)", pending)
        con.commit()

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(scan_source, s): s for s in sources}
        for fut in as_completed(futures):
            src = futures[fut]
            try:
                _, hits = fut.result()
            except Exception as e:  # keep going if one site is down
                errors.append((src["name"], e))
                continue
            fresh = []
            for h in hits:
                if con.execute("SELECT 1 FROM seen WHERE url=?", (h["url"],)).fetchone():
                    continue
                fresh.append(h)
                pending.append((h["url"], h["title"], src["name"], datetime.now().isoformat()))
            if fresh:
                new_by_source[src["name"]] = fresh
    print(f"Scanned {len(sources)} sources; {len(errors)} failed.")

    if args.baseline:
        mark_seen()
        total = sum(len(v) for v in new_by_source.values())
        print(f"Baseline saved: {total} existing matches recorded, no email sent.")
        return
    if not new_by_source:
        print("No new bids.")
        if not args.dry_run:
            send_email(
                "Bid Watcher (NJ): no new security bids this week",
                f"Weekly scan complete: {len(sources)} sites checked, no new security-system bids found.\n"
                f"{len(errors)} site(s) could not be loaded.",
                f"<p>Weekly scan complete: {len(sources)} sites checked, no new security-system bids found.<br>"
                f"{len(errors)} site(s) could not be loaded.</p>",
            )
        return
    new_by_source = dict(sorted(new_by_source.items()))
    text, html, total = build_email(new_by_source, errors)
    if args.dry_run:
        print(text)
        return
    send_email(f"Bid Watcher (NJ): {total} new security bid(s)", text, html)
    mark_seen()  # only after the email actually went out
    print(f"Emailed {total} new bid(s), {len(errors)} source error(s).")


if __name__ == "__main__":
    sys.exit(main())
