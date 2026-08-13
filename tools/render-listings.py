"""Render listings.json and sold.json into index.html.

Rewrites everything between the `listings:start` / `listings:end` and
`sold:start` / `sold:end` markers in index.html. The rest of the file is
untouched, so this is safe to re-run.

Both sections open with a map of their own contents and continue as plain
rows, in the same idiom as the Profiles / Get in touch / Documents sections.
Row numbers restart at 01 in each section, as the static sections do - they
number a list, they are not a running count of everything on the page.

The maps replaced a photo of the highest-priced listing. One house says one
thing about one house; the same space spent on a map says where the work is,
which is the question a seller is actually asking. Each section illustrates
only its own list - listings above the listings, sales above the sales - so
neither map is making a claim the rows beneath it do not support.

"Recently sold" shows the ten most recent closed sales in the order Compass
itself returns them, though its map plots all of them. The list closes with
an "and N more" link to HAR's sold-by-agent page, so the extra dots are
accounted for rather than left as a discrepancy the page never mentions. Its
prices are last list prices, not sale prices (Texas does not disclose those),
and the note under the section says so.

The maps are inline SVG, drawn from coordinates cached in geo.json by
tools/geocode.py over the freeway and water geometry in basemap.json. Drawn
rather than embedded: no tile provider, no API key, no third-party script, no
extra requests, and they theme off the same CSS variables as everything else
on the page. The inside of the 610 loop is washed with a tint and the count
of addresses falling inside it is stated, because that concentration is the
thing the maps exist to show.

Stdlib only. Runs after tools/geocode.py in the daily workflow.
The basemap it draws on is built separately by tools/basemap.py.

Usage:
    python tools/render-listings.py
    python tools/render-listings.py --dry-run
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

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
START = "<!-- listings:start"
END = "<!-- listings:end -->"
SOLD_START = "<!-- sold:start"
SOLD_END = "<!-- sold:end -->"
INDENT = " " * 6

# Ten is enough to show momentum without doubling the length of the page.
# The map still plots every sale, and render_more_row() links to the rest.
SOLD_LIMIT = 10

# Where "and N more" sends a reader: HAR's sold-by-agent page, which lists
# every closed sale rather than the ten the section shows.
MORE_SOLD_URL = "https://www.har.com/realestatepro/sold-by-agent/spear"

# The same outbound arrow the profile tiles use, in the grid column the other
# rows spend on a price. It marks the row as a way off the page rather than
# another sale; .arr already sizes and colours it.
ARROW = ('<svg class="arr" viewBox="0 0 24 24" fill="none" stroke-width="1.7" '
         'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
         '<path d="M7 17 17 7M9 7h8v8"/></svg>')

# A listing this many days old or newer is flagged "New". Two weeks is long
# enough that a home does not lose the flag over a weekend, short enough that
# the flag still means "just came up" rather than "has been sitting". Measured
# against the day the page is built, from the listedDate the scraper records,
# so the flag falls off on its own without the scraper having to touch a home.
NEW_DAYS = 14

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

# Seconds between one listing's pulse and the next. Long enough that the ring
# from one dot has cleared before its neighbour starts, so a cluster reads as
# several separate listings rather than one throb.
PULSE_STAGGER = 0.9

# How far the map may pull back to reveal an off-frame home before we give up
# and point at the note instead. A home a mile past the edge swims into view
# against roads it is genuinely near; one out in Katy would shrink the city to
# a smudge and surface in empty space, since the basemap is only drawn this far
# out. 1.6x (the view growing by three fifths) clears the near suburbs and
# stops short of the far ones - there is a clean gap in the real data between
# the two. REVEAL_PAD is the breathing room left around the revealed dot, and
# must match the same constant in the zoom script in index.html so the cutoff
# drawn here and the zoom performed there agree.
MAX_REVEAL_ZOOM = 1.6
REVEAL_PAD = 24.0

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


def is_new(listing: dict, today: datetime.date | None = None) -> bool:
    """Whether a listing went up within the last NEW_DAYS days.

    Reads listedDate (YYYY-MM-DD, written by the scraper). A missing or
    unparseable date is simply not new - the flag is a bonus, never a claim we
    would guess at. A date in the future is not new either: "coming soon" is a
    different promise than "new", and this page does not make it.
    """
    raw = listing.get("listedDate")
    if not isinstance(raw, str):
        return False
    try:
        listed = datetime.date.fromisoformat(raw)
    except ValueError:
        return False
    age = ((today or datetime.date.today()) - listed).days
    return 0 <= age <= NEW_DAYS


def meta_line(listing: dict) -> str:
    """"Montrose - 3 bd - 2 ba - 2,105 sqft - Pending", status colored."""
    parts = spec_parts(listing)
    # Active is the default state and adds nothing; call out anything else,
    # flagged with its own color so it doesn't read as just another spec.
    if listing.get("status") and listing["status"].lower() != "active":
        parts.append(f'<span class="row-status">{esc(listing["status"])}</span>')
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


def render_row(listing: dict, number: int, off_map: set = frozenset()) -> list[str]:
    address = f'<span class="row-main">{esc(listing["address"])}</span>'
    # The flag rides beside the address, not down in the spec line, so it is
    # the first thing the eye lands on rather than the last.
    if is_new(listing):
        address += '<span class="row-new">New</span>'
    text = [address,
            f'<span class="row-sub">{meta_line(listing)}</span>']
    showing = open_house_line(listing.get("openHouse", ""))
    if showing:
        text.append(f'<span class="oh-when">{showing}</span>')
    return [
        f'{INDENT}<a class="row"{home_attr(listing, off_map)} href="{esc(listing["url"])}" target="_blank" rel="noopener">',
        f'{INDENT}  <span class="num">{number:02d}</span>',
        f'{INDENT}  <span class="row-text">{"".join(text)}</span>',
        f"{INDENT}  {price_cell(listing)}",
        f"{INDENT}</a>",
    ]


def render_sold_row(sale: dict, number: int, off_map: set = frozenset()) -> list[str]:
    """A closed sale as a plain row: no photo, price labelled by the note."""
    text = [f'<span class="row-main">{esc(sale["address"])}</span>',
            f'<span class="row-sub">{sold_meta_line(sale)}</span>']
    price = sale.get("listPrice")
    cell = (f'<span class="row-price">{money(price)}</span>' if price
            else '<span class="row-price">&mdash;</span>')
    return [
        f'{INDENT}<a class="row"{home_attr(sale, off_map)} href="{esc(sale["url"])}" target="_blank" rel="noopener">',
        f'{INDENT}  <span class="num">{number:02d}</span>',
        f'{INDENT}  <span class="row-text">{"".join(text)}</span>',
        f"{INDENT}  {cell}",
        f"{INDENT}</a>",
    ]


def render_more_row(hidden: int) -> list[str]:
    """The tail of a truncated list, or nothing when nothing was cut.

    The map plots every sale while the list stops at SOLD_LIMIT, so without
    this the page shows more dots than rows and never says why. It is the one
    row that answers for the others, which is also why it carries no
    `data-home`: the hover linking pairs one row to one dot, and this row
    stands for eight homes at once.

    It keeps the row grid so it lines up with the addresses above it, and
    spends the price column on the outbound arrow instead - it is a way off
    the page, not a sale, and should not look like one.
    """
    if hidden < 1:
        return []
    return [
        f'{INDENT}<a class="row row-more" href="{esc(MORE_SOLD_URL)}" target="_blank" rel="noopener">',
        f'{INDENT}  <span class="num" aria-hidden="true"></span>',
        f'{INDENT}  <span class="row-text"><span class="row-main">and {hidden} more</span>'
        f'<span class="row-sub">sold, on HAR.com</span></span>',
        f"{INDENT}  {ARROW}",
        f"{INDENT}</a>",
    ]


def render_sold(sales: list[dict], chart: str = "",
                off_map: set = frozenset(), hidden: int = 0) -> str:
    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">Recently sold in Houston, by Mike Spear</h2>']
    if chart:
        lines.append(chart)
    for number, sale in enumerate(sales, start=1):
        lines.extend("  " + line for line in
                     render_sold_row(sale, number, off_map))
    lines.extend("  " + line for line in render_more_row(hidden))
    lines.append(f'{INDENT}  <p class="mls-note">{esc(SOLD_ATTRIBUTION)}</p>')
    lines.append(f"{INDENT}</section>")
    return "\n".join(lines)


def render(listings: list[dict], chart: str = "",
           off_map: set = frozenset()) -> str:
    lines = [f'{INDENT}<section class="group">',
             f'{INDENT}  <h2 class="label">Current listings in Houston, by Mike Spear</h2>']
    if chart:
        lines.append(chart)

    for number, listing in enumerate(sorted(listings, key=rank), start=1):
        lines.extend("  " + line for line in  # nest inside <section>
                     render_row(listing, number, off_map))

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
         frame: "Frame") -> tuple[list, list]:
    """Project every record we have a coordinate for; name the ones that miss.

    A record with no cached coordinate is not the same as one that falls
    outside the frame. The first is a gap we would want to fix, the second is
    the frame working as designed, so only the second is reported to readers.

    Off-frame homes come back the same shape as the ones inside - (x, y, key) -
    not as a bare tally, because the page needs to know which homes they are and
    where. Hovering such a row pulls the map back until that home's dot swims
    into view, and both the highlight and the zoom need its coordinates. A row
    missing from geo.json has no coordinate and so appears in neither list - it
    is a gap in our own data, not a home the frame chose to exclude.

    Each point keeps the address slug it was plotted from, so the dot and the
    row for the same home can find each other on the page. Position alone is
    not enough to pair them: the list is capped and the map is not, so the two
    are different lengths and an index would line the wrong ones up.
    """
    points, off_frame = [], []
    for record in records:
        key = geo_key(record.get("url", ""))
        point = geo.get(key or "")
        if not (isinstance(point, list) and len(point) == 2):
            continue
        try:
            x, y = frame.project(float(point[0]), float(point[1]))
        except (TypeError, ValueError):
            continue
        (points if frame.holds(x, y, radius) else off_frame).append((x, y, key))
    return points, off_frame


def reveal_scale(x: float, y: float, width: float, height: float) -> float:
    """How far out the view has to zoom to hold both the frame and one dot.

    1.0 is the frame itself; 2.0 is the view grown to twice its width. This is
    the Python side of boxFor() in index.html - the same box, reduced to the
    one number the renderer needs to decide whether a home is close enough to
    be worth revealing. Kept in step with that function so a home drawn as
    revealable here is one the script can actually bring into view.
    """
    ratio = width / height
    x0, x1 = min(0.0, x) - REVEAL_PAD, max(width, x) + REVEAL_PAD
    y0, y1 = min(0.0, y) - REVEAL_PAD, max(height, y) + REVEAL_PAD
    w, h = x1 - x0, y1 - y0
    if w / h <= ratio:      # taller than the frame's shape; width follows height
        w = h * ratio
    return w / width


def geo_key(url: str) -> str | None:
    """The geo.json key for a listing URL: its Compass address slug."""
    match = re.search(r"/homedetails/([^/]+)/", url or "")
    return match.group(1) if match else None


def home_attr(record: dict, off_map: set = frozenset()) -> str:
    """The attributes pairing a row with its dot, or with the legend instead.

    A record whose URL yields no slug has no dot to pair with, and emitting
    an empty attribute would make every such row match every other one.

    `data-offmap` marks the rows the map deliberately does not show, so that
    hovering one lights the "outside this view" note. Only homes the frame
    actually excluded get it. A home missing from geo.json is also dotless,
    but it is not in that count, and saying it is would be a lie told to
    cover a gap in our own data.
    """
    key = geo_key(record.get("url", ""))
    if not key:
        return ""
    return f' data-home="{esc(key)}"' + (" data-offmap" if key in off_map else "")


def dense(points: list) -> list:
    """The points with enough close neighbours to be worth glowing."""
    return [(x, y) for x, y, _ in points
            if sum(1 for ox, oy, _ in points
                   if (ox - x) ** 2 + (oy - y) ** 2 <= HEAT_NEAR ** 2) - 1
            >= HEAT_MIN]


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
               basemap: dict, tag: str) -> tuple[str, set]:
    """A map block to sit at the top of a section, or "" if there is nothing
    worth drawing.

    Called once per section, so `tag` suffixes every id in the SVG. Two maps
    now share a page, and a duplicated mask id means the second map quietly
    reuses the first one's - which looks fine until the two maps stop being
    the same size.

    Sales are plotted from the full list rather than the ten rows shown
    beneath, because the point of the map is the shape of the whole territory.
    The legend says how many it stands for so the two do not read as a
    contradiction.
    """
    try:
        frame = Frame(basemap)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"WARNING: basemap.json is not usable ({exc}).")
        return "", set()
    if not basemap.get("freeways"):
        print("WARNING: basemap.json has no road geometry.")
        return "", set()

    live_points, live_off = plot(listings, geo, LIVE_DOT, frame)
    sold_points, sold_off = plot(sales, geo, SOLD_DOT, frame)
    if not live_points and not sold_points:
        return "", set()

    # An off-frame home is only drawn as a dot if the map can pull back to it
    # without shrinking the city past what is worth showing; the rest are left
    # dotless and fall back to the note. Every off-frame home is still counted
    # as outside the view - the split is only about how its row behaves.
    def near(point: tuple) -> bool:
        return (reveal_scale(point[0], point[1], frame.width, frame.height)
                <= MAX_REVEAL_ZOOM)
    live_reveal = [point for point in live_off if near(point)]
    sold_reveal = [point for point in sold_off if near(point)]

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
        f'    <filter id="msSoften{tag}"><feGaussianBlur stdDeviation="5"/>'
        '</filter>',
        # The heat filter's region is the whole map, not a margin around the
        # discs. A percentage region is measured off the bounding box of
        # whichever discs the data happens to produce, and a blur this wide
        # needs about three standard deviations of room on every side - more
        # than a tight cluster's own box can offer. Falling short doesn't
        # soften the glow, it guillotines it, so the patch picks up straight
        # vertical sides. The map is the only edge that should ever cut it,
        # and the fade mask is already there to dissolve that one.
        f'    <filter id="msHeat{tag}" filterUnits="userSpaceOnUse" x="0" '
        f'y="0" width="{width:g}" height="{height:g}">'
        '<feGaussianBlur stdDeviation="9"/></filter>',
        f'    <mask id="msFade{tag}">',
        f'      <rect x="4" y="4" width="{width - 8:g}" height="{height - 8:g}" '
        f'fill="#fff" filter="url(#msSoften{tag})"/>',
        '    </mask>',
        '  </defs>',
        f'  <g mask="url(#msFade{tag})">',
    ]
    # Order is depth: the wash that marks the inside of the Loop is the ground
    # everything else sits on, then water, then roads lightest to heaviest.
    crowded = dense(sold_points + live_points)
    heat = ([f'    <g class="map-heat" filter="url(#msHeat{tag})">']
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
    for x, y, key in sold_points:
        svg.append(f'  <circle class="map-sold" data-home="{esc(key)}" '
                   f'cx="{x:.1f}" cy="{y:.1f}" r="{SOLD_DOT}"/>')
    # The halo exists to punch a live dot out of a pile of sales underneath
    # it. On the listings map there is nothing underneath, where it stops
    # reading as separation and starts reading as a drop shadow.
    if sold_points:
        for x, y, _ in live_points:
            svg.append(f'  <circle class="map-halo" cx="{x:.1f}" cy="{y:.1f}" '
                       f'r="{LIVE_RING}"/>')

    # A slow ring easing outward from each listing that is currently for sale.
    # The delays are staggered and negative, so the dots are already spread
    # through the cycle on the first frame rather than all firing together -
    # in unison it reads as a machine, offset it reads as activity. The CSS
    # stops the animation outright under prefers-reduced-motion.
    for index, (x, y, _) in enumerate(live_points):
        svg.append(f'  <circle class="map-pulse" cx="{x:.1f}" cy="{y:.1f}" '
                   f'r="{LIVE_DOT}" style="animation-delay:'
                   f'-{index * PULSE_STAGGER:.1f}s"/>')
    for x, y, key in live_points:
        svg.append(f'  <circle class="map-live" data-home="{esc(key)}" '
                   f'cx="{x:.1f}" cy="{y:.1f}" r="{LIVE_DOT}"/>')

    # The homes the frame left out but could still reach are drawn too, at their
    # true position beyond the viewBox - clipped away and invisible until a
    # hovered row pulls the map back far enough to bring one into view (the zoom
    # wiring lives in index.html). data-offmap is the flag that tells that
    # script a dot sits outside the default view, so pointing at its row should
    # zoom rather than light a dot already on screen. Homes past MAX_REVEAL_ZOOM
    # get no dot at all: their rows fall back to lighting the note, which is the
    # older behaviour and the honest one when the map has nothing near to show.
    # Sales under listings, as above.
    for x, y, key in sold_reveal:
        svg.append(f'  <circle class="map-sold is-off" data-home="{esc(key)}" '
                   f'data-offmap cx="{x:.1f}" cy="{y:.1f}" r="{SOLD_DOT}"/>')
    for x, y, key in live_reveal:
        svg.append(f'  <circle class="map-live is-off" data-home="{esc(key)}" '
                   f'data-offmap cx="{x:.1f}" cy="{y:.1f}" r="{LIVE_DOT}"/>')
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
        keys.append(f'<span class="map-off">{len(missing)} '
                    "outside this view</span>")

    inner = INDENT + "  "
    lines = [f'{inner}<div class="map-frame">']
    lines.extend(f"{inner}  {line}" for line in svg)
    lines.append(f"{inner}</div>")
    if keys:
        lines.append(f'{inner}<p class="map-legend">{"".join(keys)}</p>')
    # The slugs travel back with the drawing so the rows can be marked. With
    # no legend there is nothing for such a row to reach for, so the set is
    # empty. The off lists are (x, y, key) triples now, so pull the key out.
    off_map = {key for _, _, key in missing if key} if keys else set()
    return "\n".join(lines), off_map


def find_region(page: str, start_marker: str, end_marker: str):
    """(start, end) offsets of a generated region, or None if absent."""
    start = page.find(start_marker)
    end = page.find(end_marker)
    if start == -1 or end == -1 or end < start:
        return None
    return start, end


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
    parser.add_argument("--sold-limit", type=int, default=SOLD_LIMIT,
                        help=f"how many closed sales to show (default {SOLD_LIMIT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the generated sections instead of writing them")
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

    # Either map input failing to load costs the maps, not the lists.
    geo = load_object(pathlib.Path(args.geo), "the maps have nothing to plot")
    basemap = load_object(pathlib.Path(args.basemap),
                          "the maps have nothing to draw on")

    # Each section gets a map of its own list and nothing else. The suffixes
    # keep the two SVGs' ids apart.
    live_map, live_off_map = (render_map(listings, [], geo, basemap, "a")
                              if listings else ("", set()))
    sold_map, sold_off_map = (render_map([], all_sales, geo, basemap, "b")
                              if all_sales else ("", set()))

    section = (render(listings, live_map, live_off_map)
               if listings else "")
    # Counted rather than assumed: the cap is a maximum, not a promise, and on
    # a thin week there is nothing beyond it and so no line to write.
    beyond_cap = len(all_sales) - len(sales)
    sold_section = (render_sold(sales, sold_map, sold_off_map, beyond_cap)
                    if sales else "")

    hidden = [name for name, rows in (("Current listings", listings),
                                      ("Recently sold", sales)) if not rows]
    for name in hidden:
        print(f"HIDING the {name!r} section - no usable data.")

    # A section that renders its rows but loses its map is not worth hiding -
    # the rows are the substance. It is still worth an alarm, because it means
    # geo.json or basemap.json is broken and nobody would otherwise notice.
    mapless = [name for name, rows, drawn in
               (("Current listings", listings, live_map),
                ("Recently sold", sales, sold_map)) if rows and not drawn]
    for name in mapless:
        print(f"WARNING: the {name!r} section rendered without its map.")

    if args.dry_run:
        print(section or "(listings hidden)")
        print(sold_section or "(sold hidden)")
        return

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
    # workflow publishes the degraded state first, then fails the run to raise
    # the alarm. Anything else would either hide silently or skip the fix.
    if hidden or mapless:
        sys.exit(3)


if __name__ == "__main__":
    main()
