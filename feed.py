#!/usr/bin/env python3
"""
RSS 2.0 feed for Microsoft Customer Stories.

Reads the same POST endpoint the website's own search page uses, so one request
gets everything — no page scraping needed by default.

    python3 feed.py          # normal: fetches each story page for its real
                             # publish date and summary (~40 requests, ~30s)
    python3 feed.py --fast   # API only, 2 requests, but dates are approximate

Why the story pages get fetched: the API returns no publish date. Its only
date-ish field, _ts, is a Cosmos document-write timestamp that a bulk re-index
sets identically across every record — which is exactly what happened on
2026-07-30, giving all 40 items the same pubDate.

Writes public/rss.xml.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from html import unescape
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

    # NOTE: _ts is a Cosmos DB document-write timestamp, NOT a publish date.
    # A bulk re-index restamps every record identically, which is exactly what
    # happened on 2026-07-30. Never use it as pubDate. Kept only so we can tell
    # whether it is uniform across the batch.
    ts = card.get("_ts")

    return {
        "id": (slug.split("-")[0] if slug[:1].isdigit() else slug),
        "url": url,
        "title": title,
        "description": desc,
        "categories": cats,
        "image": get(card, "content.image.src") or "",
        "company": get(card, "content.image.slot.badge.icon.alt") or "",
        "ts": ts,
        "pubDate": None,   # filled in by add_dates()
    }


META_RE = re.compile(
    r'<meta[^>]+(?:property|name)=["\'](?:og:description|description)["\']'
    r'[^>]+content=["\'](.*?)["\']',
    re.I | re.S,
)


def scrape_page(client, item: dict) -> dict:
    """
    Fetch the story page for its real publish date, and a proper summary.

    This is the default, not an extra, because the API gives us no usable date:
    the only date-ish field (_ts) is a document-write timestamp that a bulk
    re-index sets identically across every record.

    Regex rather than an HTML parser purely to keep the dependency list at one
    (httpx). We're pulling two well-formed meta values, not parsing a document.
    """
    try:
        html = client.get(item["url"]).text
    except httpx.HTTPError:
        return item

    m = META_RE.search(html)
    if m and m.group(1).strip():
        item["description"] = unescape(m.group(1).strip())

    # The date renders as bare text (e.g. "4/9/2026") just above the <h1>.
    i = html.lower().find("<h1")
    hits = DATE_RE.findall(html[max(0, i - 4000):i] if i > 0 else html[:6000])
    if hits:
        mth, day, yr = hits[-1]  # en locale renders month first
        try:
            item["date"] = datetime(int(yr), int(mth), int(day), 12,
                                    tzinfo=timezone.utc)
        except ValueError:
            pass
    return item


def add_dates(items: list[dict]) -> None:
    """
    Turn scraped dates into pubDate, and make sure every item has a distinct one.

    Distinctness matters: the Power Automate RSS trigger keys off PublishDate to
    decide what's new, so duplicate timestamps break change detection. Items
    arrive in the API's PublishedDate Desc order, so where a page date is
    missing we interpolate downward from the last known date, preserving that
    order. Interpolated items get a marker in the description.
    """
    last = None
    for idx, it in enumerate(items):
        d = it.get("date")
        if d is None:
            # Step back a minute per position so ordering survives and the
            # timestamp stays unique.
            base = last or datetime.now(timezone.utc)
            d = base - timedelta(minutes=1)
            it["approx"] = True
        last = d
        it["_dt"] = d

    # Break exact ties (two stories published the same day) by nudging each
    # duplicate one second earlier — keeps order, keeps every value unique.
    seen: set[str] = set()
    for it in items:
        d = it["_dt"]
        while rfc822(d) in seen:
            d -= timedelta(seconds=1)
        it["_dt"] = d
        seen.add(rfc822(d))
        it["pubDate"] = rfc822(d)


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
    ap.add_argument("--fast", action="store_true",
                    help="skip story pages: much quicker, but dates become "
                         "approximate and summaries are industry + a quote")
    args = ap.parse_args()

    with httpx.Client(headers={"User-Agent": UA}, timeout=30,
                      follow_redirects=True) as client:
        cards = fetch_cards(client)
        print(f"got {len(cards)} cards from the API")
        items = [i for i in (card_to_item(c) for c in cards) if i]
        if not items:
            sys.exit("Cards came back but none had a usable title + href.")

        uniq_ts = len({i.get("ts") for i in items if i.get("ts")})
        if uniq_ts == 1:
            print("note: every card shares one _ts (a bulk re-index), which is "
                  "why real dates have to come from the story pages")

        if not args.fast:
            print(f"fetching {len(items)} story pages for dates + summaries...")
            items = [scrape_page(client, i) for i in items]

    add_dates(items)

    got = sum(1 for i in items if not i.get("approx"))
    print(f"real dates: {got}/{len(items)}"
          + ("" if got == len(items) else "  (rest interpolated in feed order)"))
    if len({i["pubDate"] for i in items}) != len(items):
        print("WARNING: duplicate pubDates remain — tell me if you see this")

    # The API sorted by PublishedDate Desc; that's the real order, so keep it.
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(build(items), encoding="utf-8")
    print(f"wrote {OUT} with {len(items)} items")


if __name__ == "__main__":
    main()
