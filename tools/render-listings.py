"""Render listings.json and sold.json into index.html.

Rewrites everything between the `listings:start` / `listings:end` and
`sold:start` / `sold:end` markers in index.html. The rest of the file is
untouched, so this is safe to re-run.

Layout is the "tiered" variant: the highest-priced listing gets a photo and
becomes the first row of the list; the rest are plain rows in the same idiom
as the Profiles / Get in touch / Documents sections. Row numbers continue
from the last number already used on the page.

The featured photo is mirrored into listing-photos/ and served from our own
domain rather than hotlinked from Compass. Files are named after the Compass
content hash, so a replaced photo becomes a new file and stale ones are
pruned automatically.

The "Recently sold" section is plain rows only - no photos - showing the ten
most recent closed sales in the order Compass itself returns them. Its prices
are last list prices, not sale prices (Texas does not disclose those), and the
note under the section says so.

Stdlib only. Runs after tools/scrape-listings.py in the daily workflow.

Usage:
    python tools/render-listings.py
    python tools/render-listings.py --dry-run
    python tools/render-listings.py --no-download
    python tools/render-listings.py --sold-limit 5
"""

import argparse
import datetime
import html
import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
START = "<!-- listings:start"
END = "<!-- listings:end -->"
SOLD_START = "<!-- sold:start"
SOLD_END = "<!-- sold:end -->"
INDENT = " " * 6

# Ten is enough to show momentum without doubling the length of the page.
SOLD_LIMIT = 10

# Attribution shown under the listings. Data comes from the Compass profile,
# so this credits Compass rather than the MLS/HAR feed.
ATTRIBUTION = (
    "Listing information from Compass. All information should be "
    "independently verified. Mike Spear, Compass RE Texas, LLC."
)

# Texas is a non-disclosure state: closed sale prices are not public, and
# Compass publishes only the last list price. Saying so keeps the figures
# from reading as sale prices.
SOLD_ATTRIBUTION = (
    "Recent closed sales from Compass. Texas is a non-disclosure state; "
    "amounts shown are the last list price, not the sale price. "
    "Mike Spear, Compass RE Texas, LLC."
)

PHOTO_RE = re.compile(r"^(https://www\.compass\.com/m/[0-9a-f]+/)[^/]+$")
# The .feat-photo box is at most ~475 CSS px wide, so 960 covers a 2x screen.
FEATURE_PHOTO_SIZE = "960x600"
FEATURE_PHOTO_W, FEATURE_PHOTO_H = 960, 600
PHOTO_DIR_NAME = "listing-photos"
NUM_RE = re.compile(r'<span class="num">(\d+)</span>')
USER_AGENT = "Mozilla/5.0 (compatible; mikespear.com listings sync)"

# Filenames referenced by this render; anything else in the photo directory
# belongs to a listing that has since sold or been re-photographed.
KEEP: set[str] = set()

# What a record has to look like before it goes on the page. These are not
# style preferences - they are the line between "publish it" and "hide the
# section", so they stay loose enough that real data never trips them and
# tight enough that a change in Compass's shape does.
LISTING_URL_RE = re.compile(r"^https://www\.compass\.com/[\w./-]+$")
MIN_PRICE, MAX_PRICE = 100, 500_000_000
MAX_ROWS = 100


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(amount: int) -> str:
    return f"${amount:,}"


def faults(record: dict, *, sold: bool) -> list[str]:
    """Everything wrong with one record. Empty means it is safe to publish."""
    bad = []
    if not isinstance(record, dict):
        return ["not an object"]

    address = record.get("address")
    if not isinstance(address, str) or not address.strip() or len(address) > 200:
        bad.append("address")

    url = record.get("url")
    if not isinstance(url, str) or not LISTING_URL_RE.match(url):
        bad.append("url")

    price = record.get("listPrice" if sold else "price")
    if price is not None and not (
        isinstance(price, int) and not isinstance(price, bool)
        and MIN_PRICE <= price <= MAX_PRICE
    ):
        bad.append("price")

    for key, ceiling in (("beds", 50), ("baths", 50), ("sqft", 100_000)):
        value = record.get(key)
        if value is not None and not (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and 0 <= value <= ceiling
        ):
            bad.append(key)
    return bad


def screen(records: list, label: str, *, sold: bool) -> list[dict]:
    """Drop records we would be embarrassed to publish, loudly."""
    good = []
    for record in records:
        problems = faults(record, sold=sold)
        if problems:
            name = (record or {}).get("address") if isinstance(record, dict) else record
            print(f"WARNING: dropping {label} entry [{', '.join(problems)}]: "
                  f"{str(name)[:60]!r}")
            continue
        # A malformed photo costs the picture, not the whole listing.
        photo = record.get("photo")
        if photo is not None and not (
            isinstance(photo, str) and photo.startswith("https://")
        ):
            print(f"WARNING: ignoring bad photo URL on {record['address']!r}")
            record = {k: v for k, v in record.items() if k != "photo"}
        good.append(record)

    if len(good) > MAX_ROWS:
        print(f"WARNING: {len(good)} {label} entries exceeds the sane maximum "
              f"of {MAX_ROWS}; treating as corrupt.")
        return []
    return good


def load(path: pathlib.Path, label: str) -> list:
    """Read a JSON array, treating anything unreadable as no data at all."""
    if not path.exists():
        print(f"WARNING: {path.name} is missing.")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"WARNING: {path.name} is unreadable ({exc}).")
        return []
    if not isinstance(data, list):
        print(f"WARNING: {path.name} is {type(data).__name__}, expected a list.")
        return []
    return data


def feature_photo(url: str, photo_dir: pathlib.Path, download: bool = True) -> str:
    """Mirror the listing photo into the repo and return a site-root path.

    Photos are served from our own domain rather than hotlinked, so the page
    does not depend on Compass's CDN staying reachable. The filename carries
    the Compass content hash, so a new photo is a new file and caches never
    serve a stale image. If the download fails we fall back to the remote
    URL - a hotlinked photo beats no photo.
    """
    match = PHOTO_RE.match(url or "")
    if not match:
        return url
    remote = f"{match.group(1)}{FEATURE_PHOTO_SIZE}.jpg"
    digest = match.group(1).rstrip("/").rsplit("/", 1)[-1][:16]
    name = f"{digest}-{FEATURE_PHOTO_SIZE}.jpg"
    local = f"/{PHOTO_DIR_NAME}/{name}"
    destination = photo_dir / name
    KEEP.add(name)

    if destination.exists():
        return local
    if not download:
        return remote
    try:
        request = urllib.request.Request(remote, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read()
        if not data.startswith(b"\xff\xd8"):  # not a JPEG
            raise ValueError("response was not a JPEG")
        photo_dir.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        print(f"Downloaded {name} ({len(data) // 1024} KB)")
        return local
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        print(f"WARNING: could not mirror {name} ({exc}); falling back to hotlink")
        KEEP.discard(name)
        return remote


def prune(photo_dir: pathlib.Path) -> None:
    """Delete mirrored photos no longer referenced by any listing."""
    if not photo_dir.exists():
        return
    for stale in sorted(photo_dir.glob("*.jpg")):
        if stale.name not in KEEP:
            stale.unlink()
            print(f"Removed unused {stale.name}")


def parse_open_house(value: str):
    """"2026-08-02T14:00/16:00" -> (start datetime, end time)."""
    try:
        start_text, end_text = value.split("/")
        start = datetime.datetime.fromisoformat(start_text)
        end_hour, end_minute = (int(part) for part in end_text.split(":"))
    except (ValueError, AttributeError):
        return None
    return start, start.replace(hour=end_hour, minute=end_minute)


def clock(moment: datetime.datetime, meridiem: bool = True) -> str:
    """2:00 PM, with the leading zero dropped."""
    pattern = "%I:%M %p" if meridiem else "%I:%M"
    return moment.strftime(pattern).lstrip("0")


def open_house_badge(value: str) -> str | None:
    """Short form for the photo overlay: "Open Sun 2-4"."""
    parsed = parse_open_house(value)
    if not parsed:
        return None
    start, end = parsed
    return f"Open {start:%a} {start.strftime('%I').lstrip('0')}&ndash;{end.strftime('%I').lstrip('0')}"


def open_house_line(value: str) -> str | None:
    """Long form under the address: "Open Sunday, August 2 - 2:00-4:00 PM"."""
    parsed = parse_open_house(value)
    if not parsed:
        return None
    start, end = parsed
    day = f"{start:%A}, {start:%B} {start.day}"
    # Say "2:00-4:00 PM", not "2:00 PM-4:00 PM", when both share a meridiem.
    same_half = start.strftime("%p") == end.strftime("%p")
    return f"Open {day} &middot; {clock(start, not same_half)}&ndash;{clock(end)}"


def spec_parts(record: dict) -> list[str]:
    """Area / beds / baths / sqft - the pieces every row shares."""
    parts = []
    if record.get("area"):
        parts.append(esc(record["area"]))
    if record.get("beds"):
        parts.append(f"{record['beds']} bd")
    if record.get("baths"):
        baths = record["baths"]
        parts.append(f"{baths:g} ba")
    if record.get("sqft"):
        parts.append(f"{record['sqft']:,} sqft")
    return parts


def meta_line(listing: dict) -> str:
    """"Montrose - 3 bd - 2 ba - 2,105 sqft - Pending"."""
    parts = spec_parts(listing)
    # Active is the default state and adds nothing; call out anything else.
    if listing.get("status") and listing["status"].lower() != "active":
        parts.append(esc(listing["status"]))
    return " &middot; ".join(parts)


def sold_meta_line(sale: dict) -> str:
    """"Rosemont Heights - 4 bd - 4.5 ba".

    Deliberately dateless. The public Compass payload has no close date, only
    a record-modified timestamp, so any date shown here would be wrong as soon
    as a listing gets edited after closing.
    """
    return " &middot; ".join(spec_parts(sale))


def price_cell(listing: dict) -> str:
    price = listing.get("price")
    if not price:
        return '<span class="row-price">&mdash;</span>'
    if listing.get("rental"):
        return f'<span class="row-price">{money(price)}<span class="per">/mo</span></span>'
    return f'<span class="row-price">{money(price)}</span>'


def rank(listing: dict) -> tuple:
    """Sort key: highest price first, so the flagship listing leads.

    Rentals fall to the bottom naturally, their monthly rent being far
    below any sale price.
    """
    return (-(listing.get("price") or 0), listing.get("address") or "")


def render_featured(listing: dict, number: int, photo_dir: pathlib.Path,
                    download: bool) -> list[str]:
    lines = [f'{INDENT}<a class="feat" href="{esc(listing["url"])}" target="_blank" rel="noopener">']

    # Without a photo there is nothing to fill the 16/10 box, so skip it -
    # the entry then renders identically to a plain row.
    photo = feature_photo(listing.get("photo", ""), photo_dir, download)
    if photo:
        badge = open_house_badge(listing.get("openHouse", ""))
        if not badge and listing.get("status", "").lower() != "active":
            badge = esc(listing["status"])
        lines.append(f'{INDENT}  <div class="feat-photo">')
        # An <img> rather than a CSS background so it can lazy-load; the
        # section sits below the fold. alt is empty because the address is
        # spelled out in text immediately underneath.
        lines.append(
            f'{INDENT}    <img src="{esc(photo)}" alt="" loading="lazy" decoding="async" '
            f'width="{FEATURE_PHOTO_W}" height="{FEATURE_PHOTO_H}">'
        )
        if badge:
            klass = "listing-status is-open" if badge.startswith("Open") else "listing-status"
            lines.append(f'{INDENT}    <span class="{klass}">{badge}</span>')
        lines.append(f"{INDENT}  </div>")

    lines.append(f'{INDENT}  <div class="feat-body">')
    lines.append(f'{INDENT}    <span class="num">{number:02d}</span>')
    lines.append(f'{INDENT}    <span class="feat-text">')
    lines.append(f'{INDENT}      <span class="row-main">{esc(listing["address"])}</span>')
    lines.append(f'{INDENT}      <span class="row-sub">{meta_line(listing)}</span>')
    showing = open_house_line(listing.get("openHouse", ""))
    if showing:
        lines.append(f'{INDENT}      <span class="oh-when">{showing}</span>')
    lines.append(f"{INDENT}    </span>")
    lines.append(f"{INDENT}    {price_cell(listing)}")
    lines.append(f"{INDENT}  </div>")
    lines.append(f"{INDENT}</a>")
    return lines


def render_row(listing: dict, number: int) -> list[str]:
    text = [f'<span class="row-main">{esc(listing["address"])}</span>',
            f'<span class="row-sub">{meta_line(listing)}</span>']
    showing = open_house_line(listing.get("openHouse", ""))
    if showing:
        text.append(f'<span class="oh-when">{showing}</span>')
    return [
        f'{INDENT}<a class="row" href="{esc(listing["url"])}" target="_blank" rel="noopener">',
        f'{INDENT}  <span class="num">{number:02d}</span>',
        f'{INDENT}  <span class="row-text">{"".join(text)}</span>',
        f"{INDENT}  {price_cell(listing)}",
        f"{INDENT}</a>",
    ]


def render_sold_row(sale: dict, number: int) -> list[str]:
    """A closed sale as a plain row: no photo, price labelled by the note."""
    text = [f'<span class="row-main">{esc(sale["address"])}</span>',
            f'<span class="row-sub">{sold_meta_line(sale)}</span>']
    price = sale.get("listPrice")
    cell = (f'<span class="row-price">{money(price)}</span>' if price
            else '<span class="row-price">&mdash;</span>')
    return [
        f'{INDENT}<a class="row" href="{esc(sale["url"])}" target="_blank" rel="noopener">',
        f'{INDENT}  <span class="num">{number:02d}</span>',
        f'{INDENT}  <span class="row-text">{"".join(text)}</span>',
        f"{INDENT}  {cell}",
        f"{INDENT}</a>",
    ]


def render_sold(sales: list[dict], start_number: int) -> str:
    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">Recently sold</h2>']
    for offset, sale in enumerate(sales):
        lines.extend("  " + line for line in
                     render_sold_row(sale, start_number + offset))
    lines.append(f'{INDENT}  <p class="mls-note">{esc(SOLD_ATTRIBUTION)}</p>')
    lines.append(f"{INDENT}</section>")
    return "\n".join(lines)


def render(listings: list[dict], start_number: int, photo_dir: pathlib.Path,
           download: bool) -> str:
    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">Current listings</h2>']

    ordered = sorted(listings, key=rank)
    for offset, listing in enumerate(ordered):
        number = start_number + offset
        if offset == 0:
            block = render_featured(listing, number, photo_dir, download)
        else:
            block = render_row(listing, number)
        lines.extend("  " + line for line in block)  # nest inside <section>

    lines.append(f'{INDENT}  <p class="mls-note">{esc(ATTRIBUTION)}</p>')
    lines.append(f"{INDENT}</section>")
    return "\n".join(lines)


def find_region(page: str, start_marker: str, end_marker: str):
    """(start, end) offsets of a generated region, or None if absent."""
    start = page.find(start_marker)
    end = page.find(end_marker)
    if start == -1 or end == -1 or end < start:
        return None
    return start, end


def strip_region(page: str, start_marker: str, end_marker: str) -> str:
    """Page with one generated region removed, for counting row numbers."""
    span = find_region(page, start_marker, end_marker)
    if not span:
        return page
    start, end = span
    return page[:start] + page[end + len(end_marker):]


def splice(page: str, start_marker: str, end_marker: str, body: str) -> str:
    """Replace a region's contents, keeping both marker lines in place.

    An empty body collapses the region to just its two markers, which removes
    the section from the page while leaving the machinery to refill it later.
    """
    start, end = find_region(page, start_marker, end_marker)
    head_end = page.index("\n", start) + 1
    if not body:
        return f"{page[:head_end]}{INDENT}{page[end:]}"
    return f"{page[:head_end]}{body}\n{INDENT}{page[end:]}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listings", default=str(REPO_ROOT / "listings.json"))
    parser.add_argument("--sold", default=str(REPO_ROOT / "sold.json"))
    parser.add_argument("--page", default=str(REPO_ROOT / "index.html"))
    parser.add_argument("--photo-dir", default=str(REPO_ROOT / PHOTO_DIR_NAME))
    parser.add_argument("--sold-limit", type=int, default=SOLD_LIMIT,
                        help=f"how many closed sales to show (default {SOLD_LIMIT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the generated sections instead of writing them")
    parser.add_argument("--no-download", action="store_true",
                        help="do not mirror photos; reference Compass URLs directly")
    args = parser.parse_args()

    page_path = pathlib.Path(args.page)
    page = page_path.read_text(encoding="utf-8")
    if not find_region(page, START, END):
        sys.exit(f"Could not find the listings markers in {page_path.name}.")

    # Anything that fails to load or fails screening leaves an empty list, and
    # an empty list hides its section. A half-broken page is worse than no
    # section at all - a stale listing is something a client acts on.
    listings = screen(load(pathlib.Path(args.listings), "listings.json"),
                      "listing", sold=False)
    sales = screen(load(pathlib.Path(args.sold), "sold.json"),
                   "sold", sold=True)[:args.sold_limit]

    if sales and not find_region(page, SOLD_START, SOLD_END):
        print(f"WARNING: no sold markers in {page_path.name}; skipping that section.")
        sales = []

    # A dry run must not touch the filesystem, so it neither fetches nor prunes.
    photo_dir = pathlib.Path(args.photo_dir)
    download = not args.dry_run and not args.no_download

    # Numbers already on the page, ignoring any inside either generated block.
    static = strip_region(strip_region(page, START, END), SOLD_START, SOLD_END)
    first = max((int(n) for n in NUM_RE.findall(static)), default=0) + 1

    section = render(listings, first, photo_dir, download) if listings else ""
    sold_section = render_sold(sales, first + len(listings)) if sales else ""

    hidden = [name for name, rows in (("Current listings", listings),
                                      ("Recently sold", sales)) if not rows]
    for name in hidden:
        print(f"HIDING the {name!r} section - no usable data.")

    if args.dry_run:
        print(section or "(listings hidden)")
        print(sold_section or "(sold hidden)")
        return

    # Pruning against an empty KEEP would delete every mirrored photo, so only
    # prune when we actually rendered listings to compare against.
    if not args.no_download and listings:
        prune(photo_dir)

    updated = splice(page, START, END, section)
    if find_region(updated, SOLD_START, SOLD_END):
        updated = splice(updated, SOLD_START, SOLD_END, sold_section)

    if updated != page:
        temp = page_path.with_suffix(".tmp")
        temp.write_text(updated, encoding="utf-8", newline="\n")
        os.replace(temp, page_path)
        print(f"Rendered {len(listings)} listing(s) and {len(sales)} closed "
              f"sale(s) into {page_path.name}")
    else:
        print(f"No change - {len(listings)} listing(s), {len(sales)} sale(s) "
              "already rendered.")

    # Exit 3 means "the page is safe, but something upstream is broken" - the
    # workflow publishes the hidden state first, then fails the run to raise
    # the alarm. Anything else would either hide silently or skip the fix.
    if hidden:
        sys.exit(3)


if __name__ == "__main__":
    main()
