"""Scrape Mike Spear's active listings from his Compass agent profile.

Reads the `window.__AGENT_PROFILE__` JSON that Compass embeds in the profile
page and writes the "Listings" section to listings.json. Only the listings
section is read (`activeListingsProps`); the "Transactions" section
(`closedDealsProps`) is deliberately ignored, as are all other pages.

Stdlib only - no pip installs needed. Runs once per day on GitHub Actions
via .github/workflows/scrape-listings.yml, which commits any change back to
main so the Pages site picks it up. Safe to run by hand too.

Usage:
    python tools/scrape-listings.py
    python tools/scrape-listings.py --out listings.json --photo-size 1024x768
    python tools/scrape-listings.py --dry-run
"""

import argparse
import datetime
import gzip
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request
import zlib
import zoneinfo

PROFILE_URL = "https://www.compass.com/agents/mike-spear/"
SITE = "https://www.compass.com"
MARKER = "window.__AGENT_PROFILE__"
REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# Compass serves listing photos as /m/<hash>/<width>x<height>.jpg
PHOTO_RE = re.compile(r"^(https://www\.compass\.com/m/[0-9a-f]+/)[^/]+$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
}


def log(message: str) -> None:
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {message}", flush=True)


def fetch(url: str, timeout: int) -> str:
    """Download the profile page and return it as text."""
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read()
        encoding = (response.headers.get("Content-Encoding") or "").lower()
    if encoding == "gzip":
        raw = gzip.decompress(raw)
    elif encoding == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", errors="replace")


def extract_profile(html: str) -> dict:
    """Pull the JSON object assigned to window.__AGENT_PROFILE__."""
    marker = html.find(MARKER)
    if marker == -1:
        sys.exit(
            f"{MARKER} not found - Compass likely changed the page layout. "
            "Nothing was written."
        )
    brace = html.find("{", marker)
    if brace == -1:
        sys.exit(f"{MARKER} found but no JSON object followed it. Nothing was written.")
    try:
        profile, _ = json.JSONDecoder().raw_decode(html[brace:])
    except json.JSONDecodeError as exc:
        sys.exit(f"Could not parse {MARKER}: {exc}. Nothing was written.")
    return profile


def photo_url(listing: dict, size: str) -> str | None:
    """Best primary photo for a listing, resized via the Compass image path."""
    media = listing.get("media") or []
    if not media:
        return None
    # category 0 is the primary/exterior shot; fall back to the first image.
    primary = next((m for m in media if m.get("category") == 0), media[0])
    source = primary.get("thumbnailUrl") or primary.get("originalUrl")
    if not source:
        return None
    match = PHOTO_RE.match(source)
    return f"{match.group(1)}{size}.jpg" if match else source


def open_house(listing: dict) -> str | None:
    """First upcoming open house as "YYYY-MM-DDTHH:MM/HH:MM" in local time."""
    events = listing.get("openHouses") or []
    tz_name = (listing.get("location") or {}).get("timezone") or "America/Chicago"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = datetime.timezone.utc

    upcoming = []
    now = datetime.datetime.now(tz)
    for event in events:
        start_ms, end_ms = event.get("startTimeMillis"), event.get("endTimeMillis")
        if not start_ms or not end_ms:
            continue
        start = datetime.datetime.fromtimestamp(start_ms / 1000, tz)
        end = datetime.datetime.fromtimestamp(end_ms / 1000, tz)
        if end >= now:
            upcoming.append((start, end))

    if not upcoming:
        return None
    start, end = min(upcoming)
    return f"{start:%Y-%m-%dT%H:%M}/{end:%H:%M}"


def convert(listing: dict, *, rental: bool, size: str) -> dict:
    """Map one Compass listing onto the listings.json schema."""
    location = listing.get("location") or {}
    dimensions = listing.get("size") or {}
    price = listing.get("price") or {}

    area = location.get("neighborhood")
    if not area:
        for key in ("mlsNeighborhoods", "subdivisionNames"):
            values = location.get(key) or []
            if values:
                area = values[0]
                break
    link = listing.get("canonicalPageLink") or listing.get("pageLink")

    record = {
        "address": location.get("prettyAddress"),
        "area": area or location.get("city"),
        "beds": dimensions.get("bedrooms"),
        "baths": dimensions.get("bathrooms"),
        "sqft": dimensions.get("squareFeet"),
        # lastKnown is the current ask (what the page shows); listed is the
        # original price, which differs once there has been a price cut.
        "price": price.get("lastKnown") or price.get("listed"),
        "status": listing.get("localizedStatus"),
        "url": f"{SITE}{link}" if link else None,
    }
    if rental:
        record["rental"] = True
    photo = photo_url(listing, size)
    if photo:
        record["photo"] = photo
    showing = open_house(listing)
    if showing:
        record["openHouse"] = showing
    return {k: v for k, v in record.items() if v is not None}


def collect(profile: dict, size: str) -> list[dict]:
    """Convert the listings section, leaving Transactions untouched."""
    try:
        section = profile["data"]["agentProfileProps"]["activeListingsProps"]
    except (KeyError, TypeError):
        sys.exit(
            "activeListingsProps missing from the profile payload - Compass "
            "changed its data shape. Nothing was written."
        )

    listings = []
    for key, total_key, is_rental in (
        ("initialSales", "initialSalesTotal", False),
        ("initialRentals", "initialRentalsTotal", True),
    ):
        items = section.get(key) or []
        total = section.get(total_key)
        if isinstance(total, int) and total > len(items):
            log(
                f"WARNING: Compass reports {total} {key.replace('initial', '').lower()} "
                f"but embedded only {len(items)} on the page. "
                "Scraping what is available."
            )
        listings.extend(convert(item, rental=is_rental, size=size) for item in items)

    # Highest price first, matching how the profile page orders them.
    listings.sort(key=lambda item: item.get("price") or 0, reverse=True)
    return listings


def write_atomic(path: pathlib.Path, payload: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=PROFILE_URL, help="agent profile URL")
    parser.add_argument("-o", "--out", default=str(REPO_ROOT / "listings.json"),
                        help="output JSON path")
    parser.add_argument("--photo-size", default="1024x768",
                        help="WxH for listing photos (Compass resizes on demand)")
    parser.add_argument("--timeout", type=int, default=45, help="HTTP timeout in seconds")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the JSON instead of writing it")
    parser.add_argument("--no-backup", action="store_true",
                        help="skip the one-time .bak copy (CI has git history)")
    args = parser.parse_args()

    log(f"Fetching {args.url}")
    try:
        html = fetch(args.url, args.timeout)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        sys.exit(f"Fetch failed: {exc}. Nothing was written.")

    listings = collect(extract_profile(html), args.photo_size)
    if not listings:
        sys.exit(
            "Parsed the page but found zero listings. Refusing to overwrite "
            "the existing file - check the profile manually."
        )

    payload = json.dumps(listings, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(payload, end="")
        log(f"Dry run - parsed {len(listings)} listing(s), nothing written.")
        return

    out = pathlib.Path(args.out).resolve()
    if out.exists() and out.read_text(encoding="utf-8") == payload:
        log(f"No change - {len(listings)} listing(s) already current in {out.name}")
        return

    backup = out.with_suffix(out.suffix + ".bak")
    if not args.no_backup and out.exists() and not backup.exists():
        backup.write_bytes(out.read_bytes())
        log(f"Saved previous contents to {backup.name}")

    write_atomic(out, payload)
    log(f"Wrote {len(listings)} listing(s) to {out}")


if __name__ == "__main__":
    main()
