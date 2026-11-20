"""Scrape Mike Spear's listings and closed sales from his Compass profile.

Reads the `window.__AGENT_PROFILE__` JSON that Compass embeds in the profile
page and writes two files:

  listings.json - the "Listings" section (`activeListingsProps`)
  sold.json     - closed *sales* from "Transactions" (`closedDealsProps`)

Closed rentals are deliberately skipped, as are all other pages. Sold entries
carry no photo: only the active listings are shown with imagery.

Each listing's first few gallery photos (--photo-count, default 9) are also
copied into listing-photos/ and listed under "photos" with their Compass
captions as alt text - see mirror_photos() for how new listings and replaced
photos are picked up.

A note on sold prices: Texas is a non-disclosure state, so Compass does not
publish what a home actually closed for - every closed deal in the payload is
flagged `hideHistoricalSoldPrice` and exposes only `price.lastKnown`, the last
list price. That is what lands in sold.json, and the renderer labels it as
such rather than implying a sale price.

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
import time
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


def gallery(listing: dict, count: int) -> list[dict]:
    """The first `count` photos in Compass gallery order, as
    {"hash", "source", "alt"}. The hash is Compass's content hash from the
    URL, so a photo the agent replaces comes back with a different one."""
    shots = []
    for item in listing.get("media") or []:
        source = item.get("originalUrl") or item.get("thumbnailUrl") or ""
        match = PHOTO_RE.match(source)
        if not match:
            continue
        digest = match.group(1).rstrip("/").rsplit("/", 1)[-1]
        shots.append({
            "hash": digest,
            "source": match.group(1),
            "alt": (item.get("description") or "").strip(),
        })
        if len(shots) == count:
            break
    return shots


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


def area_of(location: dict) -> str | None:
    """Neighbourhood name for the meta line, with sensible fallbacks."""
    area = location.get("neighborhood")
    if not area:
        for key in ("mlsNeighborhoods", "subdivisionNames", "subNeighborhoods"):
            values = location.get(key) or []
            if values:
                area = values[0]
                break
    return area or location.get("city")


def convert(listing: dict, *, rental: bool, size: str, count: int = 0) -> dict:
    """Map one Compass listing onto the listings.json schema."""
    location = listing.get("location") or {}
    dimensions = listing.get("size") or {}
    price = listing.get("price") or {}

    area = area_of(location)
    link = listing.get("canonicalPageLink") or listing.get("pageLink")

    record = {
        "address": location.get("prettyAddress"),
        "area": area,
        "beds": dimensions.get("bedrooms"),
        "baths": dimensions.get("bathrooms"),
        "sqft": dimensions.get("squareFeet"),
        # lastKnown is the current ask (what the page shows); listed is the
        # original price, which differs once there has been a price cut.
        "price": price.get("lastKnown") or price.get("listed"),
        "status": listing.get("localizedStatus"),
        # The day the home went on the market. The renderer flags a home
        # listed within the last couple of weeks as "New"; it works out that
        # window itself, against the date the page is built, so this stays a
        # fixed calendar date rather than a countdown that would go stale
        # between scrapes.
        "listedDate": local_date(listing, "listed"),
        "url": f"{SITE}{link}" if link else None,
    }
    if rental:
        record["rental"] = True
    photo = photo_url(listing, size)
    if photo:
        record["photo"] = photo
    shots = gallery(listing, count)
    if shots:
        # Placeholder for mirror_photos(), which swaps in the local files.
        record["photos"] = shots
        # How many Compass has in all, so the page can offer "All N photos".
        record["photoCount"] = sum(
            1 for m in listing.get("media") or []
            if PHOTO_RE.match(m.get("originalUrl") or m.get("thumbnailUrl") or ""))
    showing = open_house(listing)
    if showing:
        record["openHouse"] = showing
    extra = details(listing)
    if extra:
        record["details"] = extra
    return {k: v for k, v in record.items() if v is not None}


# Features worth calling out on a listing page, in the order they're shown.
# Compass spreads these across several MLS lists (amenities, lot, parking,
# outdoor space); anything not named here (dishwasher, central AC...) is
# left out as standard.
HIGHLIGHTS = [
    "Pool", "Elevator", "Corner Lot", "Alley Access", "Wine Cellar", "Wet Bar",
    "Fireplace", "Outdoor Kitchen", "Double Oven", "Hardwood Floors",
    "High Ceilings", "Soaking Tub", "Walk-In Closet", "Media Room", "Balcony",
    "Roof Deck", "Rooftop Deck", "Deck", "Patio", "Porch", "Private Yard",
    "Generator", "Gated",
]


def details(listing: dict) -> dict:
    """Extra facts for the per-listing page, all from the same profile JSON
    (no extra requests): property type, stories, lot size, year built, garage, new
    construction, price per square foot and a short list of standout
    features. Days on market is left to the page, which counts it from
    listedDate so it never goes stale between scrapes. Assessor
    data is skipped on purpose - for new builds it still describes the old
    house."""
    info = listing.get("detailedInfo") or {}
    size = listing.get("size") or {}
    price = listing.get("price") or {}
    tagged = set()
    for key in ("amenities", "lotFeatures", "parkingFeatures", "outdoorSpace"):
        tagged.update(info.get(key) or [])
    features = [name for name in HIGHLIGHTS if name in tagged]
    if info.get("poolPrivateYN") and "Pool" not in features:
        features.insert(0, "Pool")
    if info.get("primaryBedroomOnMainLevel"):
        features.append("Primary Bedroom Down")
    badges = (listing.get("badges") or {}).get("cornerBadges") or []
    per_sqft = price.get("perSquareFoot")
    stories = size.get("stories")
    # Compass's own type labels, e.g. ["Single Family"] / ["Townhouse"] /
    # ["Condo"] / ["Rental", "Multi Family"]; "Rental" is the deal, not the home.
    kinds = ((info.get("propertyType") or {}).get("masterType") or {}).get("GLOBAL") or []
    kinds = [k for k in kinds if isinstance(k, str) and k != "Rental"]
    out = {
        "propertyType": kinds[0] if kinds else None,
        "stories": (int(stories) if stories == int(stories) else stories) if stories else None,
        "lotSqft": size.get("lotSizeInSquareFeet"),
        "yearBuilt": (listing.get("buildingInfo") or {}).get("buildingYearOpened"),
        "garage": info.get("garageSpaces"),
        "newConstruction": any(b.get("displayText") == "New Construction" for b in badges) or None,
        "pricePerSqft": round(per_sqft) if per_sqft else None,
        "features": features or None,
    }
    return {k: v for k, v in out.items() if v is not None}


def collect(profile: dict, size: str, count: int = 0) -> list[dict]:
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
        listings.extend(convert(item, rental=is_rental, size=size, count=count)
                        for item in items)

    # Highest price first, matching how the profile page orders them.
    listings.sort(key=lambda item: item.get("price") or 0, reverse=True)
    return listings


def local_date(listing: dict, key: str) -> str | None:
    """A `date.<key>` millisecond timestamp as YYYY-MM-DD in the home's local
    timezone, or None if it is absent or unusable.

    Date-only, and in the listing's own timezone rather than UTC, because the
    page reasons about these as calendar days a Houston reader would recognise
    (how long a home has been listed), not instants.
    """
    stamp = (listing.get("date") or {}).get(key)
    if not stamp:
        return None
    tz_name = (listing.get("location") or {}).get("timezone") or "America/Chicago"
    try:
        tz = zoneinfo.ZoneInfo(tz_name)
    except zoneinfo.ZoneInfoNotFoundError:
        tz = datetime.timezone.utc
    return datetime.datetime.fromtimestamp(stamp / 1000, tz).strftime("%Y-%m-%d")


def sold_date(listing: dict) -> str | None:
    """When the record was last modified, as YYYY-MM-DD. NOT the close date.

    `date.updated` is the only timestamp the public payload exposes on a closed
    deal - Compass redacts close dates along with sale prices, Texas being a
    non-disclosure state. It usually equals the close date, but it moves
    whenever the record is edited: 1215 West Pierce reads 2026-07-22 here while
    the agent-authenticated view places it two sales earlier. Kept for
    debugging and left off the page; never render it as a sale date.
    """
    return local_date(listing, "updated")


def convert_sold(item: dict) -> dict:
    """Map one closed deal onto the sold.json schema (no photo)."""
    # Closed deals nest the listing one level deeper than active ones do.
    listing = item.get("listing") or {}
    location = listing.get("location") or {}
    dimensions = listing.get("size") or {}
    price = listing.get("price") or {}
    link = listing.get("canonicalPageLink") or listing.get("pageLink")

    record = {
        "address": location.get("prettyAddress"),
        "area": area_of(location),
        "beds": dimensions.get("bedrooms"),
        "baths": dimensions.get("bathrooms"),
        "sqft": dimensions.get("squareFeet"),
        # The last list price, NOT the sale price - see the module docstring.
        "listPrice": price.get("lastKnown"),
        # Named for what it is. Not a close date - see sold_date().
        "updated": sold_date(listing),
        "url": f"{SITE}{link}" if link else None,
    }
    return {k: v for k, v in record.items() if v is not None}


def collect_sold(profile: dict) -> list[dict]:
    """Closed sales from the Transactions section. Rentals are skipped."""
    try:
        section = profile["data"]["agentProfileProps"]["closedDealsProps"]
    except (KeyError, TypeError):
        log("WARNING: closedDealsProps missing from the profile payload. "
            "Leaving sold.json untouched.")
        return []

    items = section.get("initialSales") or []
    total = section.get("initialSalesCount")
    if isinstance(total, int) and total > len(items):
        log(f"NOTE: Compass reports {total} closed sales but embeds only "
            f"{len(items)} on the page. Using the {len(items)} most recent.")

    # Compass's own order is kept verbatim (closedDealsSortOrder says how it
    # sorted). Re-sorting here would override whatever is configured on the
    # profile, and the only date we could sort on is unreliable anyway.
    order = section.get("closedDealsSortOrder")
    if order and order != "DATE_DESCENDING":
        log(f"NOTE: Compass is sorting closed deals by {order}, not by date.")
    return [convert_sold(item) for item in items]


def fetch_image(url: str, timeout: int) -> bytes:
    """Download one photo, refusing anything that is not a JPEG or PNG."""
    headers = dict(HEADERS, Accept="image/avif,image/webp,image/*,*/*;q=0.8")
    headers.pop("Accept-Encoding")  # images arrive as-is; nothing to inflate
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read()
    if not (data.startswith(b"\xff\xd8\xff") or data.startswith(b"\x89PNG")):
        raise ValueError(f"not an image ({len(data)} bytes)")
    return data


def mirror_photos(listings: list[dict], folder: pathlib.Path, size: str,
                  timeout: int) -> None:
    """Copy each listing's gallery photos onto our own server.

    Files are named after Compass's content hash, so:
      - a new listing, or a Coming Soon one that gains photos, downloads them;
      - a photo the agent replaces on Compass has a new hash and downloads too;
      - anything already on disk is never fetched again, so a normal day makes
        zero image requests.
    Files no longer referenced by any listing (sold, withdrawn, replaced) are
    deleted. A download that fails falls back to the Compass URL for this run
    and is retried tomorrow, since its file still will not exist.
    """
    folder.mkdir(exist_ok=True)
    keep: set[str] = set()
    fetched = failed = 0

    for listing in listings:
        resolved = []
        for shot in listing.get("photos") or []:
            name = f"{shot['hash'][:16]}-{size}.jpg"
            path = folder / name
            remote = f"{shot['source']}{size}.jpg"
            if not path.exists():
                try:
                    data = fetch_image(remote, timeout)
                except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                    failed += 1
                    log(f"WARNING: photo for {listing.get('address')} failed "
                        f"({exc}); using the Compass URL until it downloads.")
                    resolved.append({"src": remote, "alt": shot["alt"]})
                    continue
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                os.replace(temp, path)
                fetched += 1
                time.sleep(0.5)  # be gentle with Compass on a big first run
            keep.add(name)
            resolved.append({"src": f"{folder.name}/{name}", "alt": shot["alt"]})
        if resolved:
            listing["photos"] = resolved

    pruned = 0
    for stale in folder.glob("*.jpg"):
        if stale.name not in keep:
            stale.unlink()
            pruned += 1

    log(f"Photos: {len(keep)} mirrored in {folder.name}/ "
        f"({fetched} new, {pruned} removed, {failed} failed)")


def write_atomic(path: pathlib.Path, payload: str) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(payload, encoding="utf-8", newline="\n")
    os.replace(temp, path)


def save(records: list[dict], out: pathlib.Path, noun: str, args) -> None:
    """Write records as JSON, honouring --dry-run and the one-time backup."""
    payload = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    if args.dry_run:
        print(payload, end="")
        log(f"Dry run - parsed {len(records)} {noun}(s), nothing written.")
        return

    out = out.resolve()
    if out.exists() and out.read_text(encoding="utf-8") == payload:
        log(f"No change - {len(records)} {noun}(s) already current in {out.name}")
        return

    backup = out.with_suffix(out.suffix + ".bak")
    if not args.no_backup and out.exists() and not backup.exists():
        backup.write_bytes(out.read_bytes())
        log(f"Saved previous contents to {backup.name}")

    write_atomic(out, payload)
    log(f"Wrote {len(records)} {noun}(s) to {out}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default=PROFILE_URL, help="agent profile URL")
    parser.add_argument("-o", "--out", default=str(REPO_ROOT / "listings.json"),
                        help="output JSON path")
    parser.add_argument("--sold-out", default=str(REPO_ROOT / "sold.json"),
                        help="output JSON path for closed sales")
    parser.add_argument("--no-sold", action="store_true",
                        help="skip the Transactions section entirely")
    parser.add_argument("--photo-size", default="1024x768",
                        help="WxH for listing photos (Compass resizes on demand)")
    parser.add_argument("--photo-count", type=int, default=9,
                        help="gallery photos per listing to mirror into "
                             "listing-photos/ (0 to skip)")
    parser.add_argument("--gallery-size", default="2048x1536",
                        help="WxH box for mirrored photos; Compass fits "
                             "within it without cropping (originals are "
                             "~2172 wide, so this is near full quality)")
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

    profile = extract_profile(html)

    listings = collect(profile, args.photo_size, args.photo_count)
    if not listings:
        sys.exit(
            "Parsed the page but found zero listings. Refusing to overwrite "
            "the existing file - check the profile manually."
        )
    if args.dry_run:
        for listing in listings:
            listing.pop("photos", None)
    elif args.photo_count > 0:
        mirror_photos(listings, REPO_ROOT / "listing-photos",
                      args.gallery_size, args.timeout)
    save(listings, pathlib.Path(args.out), "listing", args)

    # Additive: a change to the Transactions section must never cost us the
    # listings write above, so an empty result is a warning, not an exit.
    if not args.no_sold:
        sold = collect_sold(profile)
        if sold:
            save(sold, pathlib.Path(args.sold_out), "closed sale", args)
        else:
            log("WARNING: no closed sales parsed. sold.json left as-is.")


if __name__ == "__main__":
    main()
