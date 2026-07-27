"""Render listings.json into the listings section of index.html.

Rewrites everything between the `listings:start` / `listings:end` markers in
index.html. The rest of the file is untouched, so this is safe to re-run.

Layout is the "tiered" variant: the highest-priority listing gets a photo and
becomes the first row of the list; the rest are plain rows in the same idiom
as the Profiles / Get in touch / Documents sections. Row numbers continue
from the last number already used on the page.

Stdlib only. Runs after tools/scrape-listings.py in the daily workflow.

Usage:
    python tools/render-listings.py
    python tools/render-listings.py --dry-run
"""

import argparse
import datetime
import html
import json
import os
import pathlib
import re
import sys

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
FEATURE_PHOTO_SIZE = "1200x750"  # matches the 16/10 .feat-photo box
NUM_RE = re.compile(r'<span class="num">(\d+)</span>')


def esc(value) -> str:
    return html.escape(str(value), quote=True)


def money(amount: int) -> str:
    return f"${amount:,}"


def feature_photo(url: str) -> str:
    """Re-request the photo at the featured box's aspect ratio."""
    match = PHOTO_RE.match(url or "")
    return f"{match.group(1)}{FEATURE_PHOTO_SIZE}.jpg" if match else url


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
    """Sort key: upcoming open houses first, then by price, high to low."""
    parsed = parse_open_house(listing.get("openHouse", ""))
    return (0, parsed[0]) if parsed else (1, datetime.datetime.max)


def render_featured(listing: dict, number: int) -> list[str]:
    lines = [f'{INDENT}<a class="feat" href="{esc(listing["url"])}" target="_blank" rel="noopener">']

    # Without a photo there is nothing to fill the 16/10 box, so skip it -
    # the entry then renders identically to a plain row.
    photo = feature_photo(listing.get("photo", ""))
    if photo:
        badge = open_house_badge(listing.get("openHouse", ""))
        if not badge and listing.get("status", "").lower() != "active":
            badge = esc(listing["status"])
        lines.append(f'{INDENT}  <div class="feat-photo" style="background-image: url({esc(photo)})">')
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


def render(listings: list[dict], start_number: int) -> str:
    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">Current listings</h2>']

    ordered = sorted(listings, key=rank)
    for offset, listing in enumerate(ordered):
        number = start_number + offset
        block = render_featured(listing, number) if offset == 0 else render_row(listing, number)
        lines.extend("  " + line for line in block)  # nest inside <section>


    lines.append(f'{INDENT}  <p class="mls-note">{esc(ATTRIBUTION)}</p>')
    lines.append(f"{INDENT}</section>")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listings", default=str(REPO_ROOT / "listings.json"))
    parser.add_argument("--page", default=str(REPO_ROOT / "index.html"))
    parser.add_argument("--dry-run", action="store_true",
                        help="print the generated section instead of writing it")
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

    # Numbers already on the page, ignoring any inside the generated block.
    used = [int(n) for n in NUM_RE.findall(page[:start] + page[end:])]
    section = render(listings, max(used, default=0) + 1)

    if args.dry_run:
        print(section)
        return

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
