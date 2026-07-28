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

The "On the map" section plots both lists as an inline SVG, using coordinates
cached in geo.json by tools/geocode.py, over the freeway and water geometry
in basemap.json. It is drawn rather than embedded: no tile provider, no API
key, no third-party script, no extra requests, and it themes off the same
CSS variables as everything else on the page. The inside of the 610 loop is
washed with a tint and the count of addresses falling inside it is stated,
because that concentration is the thing the map exists to show.

Stdlib only. Runs after tools/geocode.py in the daily workflow.
The basemap it draws on is built separately by tools/basemap.py.

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
MAP_START = "<!-- map:start"
MAP_END = "<!-- map:end -->"
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

# ── Map geometry ───────────────────────────────────────────────────────────
# The frame, the viewBox and every path come out of basemap.json, which
# tools/basemap.py builds from Census TIGER/Line data. Nothing about the map's
# extent is defined here: if the dots used one frame and the roads another,
# every listing would sit at the wrong end of the wrong freeway.

# One dot per sale, no clustering and no numerals. An earlier version rolled
# neighbours up into a single dot with the count beside it; "7" turns out to
# be a much weaker signal than seven overlapping dots, which pile up into a
# visibly darker patch exactly where the work is concentrated. The dots are
# translucent and unstroked so that stacking is what makes them darker.
SOLD_DOT = 4.4
LIVE_DOT = 4.6     # active listings, drawn over the sales in the accent colour
LIVE_RING = 8.4    # halo around a live listing, so it reads through a pile

# Under the dots, homes that have neighbours also contribute a wide, faint,
# blurred disc, which stack into a warm patch over Montrose, River Oaks and
# the Heights. That patch is the argument the section exists to make, and it
# makes it before a reader has read a word of the legend.
#
# Only homes with at least HEAT_MIN neighbours within HEAT_NEAR get one. An
# earlier version gave every home a disc, which put an identical halo around
# each isolated outlier - so the drawing gained a set of coffee-ring stains
# and lost the one thing the layer was for, which is showing where the work
# is *not* spread evenly.
HEAT_R = 16.0
HEAT_NEAR = 20.0
HEAT_MIN = 2

# The drawing carries no text at all. The shape of the Loop, the spokes coming
# off it and the bayous are what orient a reader; anything written on top was
# labelling what the geometry already said.


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


def load_object(path: pathlib.Path, note: str) -> dict:
    """A JSON object from disk, treating anything unreadable as empty.

    Used for geo.json (written by tools/geocode.py) and basemap.json (written
    by tools/basemap.py). Either one coming back empty hides the map and
    nothing else - the lists do not depend on either.
    """
    if not path.exists():
        print(f"WARNING: {path.name} is missing; {note}.")
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"WARNING: {path.name} is unreadable ({exc}).")
        return {}
    if not isinstance(data, dict):
        print(f"WARNING: {path.name} is {type(data).__name__}, expected an object.")
        return {}
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


class Frame:
    """The map's window on the world, as recorded in basemap.json.

    Built from the same numbers the freeway paths were projected with, so a
    dot and the road it sits beside cannot disagree about where they are.
    """

    def __init__(self, basemap: dict):
        bounds = basemap["frame"]
        self.west, self.east = float(bounds["west"]), float(bounds["east"])
        self.south, self.north = float(bounds["south"]), float(bounds["north"])
        self.width, self.height = (float(v) for v in basemap["viewBox"])
        if not (self.east > self.west and self.north > self.south
                and self.width > 0 and self.height > 0):
            raise ValueError("frame bounds are inside out")

    def project(self, lon: float, lat: float) -> tuple[float, float]:
        """Longitude/latitude to SVG user units, north up."""
        x = (lon - self.west) / (self.east - self.west) * self.width
        y = (self.north - lat) / (self.north - self.south) * self.height
        return x, y

    def holds(self, x: float, y: float, radius: float) -> bool:
        """Whether a dot of this size lands fully inside the drawing."""
        return (radius <= x <= self.width - radius
                and radius <= y <= self.height - radius)


def plot(records: list[dict], geo: dict, radius: float,
         frame: "Frame") -> tuple[list, int]:
    """Project every record we have a coordinate for; count the ones that miss.

    A record with no cached coordinate is not the same as one that falls
    outside the frame. The first is a gap we would want to fix, the second is
    the frame working as designed, so only the second is reported to readers.
    """
    points, off_frame = [], 0
    for record in records:
        point = geo.get(geo_key(record.get("url", "")) or "")
        if not (isinstance(point, list) and len(point) == 2):
            continue
        try:
            x, y = frame.project(float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            continue
        if frame.holds(x, y, radius):
            points.append((x, y))
        else:
            off_frame += 1
    return points, off_frame


def geo_key(url: str) -> str | None:
    """The geo.json key for a listing URL: its Compass address slug."""
    match = re.search(r"/homedetails/([^/]+)/", url or "")
    return match.group(1) if match else None


def dense(points: list) -> list:
    """The points with enough close neighbours to be worth glowing."""
    return [(x, y) for x, y in points
            if sum(1 for ox, oy in points
                   if (ox - x) ** 2 + (oy - y) ** 2 <= HEAT_NEAR ** 2) - 1
            >= HEAT_MIN]


def inside_ring(x: float, y: float, ring: list) -> bool:
    """Whether a point falls within the 610 ring, by ray casting."""
    hit = False
    count = len(ring)
    for index in range(count):
        x1, y1 = ring[index]
        x2, y2 = ring[(index + 1) % count]
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            hit = not hit
    return hit


def loop_ring(basemap: dict) -> list:
    """basemap.json's 610 ring as coordinate pairs, or [] if it is unusable."""
    ring = basemap.get("loopRing")
    if not isinstance(ring, list) or len(ring) < 3:
        return []
    points = []
    for pair in ring:
        if not (isinstance(pair, list) and len(pair) == 2):
            return []
        try:
            points.append((float(pair[0]), float(pair[1])))
        except (TypeError, ValueError):
            return []
    return points


def layer(basemap: dict, key: str, css: str, closed: bool = False) -> list[str]:
    """One <path> for a basemap layer, or nothing if that layer came back empty.

    Water is the layer that can legitimately be missing - tools/basemap.py
    keeps going when the Hydro service is down - so an absent key draws
    nothing rather than an empty path or a crash.
    """
    data = basemap.get(key)
    if not isinstance(data, str) or not data:
        return []
    rule = ' fill-rule="evenodd"' if closed else ""
    return [f'  <path class="{css}" d="{esc(data)}"{rule}/>']


def render_map(listings: list[dict], sales: list[dict], geo: dict,
               basemap: dict) -> str:
    """The whole map section, or "" if there is nothing worth drawing.

    Sales are plotted from the full list rather than the ten rows shown above,
    because the point of the map is the shape of the whole territory. The
    caption says how many sales it stands for so the two do not read as a
    contradiction.
    """
    try:
        frame = Frame(basemap)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"WARNING: basemap.json is not usable ({exc}).")
        return ""
    if not basemap.get("freeways"):
        print("WARNING: basemap.json has no road geometry.")
        return ""

    live_points, live_off = plot(listings, geo, LIVE_DOT, frame)
    sold_points, sold_off = plot(sales, geo, SOLD_DOT, frame)
    if not live_points and not sold_points:
        return ""

    # A screen reader gets the summary a sighted reader takes from the
    # picture, since the picture itself says nothing out loud.
    parts = [f"{count} {one if count == 1 else many}" for count, one, many in (
        (len(live_points), "listing for sale", "listings for sale"),
        (len(sold_points), "recent sale", "recent sales")) if count]
    described = " and ".join(parts)

    width, height = frame.width, frame.height

    # The whole basemap is masked by a blurred inset rectangle, so the roads
    # fade out at the edges instead of being guillotined by a border. A hard
    # box around a drawing is what makes it read as a chart rather than a map;
    # dissolving the edge also stops a freeway that leaves the frame from
    # looking like it simply stops. The ids are prefixed because this SVG is
    # inlined into a page that has its own.
    svg = [
        f'<svg class="map" viewBox="0 0 {width:g} {height:g}" role="img" '
        f'aria-label="Map of Houston centred on the 610 loop, showing '
        f'{described}." focusable="false">',
        '  <defs>',
        '    <filter id="msSoften"><feGaussianBlur stdDeviation="5"/></filter>',
        '    <filter id="msHeat" x="-25%" y="-25%" width="150%" height="150%">'
        '<feGaussianBlur stdDeviation="9"/></filter>',
        '    <mask id="msFade">',
        f'      <rect x="4" y="4" width="{width - 8:g}" height="{height - 8:g}" '
        'fill="#fff" filter="url(#msSoften)"/>',
        '    </mask>',
        '  </defs>',
        '  <g mask="url(#msFade)">',
    ]
    # Order is depth: the wash that marks the inside of the Loop is the ground
    # everything else sits on, then water, then roads lightest to heaviest.
    crowded = dense(sold_points + live_points)
    heat = (['    <g class="map-heat" filter="url(#msHeat)">']
            + [f'      <circle cx="{x:.1f}" cy="{y:.1f}" r="{HEAT_R}"/>'
               for x, y in crowded]
            + ['    </g>']) if crowded else []

    svg += ["  " + line for line in
            layer(basemap, "loopFill", "map-loop-fill", closed=True)
            + layer(basemap, "waterArea", "map-water-area", closed=True)
            + layer(basemap, "water", "map-water")]
    # Over the water, under the roads, and blended rather than stacked. Buffalo
    # Bayou runs straight through the middle of the glow, and with the glow
    # underneath it the bayou cut a cold line across a warm patch. Blending
    # lets the two mix, so the water warms where it crosses the concentration
    # instead of interrupting it; the roads still draw crisply over both.
    svg += heat
    svg += ["  " + line for line in
            layer(basemap, "arterials", "map-minor")
            + layer(basemap, "tollways", "map-toll")
            + layer(basemap, "freeways", "map-fwy")
            + layer(basemap, "loop", "map-loop")]
    svg += ['  </g>']

    # Two passes, because these overlap and the order decides what survives.
    # Sales sit at the bottom, stacking into a darker patch wherever several
    # fall on the same few blocks; live listings go over them, since a house
    # for sale in the middle of a block already sold is the thing worth
    # seeing. Each live dot gets a halo first so it still separates from a
    # pile of sales underneath it.
    for x, y in sold_points:
        svg.append(f'  <circle class="map-sold" cx="{x:.1f}" cy="{y:.1f}" '
                   f'r="{SOLD_DOT}"/>')
    for x, y in live_points:
        svg.append(f'  <circle class="map-halo" cx="{x:.1f}" cy="{y:.1f}" '
                   f'r="{LIVE_RING}"/>')
    for x, y in live_points:
        svg.append(f'  <circle class="map-live" cx="{x:.1f}" cy="{y:.1f}" '
                   f'r="{LIVE_DOT}"/>')
    svg.append("</svg>")

    keys = []
    if live_points:
        keys.append('<span class="map-key"><i class="map-dot is-live"></i>'
                    "For sale</span>")
    if sold_points:
        noun = "sale" if len(sold_points) == 1 else "sales"
        keys.append('<span class="map-key"><i class="map-dot is-sold"></i>'
                    f"{len(sold_points)} recent {noun}</span>")
    missing = live_off + sold_off
    if missing:
        keys.append(f'<span class="map-off">{missing} outside this view</span>')

    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">On the map</h2>']
    claim = loop_claim(listings, sales, geo, basemap, frame)
    if claim:
        lines.append(f'{INDENT}  <p class="map-claim">{claim}</p>')
    lines.append(f'{INDENT}  <div class="map-frame">')
    lines.extend(f"{INDENT}    {line}" for line in svg)
    lines.append(f"{INDENT}  </div>")
    lines.append(f'{INDENT}  <p class="map-legend">{"".join(keys)}</p>')
    lines.append(f"{INDENT}</section>")
    return "\n".join(lines)


def loop_claim(listings: list[dict], sales: list[dict], geo: dict,
               basemap: dict, frame: "Frame") -> str:
    """"15 of 24 inside the 610 Loop", or "" if it cannot be worked out.

    Counted over every address we have a coordinate for, including the ones
    that fall outside the frame - Katy and Manvel are emphatically not inside
    the Loop, and quietly dropping them would inflate the fraction.
    """
    ring = loop_ring(basemap)
    if not ring:
        return ""
    inside = total = 0
    for record in list(listings) + list(sales):
        point = geo.get(geo_key(record.get("url", "")) or "")
        if not (isinstance(point, list) and len(point) == 2):
            continue
        try:
            x, y = frame.project(float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            continue
        total += 1
        inside += inside_ring(x, y, ring)
    if not total or not inside:
        return ""
    noun = "home" if total == 1 else "homes"
    return (f'<strong>{inside} of {total}</strong> {noun} inside the 610 Loop')


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
    parser.add_argument("--geo", default=str(REPO_ROOT / "geo.json"))
    parser.add_argument("--basemap", default=str(REPO_ROOT / "basemap.json"))
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
    all_sales = screen(load(pathlib.Path(args.sold), "sold.json"),
                       "sold", sold=True)
    sales = all_sales[:args.sold_limit]

    if sales and not find_region(page, SOLD_START, SOLD_END):
        print(f"WARNING: no sold markers in {page_path.name}; skipping that section.")
        sales = []

    # Either map input failing to load costs the map, not the lists.
    geo = load_object(pathlib.Path(args.geo), "the map has nothing to plot")
    basemap = load_object(pathlib.Path(args.basemap),
                          "the map has nothing to draw on")
    has_map = bool(find_region(page, MAP_START, MAP_END))
    if geo and basemap and not has_map:
        print(f"WARNING: no map markers in {page_path.name}; skipping that section.")

    # A dry run must not touch the filesystem, so it neither fetches nor prunes.
    photo_dir = pathlib.Path(args.photo_dir)
    download = not args.dry_run and not args.no_download

    # Numbers already on the page, ignoring any inside either generated block.
    static = strip_region(strip_region(page, START, END), SOLD_START, SOLD_END)
    first = max((int(n) for n in NUM_RE.findall(static)), default=0) + 1

    section = render(listings, first, photo_dir, download) if listings else ""
    sold_section = render_sold(sales, first + len(listings)) if sales else ""
    map_section = (render_map(listings, all_sales, geo, basemap)
                   if has_map else "")

    hidden = [name for name, rows in (("Current listings", listings),
                                      ("Recently sold", sales),
                                      ("On the map", map_section)) if not rows]
    for name in hidden:
        print(f"HIDING the {name!r} section - no usable data.")

    if args.dry_run:
        print(section or "(listings hidden)")
        print(sold_section or "(sold hidden)")
        print(map_section or "(map hidden)")
        return

    # Pruning against an empty KEEP would delete every mirrored photo, so only
    # prune when we actually rendered listings to compare against.
    if not args.no_download and listings:
        prune(photo_dir)

    updated = splice(page, START, END, section)
    if find_region(updated, SOLD_START, SOLD_END):
        updated = splice(updated, SOLD_START, SOLD_END, sold_section)
    if find_region(updated, MAP_START, MAP_END):
        updated = splice(updated, MAP_START, MAP_END, map_section)

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
