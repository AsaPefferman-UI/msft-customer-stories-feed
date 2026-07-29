#!/usr/bin/env python3
"""
RSS 2.0 feed for Microsoft Customer Stories.

Reads the same POST endpoint the website's own search page uses, so one request
gets everything — no page scraping needed by default.

    python3 feed.py              # fast: 1-2 requests, everything from the API
    python3 feed.py --rich       # also fetch each story page for a real
                                 # summary and the displayed publish date
                                 # (slower: one request per story)

Writes public/rss.xml.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx

OUT = Path(os.environ.get("OUT", "public/rss.xml"))
SELF_URL = os.environ.get("SELF_URL", "https://example.pages.dev/rss.xml")
UA = os.environ.get("FEED_UA", "customer-stories-feed/1.0 (+you@example.com)")
LIMIT = int(os.environ.get("LIMIT", "40"))
PAGE = 24  # per request; the endpoint pages via skip/hasMorePages

API = "https://www.microsoft.com/msstoreapiprod/api/customerstoriessearch"
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "Referer": "https://www.microsoft.com/en-us/customers/search",
}
LOCALE = "en-ww"

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MONTHS = [None, "Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(20\d{2})\b")


def rfc822(dt: datetime) -> str:
    """RSS 2.0 requires RFC 822 dates. ISO 8601 breaks the Power Automate
    RSS trigger, which is a confusing failure to debug."""
    dt = dt.astimezone(timezone.utc)
    return (f"{DAYS[dt.weekday()]}, {dt.day:02d} {MONTHS[dt.month]} "
            f"{dt.year} {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d} +0000")


def url_locale(loc: str) -> str:
    """'en-ww' means English worldwide, which appears as /en/ in story URLs.
    Region-specific locales like en-us are used verbatim."""
    return loc.split("-")[0] if loc.lower().endswith("-ww") else loc


def get(d: dict, path: str, default=None):
    """Safe nested lookup: get(card, 'content.action.href')."""
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def fetch_cards(client) -> list[dict]:
    """Page through the endpoint until we have LIMIT cards."""
    cards, skip = [], 0
    while len(cards) < LIMIT:
        body = {"locale": LOCALE, "top": PAGE, "skip": skip,
                "orderBy": "PublishedDate Desc"}
        r = client.post(API, json=body, headers=HEADERS)
        if r.status_code != 200:
            sys.exit(f"API returned HTTP {r.status_code}\n{r.text[:400]}")
        data = r.json()
        batch = data.get("cards") or []
        if not batch:
            break
        cards.extend(batch)
        if not data.get("hasMorePages"):
            break
        skip += PAGE
    return cards[:LIMIT]


def card_to_item(card: dict) -> dict | None:
    # The href is just a slug ("26951-krones-azure-hpc"); card.name has the
    # same value, so use it as a fallback.
    slug = get(card, "content.action.href") or card.get("name")
    title = get(card, "content.title")
    if not slug or not title:
        return None

    loc = url_locale(get(card, "content.action.locale", LOCALE))
    url = f"https://www.microsoft.com/{loc}/customers/story/{slug}"

    # No summary field exists in the response. The eyebrow carries the industry
    # and the quotes carry real pull-quotes; together they make a usable
    # description. Use --rich to get the page's actual og:description instead.
    eyebrow = get(card, "content.eyebrow") or ""
    quotes = get(card, "content.quotes") or []
    quote = (quotes[0].get("text") or "").strip() if quotes else ""
    desc = " — ".join(p for p in (eyebrow, quote) if p) or title

    cats = [i["text"] for i in (get(card, "content.industries") or [])
            if isinstance(i, dict) and i.get("text")]
    cats += [p["label"] for p in
             (get(card, "content.footer.relatedProducts.products") or [])
             if isinstance(p, dict) and p.get("label")]

    ts = card.get("_ts")
    pub = rfc822(datetime.fromtimestamp(ts, timezone.utc)) if ts else None

    return {
        "id": (slug.split("-")[0] if slug[:1].isdigit() else slug),
        "url": url,
        "title": title,
        "description": desc,
        "categories": cats,
        "image": get(card, "content.image.src") or "",
        "company": get(card, "content.image.slot.badge.icon.alt") or "",
        "pubDate": pub,
    }


def enrich(client, item: dict) -> dict:
    """--rich only: fetch the story page for a real summary and the displayed
    date. selectolax is imported here so the default path needs only httpx."""
    from selectolax.parser import HTMLParser
    try:
        html = client.get(item["url"]).text
    except httpx.HTTPError:
        return item
    doc = HTMLParser(html)

    for attr, name in (("property", "og:description"), ("name", "description")):
        n = doc.css_first(f'meta[{attr}="{name}"]')
        if n and n.attributes.get("content"):
            item["description"] = n.attributes["content"].strip()
            break

    i = html.lower().find("<h1")
    hits = DATE_RE.findall(html[max(0, i - 4000):i] if i > 0 else html[:6000])
    if hits:
        mth, day, yr = hits[-1]  # en locale renders month first
        try:
            item["pubDate"] = rfc822(
                datetime(int(yr), int(mth), int(day), 12, tzinfo=timezone.utc))
        except ValueError:
            pass
    return item


def build(items: list[dict]) -> str:
    rss = ET.Element("rss", {"version": "2.0",
                             "xmlns:atom": "http://www.w3.org/2005/Atom"})
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Microsoft Customer Stories (unofficial)"
    ET.SubElement(ch, "link").text = "https://www.microsoft.com/en-us/customers"
    ET.SubElement(ch, "description").text = (
        "Unofficial feed of Microsoft customer success stories.")
    ET.SubElement(ch, "language").text = "en"
    ET.SubElement(ch, "atom:link", {"rel": "self", "href": SELF_URL,
                                    "type": "application/rss+xml"})
    for it in items:
        e = ET.SubElement(ch, "item")
        ET.SubElement(e, "title").text = it["title"]
        ET.SubElement(e, "link").text = it["url"]
        ET.SubElement(e, "description").text = it["description"]
        # isPermaLink="true" is what Power Automate dedupes on between polls.
        ET.SubElement(e, "guid", {"isPermaLink": "true"}).text = it["url"]
        if it.get("pubDate"):
            ET.SubElement(e, "pubDate").text = it["pubDate"]
        for c in it.get("categories", []):
            ET.SubElement(e, "category").text = c
        if it.get("image"):
            ET.SubElement(e, "enclosure", {"url": it["image"],
                                           "type": "image/jpeg",
                                           "length": "0"})
    ET.indent(rss, space="  ")
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            + ET.tostring(rss, encoding="unicode") + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rich", action="store_true",
                    help="fetch each story page for real summaries and dates")
    args = ap.parse_args()

    with httpx.Client(headers={"User-Agent": UA}, timeout=30,
                      follow_redirects=True) as client:
        cards = fetch_cards(client)
        print(f"got {len(cards)} cards from the API")
        items = [i for i in (card_to_item(c) for c in cards) if i]
        if not items:
            sys.exit("Cards came back but none had a usable title + href.")
        if args.rich:
            print(f"fetching {len(items)} story pages for summaries...")
            items = [enrich(client, i) for i in items]

    # The API sorted by PublishedDate Desc; that's the real order, so keep it.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(items), encoding="utf-8")
    print(f"wrote {OUT} with {len(items)} items")


if __name__ == "__main__":
    main()
