"""Render listings.json into the listings section of index.html.

Rewrites everything between the `listings:start` / `listings:end` markers in
index.html. The rest of the file is untouched, so this is safe to re-run.

Layout is the "tiered" variant: the highest-priced listing gets a photo and
becomes the first row of the list; the rest are plain rows in the same idiom
as the Profiles / Get in touch / Documents sections. Row numbers continue
from the last number already used on the page.

The featured photo is mirrored into listing-photos/ and served from our own
domain rather than hotlinked from Compass. Files are named after the Compass
content hash, so a replaced photo becomes a new file and stale ones are
pruned automatically.

Stdlib only. Runs after tools/scrape-listings.py in the daily workflow.

Usage:
    python tools/render-listings.py
    python tools/render-listings.py --dry-run
    python tools/render-listings.py --no-download
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
INDENT = " " * 6

# Attribution shown under the listings. Data comes from the Compass profile,
# so this credits Compass rather than the MLS/HAR feed.
ATTRIBUTION = (
    "Listing information from Compass. All information should be "
    "independently verified. Mike Spear, Compass RE Texas, LLC."
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


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(amount: int) -> str:
    return f"${amount:,}"


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


def meta_line(listing: dict) -> str:
    """"Montrose - 3 bd - 2 ba - 2,105 sqft - Pending"."""
    parts = []
    if listing.get("area"):
        parts.append(esc(listing["area"]))
    if listing.get("beds"):
        parts.append(f"{listing['beds']} bd")
    if listing.get("baths"):
        baths = listing["baths"]
        parts.append(f"{baths:g} ba")
    if listing.get("sqft"):
        parts.append(f"{listing['sqft']:,} sqft")
    # Active is the default state and adds nothing; call out anything else.
    if listing.get("status") and listing["status"].lower() != "active":
        parts.append(esc(listing["status"]))
    return " &middot; ".join(parts)


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listings", default=str(REPO_ROOT / "listings.json"))
    parser.add_argument("--page", default=str(REPO_ROOT / "index.html"))
    parser.add_argument("--photo-dir", default=str(REPO_ROOT / PHOTO_DIR_NAME))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the generated section instead of writing it")
    parser.add_argument("--no-download", action="store_true",
                        help="do not mirror photos; reference Compass URLs directly")
    args = parser.parse_args()

    listings_path = pathlib.Path(args.listings)
    page_path = pathlib.Path(args.page)
    if not listings_path.exists():
        sys.exit(f"{listings_path} not found - run tools/scrape-listings.py first.")

    listings = json.loads(listings_path.read_text(encoding="utf-8"))
    if not listings:
        sys.exit("listings.json is empty - leaving index.html alone.")

    page = page_path.read_text(encoding="utf-8")
    start = page.find(START)
    end = page.find(END)
    if start == -1 or end == -1 or end < start:
        sys.exit(f"Could not find the listings markers in {page_path.name}.")

    # A dry run must not touch the filesystem, so it neither fetches nor prunes.
    photo_dir = pathlib.Path(args.photo_dir)
    download = not args.dry_run and not args.no_download

    # Numbers already on the page, ignoring any inside the generated block.
    used = [int(n) for n in NUM_RE.findall(page[:start] + page[end:])]
    section = render(listings, max(used, default=0) + 1, photo_dir, download)

    if args.dry_run:
        print(section)
        return

    if not args.no_download:
        prune(photo_dir)

    head = page[:page.index("\n", start) + 1]
    updated = f"{head}{section}\n{INDENT}{page[end:]}"
    if updated == page:
        print(f"No change - {len(listings)} listing(s) already rendered.")
        return

    temp = page_path.with_suffix(".tmp")
    temp.write_text(updated, encoding="utf-8", newline="\n")
    os.replace(temp, page_path)
    print(f"Rendered {len(listings)} listing(s) into {page_path.name}")


if __name__ == "__main__":
    main()
