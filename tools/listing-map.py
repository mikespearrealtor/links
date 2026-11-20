"""Draw the neighbourhood map for each listing page.

Every listing page has a full-bleed street map of the blocks around the
house: the roads, parks and water, the restaurants, coffee, bars, grocery,
schools, parks and hospital closest to it, and the house itself. Nothing on
it is picked by hand, so a new listing gets its map the day it appears.

  * Data comes from OpenStreetMap through the Overpass API, fetched once per
    listing. The finished map is committed to listing-maps/<slug>.html and a
    listing that already has one makes no network call at all, so a normal
    day costs nothing.
  * Places are chosen by rule: per category, the nearest named places inside
    the frame, one of each chain, up to a fixed count. A label that cannot be
    placed without covering another is dropped rather than drawn on top.
  * Every place links to Google Maps by its OpenStreetMap street address
    (so a chain opens that exact store), or failing that by its name and the
    street it sits on, searched around its own coordinates.
  * Street names, neighbourhood names, water names and freeway shields are
    placed automatically along the straightest stretch of each road.

Two drawings per listing: a wide one for computers, rendered far wider than
any window so the page can crop it to the screen (preserveAspectRatio slice),
and a tighter, taller one for phones. Text is measured from width tables of
the site's own fonts (tools/font-widths.json), so this stays stdlib only.

Usage:
    python tools/listing-map.py              # maps for listings that lack one
    python tools/listing-map.py --rebuild    # redraw every map (refetches)
    python tools/listing-map.py --only 1265-du-barry-lane
"""

import argparse
import gzip
import json
import math
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "listing-maps"
# Building outlines traced from satellite imagery (Microsoft Global ML
# Building Footprints), as [[lat, lon], ...] rings. Off (empty) unless set:
# drawn faintly under the streets, and used for a school or store that
# OpenStreetMap has only as a point.
FOOTPRINTS: list = []
# The Houston tile of that dataset (quadkey 023131022, 138 MB). Kept outside
# the repo; with it missing the maps simply go without buildings.
FOOTPRINT_DIR = pathlib.Path(os.environ.get("FOOTPRINT_DIR") or pathlib.Path.home() / "Documents" / "Claude Misc" / "ms-building-footprints")
FOOTPRINT_INDEX = "https://minedbuildings.z5.web.core.windows.net/global-buildings/dataset-links.csv"
# How the building layer is drawn: "all" (across the map), "near" (only
# around the house, fading out), "wide" (computer map only) or "none"
# (outlines for schools and stores only).
FOOTPRINT_LAYER = "wide"
FOOTPRINT_NEAR_M = (650, 1000)  # solid out to the first distance, gone by the second
WIDTHS = json.loads((ROOT / "tools" / "font-widths.json").read_text(encoding="utf-8"))

OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Frames, as half-spans in degrees around the house. The core is what must
# stay on screen on a computer; the wide drawing runs on east and west of it
# at the same scale so wide windows get more streets instead of margins.
CORE_LON, CORE_LAT, CORE_W = 0.02475, 0.0122, 1600
WIDE_LON = 0.0755
TALL_LON, TALL_LAT, TALL_W = 0.0093, 0.0094, 460
FETCH_LAT = CORE_LAT + 0.0015

CATS = {
    "eat": "#B5432E", "coffee": "#8B5A2B", "drink": "#6C3E7C", "shop": "#1D6F6A", "grocery": "#1D6F6A",
    "school": "#2A5B9D", "park": "#3D7C39", "health": "#A8325E",
}
# how many of each category the computer map and the phone map show
CAPS_WIDE = {"landmark": 7, "eat": 8, "coffee": 6, "drink": 4, "grocery": 4, "shop": 1, "school": 6, "park": 4, "health": 1}
CAPS_TALL = {"landmark": 3, "eat": 2, "coffee": 2, "drink": 1, "grocery": 2, "shop": 0, "school": 3, "park": 2, "health": 0}
# Buyers ask about the grocery store and the schools first, so these are
# placed right after the landmarks (never the ones squeezed out) and drawn a
# little heavier than the restaurants and bars.
KEY_CATS = {"grocery", "school"}
# Grocery stores people know by name come ahead of a closer corner store
# that OpenStreetMap happens to tag as a supermarket.
GROCERY_CHAINS = re.compile(r"h-?e-?b|whole foods|kroger|aldi|trader joe|randalls|sprouts|central market|fiesta|"
                            r"costco|sam's club|phoenicia|rice epicurean|food town|sellers bros|el rancho|joe v|"
                            r"99 ranch|h mart|hong kong food|walmart|natural grocers|tom thumb|el super|la michoacana", re.I)

# The can't-miss places (tools/landmarks.json, kept by tools/landmarks.py):
# whichever fall inside the frame are drawn before anything else, so a house
# by Hermann Park says so at first glance. The big ones are drawn as their
# real outline with the name set inside it; the rest get a bold label.
_MARKS = json.loads((ROOT / "tools" / "landmarks.json").read_text(encoding="utf-8"))
LANDMARKS = [m for m in _MARKS if m.get("kind") != "trail"]
# Trails and greenways: being a walk from one is a big draw in Houston, but a
# greenway is dozens of OpenStreetMap paths (often one per bank), so they are
# found by the phrases their names contain, then joined and drawn as one line.
TRAILS = [m for m in _MARKS if m.get("kind") == "trail"]
TRAIL_GREEN = "#3D7C39"
TRAIL_MIN_M = 300      # shorter pieces are a footbridge or a stub, not the trail
TRAIL_BANKS_M = 70     # a path this close to one already drawn is the other bank
LM_KIND = {  # kind: (dot and name colour, outline fill, outline edge)
    "park": ("#3D7C39", "#DCE8D5", "#A9C3A0"), "campus": ("#2A5B9D", "#DFE6F1", "#AEBFD9"),
    "health": ("#A8325E", "#F2E0E7", "#D7AEBE"), "sight": ("#2B2B2B", "#E2D9C6", "#A99A7C"),
}
# which everyday kinds a landmark of each kind stands in for, when the same
# place also turns up among the restaurants, schools and parks
LM_SAME = {"park": {"park"}, "campus": {"school"}, "health": {"health"}, "sight": {"eat", "coffee", "drink", "grocery", "shop"}}

LIGHT_ROADS = {  # class: (fill width, edge width, fill, edge)
    "motorway": (10.0, 10.0, "#6a6a6a", "#6a6a6a"),
    "motorway_link": (3.4, 3.4, "#8E8E8E", "#8E8E8E"),
    "primary": (6.4, 8.2, "#fff", "#7A7A7A"),
    "secondary": (5.4, 7.0, "#fff", "#8A8A8A"),
    "frontage": (3.2, 4.6, "#fff", "#A6A6A6"),
    "tertiary": (4.2, 5.6, "#fff", "#959595"),
    "link": (2.2, 3.4, "#fff", "#B4B4B4"),
    "residential": (2.3, 3.5, "#fff", "#B4B4B4"),
}
ORDER = ["residential", "link", "frontage", "tertiary", "secondary", "primary", "motorway_link", "motorway"]

# Scoped under .lm so the map's short class names cannot meet the page's.
CSS = ".lm text{font-family:\"InterVar\",Arial,sans-serif;font-weight:300;fill:#1d1d1d}" \
      ".lm .st{fill:#4a4a4a;paint-order:stroke;stroke:#fff;stroke-width:3.4px;stroke-linejoin:round}" \
      ".lm .st.maj{fill:#000;font-weight:500}" \
      ".lm .pl{font-size:12.5px;fill:#1d1d1d}.lm .pl.lk{font-weight:600}.lm .pl.key{font-weight:500}" \
      ".lm .lmn{font-family:\"Fraunces\",Georgia,serif;font-style:italic;font-weight:600;paint-order:stroke;stroke-width:3.5px;stroke-linejoin:round}" \
      ".lm .hint{font-family:\"Fraunces\",Georgia,serif;font-style:italic;font-weight:600;font-size:11.8px;fill:#111}" \
      ".lm .hl{fill:#fff;font-size:14.5px;font-weight:600}" \
      ".lm .hw{fill:#fff;font-size:10.5px;font-weight:600}" \
      ".lm .wt{font-family:\"Fraunces\",Georgia,serif;font-style:italic;font-weight:600;font-size:13px;fill:#111;paint-order:stroke;stroke:#fff;stroke-width:4px;stroke-linejoin:round}" \
      ".lm .nb{font-family:\"Fraunces\",Georgia,serif;font-style:italic;font-weight:500;font-size:25px;fill:#5a5a5a;paint-order:stroke;stroke:#fff;stroke-width:5px;stroke-linejoin:round}" \
      ".lm .sc{font-size:11px;fill:#1d1d1d;paint-order:stroke;stroke:#fff;stroke-width:4px;stroke-linejoin:round}" \
      ".lm a.lm-home{cursor:pointer}.lm a.lm-home rect,.lm a.lm-home circle{transition:fill .2s ease}" \
      ".lm a.lm-home:hover rect,.lm a.lm-home:focus-visible rect{fill:#444}" \
      ".lm a.lm-home:hover circle:last-of-type{fill:#444}.lm a.lm-home:focus{outline:none}"


def log(msg: str) -> None:
    print(msg, flush=True)


def text_w(s: str, size: float, font: str = "inter300") -> float:
    table = WIDTHS[font]
    return sum(table.get(c, 58) for c in s) * size / 100


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace('"', "&quot;")


def slug_of(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")


def geo_key(url: str) -> str | None:
    m = re.search(r"/homedetails/([^/]+)/", url or "")
    return m.group(1) if m else None


# ── Fetch ────────────────────────────────────────────────────────────────
def overpass(query: str) -> dict:
    body = urllib.parse.urlencode({"data": query}).encode()
    last = None
    for attempt in range(3):
        for url in OVERPASS:
            try:
                req = urllib.request.Request(url, data=body, headers={"User-Agent": "listing-map/1.0"})
                raw = urllib.request.urlopen(req, timeout=120).read()
                if not raw.lstrip().startswith(b"{"):
                    raise ValueError("not JSON: " + raw[:80].decode("latin1"))
                return json.loads(raw)
            except Exception as e:  # try the next mirror
                last = e
                log(f"  overpass {url.split('/')[2]}: {e}")
        time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Overpass unavailable: {last}")


def fetch_stores(lat: float, lon: float) -> list:
    """Grocery store buildings: stores mapped as their own outline, and the
    building under each store mapped as a single point."""
    cb = f"({lat - CORE_LAT},{lon - CORE_LON},{lat + CORE_LAT},{lon + CORE_LON})"
    return overpass(
        f'[out:json][timeout:90];(way["shop"="supermarket"]{cb};relation["shop"="supermarket"]{cb};)->.a;'
        f'node["shop"="supermarket"]{cb}->.n;way(around.n:25)["building"]->.b;(.a;.b;);out geom;')["elements"]


def trail_of(name: str):
    """The curated trail a path's name belongs to, or None."""
    n = name.lower()
    return next((t for t in TRAILS if any(v in n for v in t["names"])), None)


def fetch_trails(lat: float, lon: float) -> list:
    """Named paths that belong to one of the curated trails, in the wide frame."""
    s, n = lat - FETCH_LAT, lat + FETCH_LAT
    w, e = lon - WIDE_LON - 0.002, lon + WIDE_LON + 0.002
    pat = "|".join(re.escape(v) for t in TRAILS for v in t["names"])
    els = overpass(f'[out:json][timeout:90];way["highway"~"^(cycleway|footway|path|track|bridleway)$"]'
                   f'["name"~"({pat})",i]({s},{w},{n},{e});out tags geom;')["elements"]
    return [e for e in els if trail_of(e.get("tags", {}).get("name", ""))]


def fetch(lat: float, lon: float) -> tuple[list, list, list, list, list]:
    s, n = lat - FETCH_LAT, lat + FETCH_LAT
    w, e = lon - WIDE_LON - 0.002, lon + WIDE_LON + 0.002
    bb = f"({s},{w},{n},{e})"
    roads = overpass(
        f'[out:json][timeout:90];(way["highway"~"^(motorway|trunk|primary|secondary|tertiary|residential|unclassified|living_street|motorway_link|trunk_link|primary_link|secondary_link|tertiary_link)$"]{bb};'
        f'way["railway"="rail"]{bb};);out geom;')["elements"]
    land = overpass(
        f'[out:json][timeout:90];(way["leisure"~"^(park|golf_course|nature_reserve)$"]{bb};relation["leisure"~"^(park|golf_course)$"]{bb};'
        f'way["landuse"~"^(recreation_ground|cemetery|forest)$"]{bb};way["natural"~"^(water|wood)$"]{bb};relation["natural"="water"]{bb};'
        f'way["waterway"~"^(river|stream|canal)$"]{bb};);out geom;')["elements"]
    cb = f"({lat - CORE_LAT},{lon - CORE_LON},{lat + CORE_LAT},{lon + CORE_LON})"
    pois = overpass(
        f'[out:json][timeout:90];(nwr["amenity"~"^(restaurant|cafe|bar|pub|biergarten|ice_cream|school|library|hospital)$"]["name"]{cb};'
        f'nwr["shop"~"^(supermarket|bakery|pastry|coffee|doityourself)$"]["name"]{cb};nwr["craft"="brewery"]["name"]{cb};'
        f'nwr["leisure"="park"]["name"]{cb};node["place"~"^(neighbourhood|quarter)$"]["name"]{cb};);out center tags;')["elements"]
    return roads, land, pois, fetch_stores(lat, lon), fetch_trails(lat, lon)


# ── Geometry helpers ─────────────────────────────────────────────────────
class Frame:
    def __init__(self, lat, lon, half_lon, half_lat, width):
        self.cos = math.cos(math.radians(lat))
        self.w_lon, self.n_lat = lon - half_lon, lat + half_lat
        self.k = width / (2 * half_lon * self.cos)
        self.cw = width
        self.ch = round(2 * half_lat * self.k)

    def xy(self, lat, lon):
        return ((lon - self.w_lon) * self.cos * self.k, (self.n_lat - lat) * self.k)

    def inside(self, x, y, pad=0):
        return -pad <= x <= self.cw + pad and -pad <= y <= self.ch + pad


def point_in(x, y, ring):
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def rdp(pts, tol):
    if len(pts) < 3:
        return pts
    (ax, ay), (bx, by) = pts[0], pts[-1]
    dx, dy = bx - ax, by - ay
    L = math.hypot(dx, dy)
    i, dmax = 0, -1.0
    for j in range(1, len(pts) - 1):
        # a closed ring starts and ends on the same point: measure from it,
        # or every point reads as on the line and the ring collapses to nothing
        d = abs(dy * (pts[j][0] - ax) - dx * (pts[j][1] - ay)) / L if L else math.hypot(pts[j][0] - ax, pts[j][1] - ay)
        if d > dmax:
            i, dmax = j, d
    if dmax <= tol:
        return [pts[0], pts[-1]]
    return rdp(pts[:i + 1], tol)[:-1] + rdp(pts[i:], tol)


def clip_runs(pts, fr, pad=30):
    """Split a polyline into the stretches that are on (or next to) the frame."""
    runs, cur = [], []
    for i, p in enumerate(pts):
        near = fr.inside(*p, pad) or (i > 0 and fr.inside(*pts[i - 1], pad)) or (i + 1 < len(pts) and fr.inside(*pts[i + 1], pad))
        if near:
            cur.append(p)
        elif cur:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)
    return [r for r in runs if len(r) > 1]


def path_d(p, close=False):
    return "M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in p) + ("Z" if close else "")


def overlap(a, b, m=0):
    return a[0] < b[2] + m and b[0] < a[2] + m and a[1] < b[3] + m and b[1] < a[3] + m


def straight_runs(poly, max_turn=9):
    """Stretches of a polyline that stay within max_turn degrees of their start heading."""
    out, i = [], 0
    while i < len(poly) - 1:
        (x0, y0), (x1, y1) = poly[i], poly[i + 1]
        h0 = math.atan2(y1 - y0, x1 - x0)
        j, length = i + 1, math.hypot(x1 - x0, y1 - y0)
        while j < len(poly) - 1:
            (a, b), (c, d) = poly[j], poly[j + 1]
            h = math.atan2(d - b, c - a)
            turn = abs((math.degrees(h - h0) + 180) % 360 - 180)
            if turn > max_turn:
                break
            length += math.hypot(c - a, d - b)
            j += 1
        out.append((length, poly[i:j + 1]))
        i = j
    return out


def point_at(run, frac=0.5):
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(run, run[1:]))
    goal = total * frac
    for a, b in zip(run, run[1:]):
        seg = math.hypot(b[0] - a[0], b[1] - a[1])
        if goal <= seg and seg > 0:
            t = goal / seg
            return a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
        goal -= seg
    return run[-1]


ABBR = [(r"^West ", "W "), (r"^East ", "E "), (r"^North ", "N "), (r"^South ", "S "),
        (r" Street$", " St"), (r" Boulevard$", " Blvd"), (r" Drive$", " Dr"), (r" Road$", " Rd"),
        (r" Lane$", " Ln"), (r" Avenue$", " Ave"), (r" Parkway$", " Pkwy"), (r" Freeway$", " Fwy"),
        (r" Court$", " Ct"), (r" Place$", " Pl"), (r" Circle$", " Cir"), (r" Highway$", " Hwy"),
        (r" Trail$", " Trl"), (r" Terrace$", " Ter")]


def abbreviate(name: str) -> str:
    for a, b in ABBR:
        name = re.sub(a, b, name)
    return name


def join_lines(lines, tol):
    """Chain polylines whose ends meet (within tol) into longer ones."""
    lines = [list(l) for l in lines]
    joined = True
    while joined:
        joined = False
        for i in range(len(lines)):
            for j in range(i + 1, len(lines)):
                a, b = lines[i], lines[j]
                for a2, b2 in ((a, b), (a, b[::-1]), (a[::-1], b), (a[::-1], b[::-1])):
                    if math.hypot(a2[-1][0] - b2[0][0], a2[-1][1] - b2[0][1]) <= tol:
                        lines[i] = a2 + b2[1:]
                        del lines[j]
                        joined = True
                        break
                if joined:
                    break
            if joined:
                break
    return lines


def line_len(l):
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(l, l[1:]))


def densify(l, step):
    out = [l[0]]
    for a, b in zip(l, l[1:]):
        n = max(1, int(math.hypot(b[0] - a[0], b[1] - a[1]) / step))
        out += [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(1, n + 1)]
    return out


def merge_banks(lines, gap):
    """Longest first; of each later line keep only the stretches farther than
    gap from what is kept, so paths on both banks of a bayou draw as one."""
    grid = {}
    kept = []
    cell = lambda x, y: (int(x // gap), int(y // gap))

    def near(x, y):
        cx, cy = cell(x, y)
        return any(math.hypot(x - qx, y - qy) < gap
                   for i in (-1, 0, 1) for j in (-1, 0, 1) for qx, qy in grid.get((cx + i, cy + j), ()))

    for l in sorted(lines, key=line_len, reverse=True):
        run, runs = [], []
        for q in densify(l, gap / 6):
            if near(*q):
                if len(run) > 1:
                    runs.append(run)
                run = []
            else:
                run.append(q)
        if len(run) > 1:
            runs.append(run)
        for r in runs:
            kept.append(r)
            for q in r:
                grid.setdefault(cell(*q), []).append(q)
    return kept


# ── Places ───────────────────────────────────────────────────────────────
def category(t: dict) -> tuple[str, str] | None:
    """(category, hint) for a POI's tags, or None to leave it off."""
    a, s, n = t.get("amenity"), t.get("shop"), t.get("name", "").lower()
    has = lambda *words: any(w in n for w in words)
    if a == "restaurant":
        return "eat", ""
    if a == "cafe" or s == "coffee":
        return "coffee", "" if has("coffee", "café", "cafe", "espresso", "roast", "tea", "java", "brew") else "(coffee)"
    if a == "ice_cream":
        return "coffee", "" if has("ice cream", "gelato", "creamery", "frozen", "paleta", "yogurt") else "(ice cream)"
    if s in ("bakery", "pastry"):
        return "coffee", "" if has("bake", "bakery", "donut", "do-nut", "pastr", "kolache", "bread", "cake", "boulangerie", "patisserie") else "(bakery)"
    if a in ("bar", "pub", "biergarten") or t.get("craft") == "brewery":
        return "drink", "" if has("bar", "pub", "brew", "tavern", "saloon", "lounge", "wine", "beer", "ice house", "icehouse", "taproom", "tap", "cocktail", "spirits", "distill") else "(bar)"
    if s == "supermarket":
        return "grocery", "" if has("market", "grocery", "foods", "h-e-b", "heb") else "(grocery)"
    if s == "doityourself":
        return "shop", ""
    if a == "school":
        return "school", "" if has("school", "elementary", "academy", "middle", "high", "prep", "montessori", "college", "university") else "(school)"
    if a == "library":
        return "school", "" if has("library") else "(library)"
    if t.get("leisure") == "park":
        # Whole words only: "T C Jester Parkway" is a park, but reads as a road.
        parky = re.search(r"\b(park|parks|green|garden|gardens|plaza|square|playground|field|fields|commons|greenway|"
                          r"trail|preserve|arboretum|nature|sanctuary|reserve|pocket park)\b", n)
        return "park", "" if parky else "(park)"
    if a == "hospital":
        # Only a hospital you would drive to in an emergency: surgical,
        # rehab, psychiatric and other specialty hospitals are left off.
        if t.get("emergency") == "no" or re.search(SPECIALTY, n):
            return None
        return "health", "" if has("hospital", "medical center", "emergency") else "(hospital)"
    return None


SPECIALTY = (r"\b(pain|spine|surgical|surgery|surgi|rehab|rehabilitation|behavioral|psychiatric|mental|orthopa?edic|"
             r"specialty|long[- ]term|ltac|kindred|select|cancer|heart|eye|women|children|shriners|veterans|"
             r"hospice|dialysis|sleep|plastic|bariatric|fertility|vein)\b")


def clean_name(name: str) -> str:
    name = re.sub(r" Elementary School$", " Elementary", name)
    # co-branded stores ("Marble Slab Creamery/Great American Cookies"): the first brand
    if "/" in name and not re.search(r"\d/\d", name):
        name = name.split("/")[0]
    name = re.sub(r"\s+", " ", name).strip()
    return name


def split_long(name: str, limit: int = 30) -> list[str]:
    if len(name) <= limit:
        return [name]
    words, best = name.split(" "), None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(a), len(b))
        if best is None or score < best[0]:
            best = (score, [a, b])
    return best[1]


def nearest_street(roads, lat, lon, within=90):
    best = (within, None)
    for e in roads:
        t = e.get("tags", {})
        nm = t.get("name")
        if not nm or "highway" not in t or t["highway"].startswith("motorway"):
            continue
        g = [((q["lon"] - lon) * 96000, (q["lat"] - lat) * 111000) for q in e.get("geometry") or []]
        for (ax, ay), (bx, by) in zip(g, g[1:]):
            dx, dy = bx - ax, by - ay
            u = max(0, min(1, -(ax * dx + ay * dy) / (dx * dx + dy * dy or 1)))
            d = math.hypot(ax + u * dx, ay + u * dy)
            if d < best[0]:
                best = (d, nm)
    return best[1]


def maps_link(name, tags, lat, lon, roads):
    city = tags.get("addr:city") or "Houston"
    if tags.get("addr:housenumber") and tags.get("addr:street"):
        q = f'{name}, {tags["addr:housenumber"]} {tags["addr:street"]}, {city}, TX {tags.get("addr:postcode", "")}'.strip()
        return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(q)
    street = nearest_street(roads, lat, lon)
    q = f"{name}, {street}, {city}, TX" if street else f"{name}, {city}, TX"
    return f"https://www.google.com/maps/search/{urllib.parse.quote(q)}/@{lat:.5f},{lon:.5f},17z"


def park_areas(land) -> dict:
    """Largest mapped area, in square metres, of each named park."""
    out = {}
    for e in land:
        t = e.get("tags", {})
        if t.get("leisure") != "park" or not t.get("name") or e.get("type") != "way":
            continue
        g = e.get("geometry") or []
        if len(g) < 3:
            continue
        pts = [(q["lon"] * 96000, q["lat"] * 111000) for q in g]
        a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2
        out[t["name"]] = max(a, out.get(t["name"], 0))
    return out


COMMON = {"park", "parks", "school", "elementary", "middle", "high", "academy", "library", "neighborhood", "center",
          "campus", "coffee", "cafe", "restaurant", "grill", "kitchen", "house", "market", "bakery", "street", "houston",
          "early", "college", "the", "and", "bar", "pizza", "taco", "tacos", "food", "foods", "brewing", "company"}
MIN_PARK_M2 = 6000   # leaves off esplanades and street-corner triangles


def pick_places(pois, roads, lat, lon, frame, caps, parks=None, marks=()):
    """Nearest named places per category inside the frame, one per chain."""
    cand = {}
    parks = parks or {}
    words = lambda nm: {w for w in re.findall(r"[a-z']{4,}", nm.lower()) if w not in COMMON}
    norm = lambda nm: re.sub(r"[^a-z0-9]", "", nm.lower())

    def a_landmark(name, cat, la, lo):
        """True when a landmark on the map already stands for this place."""
        for m in marks:
            if norm(m["name"]) == norm(name):
                return True
            if cat not in LM_SAME[m["kind"]] or math.hypot((m["lat"] - la) * 111000, (m["lon"] - lo) * 96000) > 400:
                continue
            a, b = words(name), words(m["name"])
            if a and b and (a <= b or b <= a):
                return True
        return False
    for e in pois:
        t = e.get("tags", {})
        c = category(t)
        if not c:
            continue
        la, lo = (e["lat"], e["lon"]) if "lat" in e else (e["center"]["lat"], e["center"]["lon"])
        x, y = frame.xy(la, lo)
        if not frame.inside(x, y, -40):
            continue
        if c[0] == "park" and parks.get(t["name"], MIN_PARK_M2) < MIN_PARK_M2:
            continue
        name = clean_name(t["name"])
        if not re.search(r"[A-Za-z]{2}", name):  # "1", "#3": a unit number, not a name
            continue
        if a_landmark(name, c[0], la, lo):
            continue
        d = math.hypot((la - lat) * 111000, (lo - lon) * 96000)
        if d < 25:  # the listing itself, or something sharing its lot
            continue
        key = re.sub(r"[^a-z0-9]", "", name.lower())
        if key in cand and cand[key]["d"] <= d:
            continue
        cand[key] = dict(name=name, hint=c[1], cat=c[0], lat=la, lon=lo, d=d, tags=t,
                         key=c[0] in KEY_CATS and t.get("amenity") != "library")
    # Fold near-duplicates: two places of a kind within 250 m sharing a
    # distinctive word are one place mapped twice ("Clayton House" and the
    # "Family History Research Center at the Clayton Library Campus").
    ranked = []
    for cat, cap in caps.items():
        if cat == "landmark":
            continue
        group = []
        order = (lambda p: (not GROCERY_CHAINS.search(p["name"]), p["d"])) if cat == "grocery" else (lambda p: p["d"])
        for p in sorted((p for p in cand.values() if p["cat"] == cat), key=order):
            if any(words(p["name"]) & words(q["name"]) and math.hypot((p["lat"] - q["lat"]) * 111000, (p["lon"] - q["lon"]) * 96000) < 250
                   for q in group):
                continue
            group.append(p)
        # three spares per kind, so a label with no room gives way to the next nearest
        for rank, p in enumerate(group[:cap + 3]):
            p["rank"] = rank
            ranked.append(p)
    for p in ranked:
        p["href"] = maps_link(p["name"], p["tags"], p["lat"], p["lon"], roads)
    return ranked


def quadkey(lat, lon, level=9):
    """Bing-maps quadkey of the tile holding a point; Microsoft's footprints
    come in one file per level-9 tile."""
    lat = max(min(lat, 85.05112878), -85.05112878)
    x = (lon + 180) / 360
    sl = math.sin(math.radians(lat))
    y = 0.5 - math.log((1 + sl) / (1 - sl)) / (4 * math.pi)
    n = 1 << level
    tx, ty = min(int(x * n), n - 1), min(int(y * n), n - 1)
    return "".join(str(((tx >> i) & 1) + 2 * ((ty >> i) & 1)) for i in range(level - 1, -1, -1))


def footprint_tile(qk):
    """The footprint file for one tile, downloaded the first time it's needed
    (about 50-150 MB for a city tile) and kept in FOOTPRINT_DIR. None when it
    can't be had; the map then simply goes without buildings."""
    path = FOOTPRINT_DIR / f"{qk}.csv.gz"
    if qk == "023131022" and not path.exists() and (FOOTPRINT_DIR / "houston.csv.gz").exists():
        return FOOTPRINT_DIR / "houston.csv.gz"   # the first Houston download
    if path.exists():
        return path
    try:
        FOOTPRINT_DIR.mkdir(parents=True, exist_ok=True)
        index = FOOTPRINT_DIR / "dataset-links.csv"
        if not index.exists():
            urllib.request.urlretrieve(FOOTPRINT_INDEX, index)
        url = next((row.split(",")[2] for row in index.read_text(encoding="utf-8").splitlines()
                    if row.startswith("UnitedStates,") and row.split(",")[1] == qk), None)
        if not url:
            log(f"no building footprints published for tile {qk}")
            return None
        log(f"downloading building footprints for tile {qk}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(url, tmp)
        tmp.replace(path)
        return path
    except OSError as exc:
        log(f"building footprints for tile {qk} unavailable ({exc}); map goes without them")
        return None


def load_footprints(lat, lon, cache=None):
    """Footprints across a listing's core frame (the phone map lies inside
    it), read from its tile(s) in one pass each and cached next to the map
    data. A frame on a tile edge reads both tiles."""
    hit = cache / f"fp-{lat:.5f},{lon:.5f}.json" if cache else None
    if hit and hit.exists():
        return json.loads(hit.read_text(encoding="utf-8"))
    s, n = lat - CORE_LAT - 0.0015, lat + CORE_LAT + 0.0015
    w, e = lon - CORE_LON - 0.002, lon + CORE_LON + 0.002
    tiles = sorted({quadkey(a, b) for a in (s, n) for b in (w, e)})
    out = []
    for qk in tiles:
        tile = footprint_tile(qk)
        if not tile:
            continue
        with gzip.open(tile, "rt", encoding="utf-8") as f:
            for line in f:
                i = line.find("[[[")
                if i < 0:
                    continue
                x, y = map(float, line[i + 3:line.find("]", i)].split(","))
                if w < x < e and s < y < n:
                    ring = json.loads(line)["geometry"]["coordinates"][0]
                    out.append([[round(b, 6), round(a, 6)] for a, b in ring])
    if hit:
        hit.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
    return out


def footprint_at(lat, lon, min_m2=400, near_m=60):
    """The footprint a place sits in, else the biggest one close by."""
    x, y = lon * 96000, lat * 111000
    best = None
    for ring in FOOTPRINTS:
        if abs(ring[0][0] - lat) > 0.001 or abs(ring[0][1] - lon) > 0.001:
            continue
        pts = [(b * 96000, a * 111000) for a, b in ring]
        a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2
        if a < min_m2:
            continue
        inside = False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        cx, cy = sum(q[0] for q in pts) / len(pts), sum(q[1] for q in pts) / len(pts)
        if inside or math.hypot(cx - x, cy - y) <= near_m:
            rank = (not inside, -a)
            if best is None or rank < best[0]:
                best = (rank, ring)
    return [{"lat": a, "lon": b} for a, b in best[1]] if best else None


def store_outline(q, stores):
    """The building a grocery store is in, as lat/lon points, or None."""
    m2 = lambda g: [(p["lon"] * 96000, p["lat"] * 111000) for p in g]
    area = lambda pts: abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2

    def holds(pts, x, y):
        inside = False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                inside = not inside
        return inside

    x, y = q["lon"] * 96000, q["lat"] * 111000
    best = None
    for e in stores:
        g = e.get("geometry")
        if e.get("type") == "relation":
            g = next((mb["geometry"] for mb in e.get("members", []) if mb.get("role") == "outer" and mb.get("geometry")), None)
        if not g or len(g) < 4:
            continue
        pts = m2(g)
        a = area(pts)
        if not 300 <= a <= 60000:  # a shop, not a strip-centre parcel or a shed
            continue
        t = e.get("tags", {})
        if t.get("shop") == "supermarket" and t.get("name") and clean_name(t["name"]) == q["name"]:
            return g
        if holds(pts, x, y):
            if best is None or a < best[0]:
                best = (a, g)
    return best[1] if best else None


# ── Drawing ──────────────────────────────────────────────────────────────
SIDES = {"E": (1, 0), "W": (-1, 0), "N": (0, -1), "S": (0, 1), "NE": (1, -1), "NW": (-1, -1), "SE": (1, 1), "SW": (-1, 1)}
FS, HS, LH, PADX = 12.5, 11.8, 16.0, 7


def box_for(x, y, w, h, side, gap):
    sx, sy = SIDES[side]
    x0 = x - w / 2 if sx == 0 else x + gap if sx > 0 else x - gap - w
    g = gap * (0.75 if sx else 1)
    y0 = y - h / 2 if sy == 0 else y + g if sy > 0 else y - g - h
    return (x0, y0, x0 + w, y0 + h)


def rotated_box(cx, cy, w, size, ang):
    a = math.radians(ang)
    bw = abs(w * math.cos(a)) + abs(size * math.sin(a))
    bh = abs(w * math.sin(a)) + abs(size * math.cos(a))
    return (cx - bw / 2, cy - bh / 2 - 3, cx + bw / 2, cy + bh / 2)


def draw(listing, data, lat, lon, kind):
    roads, land, pois, stores, trail_ways = data
    if kind == "wide":
        fr = Frame(lat, lon, WIDE_LON, CORE_LAT, round(CORE_W * WIDE_LON / CORE_LON))
        core = Frame(lat, lon, CORE_LON, CORE_LAT, CORE_W)
        cx0 = (fr.cw - core.cw) / 2
        # where labels may go: the core, clear of the strips that fade out
        # into the page above and below the map in the light theme
        zone = (cx0 + 10, 72, cx0 + core.cw - 10, fr.ch - 72)
        caps = CAPS_WIDE
    else:
        fr = Frame(lat, lon, TALL_LON, TALL_LAT, TALL_W)
        core = fr
        zone = (8, 40, fr.cw - 8, fr.ch - 40)
        caps = CAPS_TALL
    in_zone = lambda b: b[0] >= zone[0] and b[1] >= zone[1] and b[2] <= zone[2] and b[3] <= zone[3]
    HX, HY = fr.xy(lat, lon)
    tol = 0.8

    # base layers
    parks, waters, water_lines, rail = [], [], [], []
    for e in land:
        t = e.get("tags", {})
        if t.get("waterway"):
            for run in clip_runs([fr.xy(p["lat"], p["lon"]) for p in e.get("geometry", [])], fr):
                water_lines.append((t["waterway"], t.get("name", ""), rdp(run, tol)))
            continue
        if t.get("leisure") or t.get("natural") == "wood" or t.get("landuse"):
            target = parks
        elif t.get("natural") == "water":
            target = waters
        else:
            continue
        geoms = [e["geometry"]] if e["type"] == "way" else [m["geometry"] for m in e.get("members", []) if m.get("role") != "inner" and "geometry" in m]
        for g in geoms:
            p = [fr.xy(q["lat"], q["lon"]) for q in g]
            if len(p) > 2 and any(fr.inside(x, y, 30) for x, y in p):
                target.append(rdp(p, tol))
    by_class = {k: [] for k in ORDER}
    named, shields = {}, {}
    for e in roads:
        t = e.get("tags", {})
        pts = [fr.xy(q["lat"], q["lon"]) for q in e.get("geometry", [])]
        for run in clip_runs(pts, fr):
            run = rdp(run, tol)
            if t.get("railway"):
                rail.append(run)
                continue
            h, name = t.get("highway", ""), t.get("name", "")
            if h in ("primary_link", "secondary_link", "tertiary_link", "trunk_link"):
                cls = "link"
            elif h in ("unclassified", "living_street"):
                cls = "residential"
            elif h == "trunk":
                cls = "primary"
            elif "Frontage" in name or "Service" in name:
                cls = "frontage"
            else:
                cls = h
            by_class[cls].append(run)
            if name and cls in ("primary", "secondary", "tertiary", "residential"):
                named.setdefault(name, {"cls": cls, "runs": []})["runs"].append(run)
                if ORDER.index(cls) > ORDER.index(named[name]["cls"]):
                    named[name]["cls"] = cls
            if cls == "motorway" and t.get("ref"):
                num = re.sub(r"[^0-9]", "", t["ref"].split(";")[0])
                if num:
                    shields.setdefault(num, []).extend(run)

    # trails: each curated trail's paths joined, both banks merged into one
    # line, stubs dropped; the trail nearest the house first
    px_m = fr.k / 111000
    by_trail = {}
    for e in trail_ways:
        t = trail_of(e.get("tags", {}).get("name", ""))
        for run in clip_runs([fr.xy(q["lat"], q["lon"]) for q in e.get("geometry", [])], fr):
            by_trail.setdefault(t["name"], []).append(run)
    trails = []
    for tname, runs in by_trail.items():
        lines = join_lines(merge_banks(join_lines(runs, 3 * px_m), TRAIL_BANKS_M * px_m), 25 * px_m)
        lines = [rdp(l, tol) for l in lines if line_len(l) >= TRAIL_MIN_M * px_m]
        if lines:
            d = min(math.hypot(x - HX, y - HY) for l in lines for x, y in densify(l, 5)) / px_m
            trails.append(dict(name=tname, lines=lines, d=d))
    trails.sort(key=lambda t: t["d"])

    m = [f'<rect width="{fr.cw}" height="{fr.ch}" fill="#fff"/>']
    if parks:
        m.append(f'<path fill="#E7E7E7" d="{"".join(path_d(p, True) for p in parks)}"/>')
    if waters:
        m.append(f'<path fill="#CFCFCF" d="{"".join(path_d(p, True) for p in waters)}"/>')
    for wk, _, p in water_lines:
        m.append(f'<path d="{path_d(p)}" fill="none" stroke="#CFCFCF" stroke-width="{13 if wk == "river" else 3}" stroke-linecap="round" stroke-linejoin="round"/>')
    layer = FOOTPRINT_LAYER if not (FOOTPRINT_LAYER == "wide" and kind == "tall") else "none"
    if FOOTPRINTS and layer != "none":
        # every building, faint: on the wide map only across the core, where
        # the page is looked at; sheds and garages left out
        x_lo, x_hi = (cx0, cx0 + core.cw) if kind == "wide" else (0, fr.cw)
        bd = {}
        r0, r1 = FOOTPRINT_NEAR_M
        for ring in FOOTPRINTS:
            pts = [fr.xy(a, b) for a, b in ring]
            if not all(x_lo - 20 <= x <= x_hi + 20 and -20 <= y <= fr.ch + 20 for x, y in pts):
                continue
            a = abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))) / 2 / (fr.k / 111000) ** 2
            if a < 50:
                continue
            band = 5
            if layer == "near":
                d = min(math.hypot(x - HX, y - HY) for x, y in pts) / (fr.k / 111000)
                if d >= r1:
                    continue
                band = 5 if d <= r0 else int(5 * (r1 - d) / (r1 - r0))
            bd.setdefault(band, []).append("M" + "L".join(f"{x:.0f},{y:.0f}" for x, y in rdp(pts, 0.6)[:-1]) + "Z")
        for band, ds in sorted(bd.items()):
            op = "" if band == 5 else f' opacity="{(band + 0.5) / 5.5:.2f}"'
            m.append(f'<path fill="#EEEEEE" stroke="#DADADA" stroke-width=".6"{op} d="{"".join(ds)}"/>')
    # landmark outlines, over the park fill and under the roads
    lm_areas = []
    for lmk in LANDMARKS:
        # every landmark is drawn as itself, however small its footprint
        # (Jones Hall, the Alley): a name too big to sit inside its building
        # gets a label beside it instead
        if not lmk.get("shape"):
            continue
        rings = [rdp([fr.xy(a, b) for a, b in ring], tol) for ring in lmk["shape"]]
        if any(max(x for x, _ in r) > 0 and min(x for x, _ in r) < fr.cw and max(y for _, y in r) > 0 and min(y for _, y in r) < fr.ch
               for r in rings):
            lm_areas.append((lmk, rings))
    lm_areas.sort(key=lambda a: -a[0]["area"])  # the zoo drawn over Hermann Park, not under it
    for lmk, rings in lm_areas:
        _, fill, edge = LM_KIND[lmk["kind"]]
        m.append(f'<path fill="{fill}" stroke="{edge}" stroke-width="1" d="{"".join(path_d(r, True) for r in rings)}"/>')
    if rail:
        m.append(f'<path d="{"".join(path_d(p) for p in rail)}" fill="none" stroke="#8E8E8E" stroke-width="1.3" stroke-dasharray="7 4"/>')
    for i in (3, 2):
        for cls in ORDER:
            if by_class[cls]:
                st = LIGHT_ROADS[cls]
                m.append(f'<path d="{"".join(path_d(p) for p in by_class[cls])}" fill="none" stroke="{st[i]}" stroke-width="{st[i - 2]}" stroke-linecap="round" stroke-linejoin="round"/>')
            if i == 2 and cls == "tertiary" and trails:
                # over the neighbourhood streets, under the main roads
                d_ = "".join(path_d(l) for t in trails for l in t["lines"])
                m.append(f'<path d="{d_}" fill="none" stroke="#fff" stroke-width="6" stroke-linecap="round" stroke-linejoin="round"/>')
                m.append(f'<path d="{d_}" fill="none" stroke="{TRAIL_GREEN}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>')

    # fixed things labels must avoid
    address = listing["address"]
    hw = text_w(address, 14.5, "inter600") + 24
    hh = 27
    house_box = (HX - 16, HY - 16, HX + 16, HY + 16)
    house_label = (HX + 14, HY - 23 - hh, HX + 14 + hw, HY - 23)
    if house_label[2] > zone[2]:
        house_label = (HX - 14 - hw, HY - 23 - hh, HX - 14, HY - 23)
    if house_label[0] < zone[0]:
        # too long for either side (a unit number on the phone map): centre
        # it over the house, kept inside the map
        x0 = min(max(HX - hw / 2, zone[0]), zone[2] - hw)
        house_label = (x0, HY - 23 - hh, x0 + hw, HY - 23)
    fixed = [house_box, house_label]
    if kind == "tall":
        fixed += [(14, fr.ch - 56, 14 + 0.5 * fr.k / 69.05 + 60, fr.ch), (fr.cw - 120, fr.ch - 24, fr.cw, fr.ch)]
    text_boxes = []

    def free(b, m_=4):
        return in_zone(b) and not any(overlap(b, f, m_) for f in fixed + text_boxes)

    # freeway shields
    sv = []
    zc = ((zone[0] + zone[2]) / 2, (zone[1] + zone[3]) / 2)
    for num, pts in sorted(shields.items()):
        sw = 14 + 8 * len(num)
        for q in sorted(pts, key=lambda q: math.hypot(q[0] - zc[0], q[1] - zc[1])):
            b = (q[0] - sw / 2, q[1] - 9, q[0] + sw / 2, q[1] + 9)
            if free(b, 2):
                text_boxes.append(b)
                sv.append(f'<g transform="translate({b[0]:.1f},{b[1]:.1f})"><rect width="{sw}" height="18" fill="#000" stroke="#fff" stroke-width="1.5"/><text class="hw" x="{sw / 2}" y="13" text-anchor="middle">{num}</text></g>')
                break

    # trail names, before the street names claim the space: along the
    # straightest stretch, nearest the house first
    for t in trails:
        w = text_w(t["name"], 13, "frauncesItalic600")
        segs = [seg for l in t["lines"] for length, seg in straight_runs(l, 14) if length >= w + 24]
        for seg in sorted(segs, key=lambda sg: math.hypot(point_at(sg)[0] - HX, point_at(sg)[1] - HY)):
            px_, py_ = point_at(seg)
            (x1, y1), (x2, y2) = seg[0], seg[-1]
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
            ang = ang - 180 if ang > 90 else ang + 180 if ang < -90 else ang
            a = math.radians(ang)
            cx, cy = px_ + math.sin(a) * 9, py_ - math.cos(a) * 9
            b = rotated_box(cx, cy, w, 13, ang)
            if free(b, 2):
                text_boxes.append(b)
                sv.append(f'<text class="lmn" font-size="13" fill="{TRAIL_GREEN}" stroke="#fff" text-anchor="middle" '
                          f'transform="translate({cx:.1f},{cy + 4:.1f}) rotate({ang:.1f})">{esc(t["name"])}</text>')
                break

    # street names: along the straightest stretches, nearest the house first
    house_street = nearest_street(roads, lat, lon, 120)
    roads_to_label = []
    for name, r in named.items():
        major = r["cls"] in ("primary", "secondary")
        if r["cls"] == "residential" and name != house_street:
            continue
        dist = min(math.hypot(q[0] - HX, q[1] - HY) for run in r["runs"] for q in run)
        roads_to_label.append((0 if name == house_street else 1 if major else 2, dist, name, major))
    roads_to_label.sort()
    minors = 0
    street_labels = []
    for _, dist, name, major in roads_to_label:
        if not major and name != house_street:
            if minors >= (8 if kind == "wide" else 3):
                continue
        text = abbreviate(name)
        size = 12.5 if major else 10.5
        w = text_w(text, size, "inter500" if major else "inter300")
        runs = []
        for run in named[name]["runs"]:
            for length, seg in straight_runs(run):
                if length >= w + 30:
                    mx, my = point_at(seg)
                    runs.append((math.hypot(mx - HX, my - HY), seg))
        runs.sort(key=lambda r: r[0])
        placed_here = []
        for _, seg in runs:
            if len(placed_here) >= (2 if major and kind == "wide" else 1):
                break
            px, py = point_at(seg)
            if any(math.hypot(px - a, py - b) < 450 for a, b in placed_here):
                continue
            (x1, y1), (x2, y2) = seg[0], seg[-1]
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
            ang = ang - 180 if ang > 90 else ang + 180 if ang < -90 else ang
            off = -(7.5 if major else 5.5)
            a = math.radians(ang)
            cx, cy = px - math.sin(a) * off, py + math.cos(a) * off
            b = rotated_box(cx, cy, w, size, ang)
            if not free(b, 2):
                continue
            text_boxes.append(b)
            placed_here.append((px, py))
            street_labels.append(dict(box=b, i=len(sv), keep=name == house_street))
            sv.append(f'<text class="st{" maj" if major else ""}" font-size="{size}" text-anchor="middle" transform="translate({cx:.1f},{cy:.1f}) rotate({ang:.1f})">{esc(text)}</text>')
        if placed_here and not major and name != house_street:
            minors += 1
    street_boxes = list(text_boxes)

    # water names
    for _, wname, p in sorted(water_lines, key=lambda w: -len(w[2])):
        if not wname or any(wname in s for s in sv):
            continue
        w = text_w(wname, 13, "frauncesItalic600")
        for length, seg in sorted(straight_runs(p, 14), key=lambda r: -r[0]):
            if length < w + 20:
                break
            px, py = point_at(seg)
            (x1, y1), (x2, y2) = seg[0], seg[-1]
            ang = math.degrees(math.atan2(y2 - y1, x2 - x1))
            ang = ang - 180 if ang > 90 else ang + 180 if ang < -90 else ang
            b = rotated_box(px, py - 9, w, 13, ang)
            if free(b, 2):
                text_boxes.append(b)
                sv.append(f'<text class="wt" text-anchor="middle" transform="translate({px:.1f},{py - 9:.1f}) rotate({ang:.1f})">{esc(wname)}</text>')
                break

    # landmark outlines: the name set in the middle of the part of the shape
    # on screen, pulled a little toward the house; where it cannot sit wholly
    # inside (a sliver of the Medical Center at the map's edge) it may lap
    # over the outline, and only failing that does it fall back to a tag
    named_inside, lm_label_boxes = set(), []
    near = lambda lmk: math.hypot(*(a - b for a, b in zip(fr.xy(lmk["lat"], lmk["lon"]), (HX, HY))))
    for lmk, rings in sorted(lm_areas, key=lambda a: (a[0]["tier"], near(a[0]))):
        size = 17 if lmk["area"] > 400000 else 14.5
        lines = split_long(lmk["name"], 20)
        w = max(text_w(l, size, "frauncesItalic600") for l in lines)
        h = size * 1.2 * len(lines)
        # a landmark wholly in the strips beside the core (shown on wide
        # windows only) is named there, beside its own outline
        lz = zone
        if kind == "wide" and (max(x for r in rings for x, _ in r) < zone[0] or min(x for r in rings for x, _ in r) > zone[2]):
            lz = (8, zone[1], fr.cw - 8, zone[3])
        x0 = max(lz[0], min(x for r in rings for x, _ in r))
        x1 = min(lz[2], max(x for r in rings for x, _ in r))
        y0 = max(lz[1], min(y for r in rings for _, y in r))
        y1 = min(lz[3], max(y for r in rings for _, y in r))
        if x1 - x0 < 1 or y1 - y0 < 1:
            continue
        inside = lambda x, y: any(point_in(x, y, r) for r in rings)
        segs = [(a_, b_) for r in rings for a_, b_ in zip(r, r[1:] + r[:1])]

        def edge_d(x, y):
            best_ = 1e9
            for (ax, ay), (bx, by) in segs:
                dx, dy = bx - ax, by - ay
                u = max(0, min(1, ((x - ax) * dx + (y - ay) * dy) / (dx * dx + dy * dy or 1)))
                best_ = min(best_, math.hypot(ax + u * dx - x, ay + u * dy - y))
            return best_
        step = 9 if kind == "tall" else 12
        # the middle of what shows: average of the grid points inside it
        seen = [(gx, gy) for gy in range(int(y0), int(y1) + 1, step) for gx in range(int(x0), int(x1) + 1, step) if inside(gx, gy)]
        if not seen:
            continue
        mx, my = sum(p[0] for p in seen) / len(seen), sum(p[1] for p in seen) / len(seen)
        best = None
        # of the 15 points sampled over the label: all inside the shape, then
        # two thirds, then just touching it (a tip poking in from off the map)
        for need in (15, 10, 1):
            # a label lapping out of the shape may reach past it, within the frame
            lap = 0 if need > 1 else 1
            gys = range(int(max(lz[1] + h / 2, y0 + h / 2 - lap * h)), int(min(lz[3] - h / 2, y1 - h / 2 + lap * h)) + 1, step)
            for gy in gys:
                for gx in range(int(max(lz[0] + w / 2, x0 + w / 2 - lap * w)), int(min(lz[2] - w / 2, x1 - w / 2 + lap * w)) + 1, step):
                    # inside the shape, the middle of it; lapping off it, as
                    # close to its edge as fits, so the name hugs the building
                    pull = math.hypot(gx - mx, gy - my) if need > 1 else 2 * edge_d(gx, gy)
                    score = pull + 0.25 * math.hypot(gx - HX, gy - HY)
                    if best and score >= best[0]:
                        continue
                    b = (gx - w / 2 - 6, gy - h / 2 - 4, gx + w / 2 + 6, gy + h / 2 + 4)
                    if need > 1 and not inside(gx, gy):
                        continue
                    pts = [(px, py) for px in (b[0], (b[0] + gx) / 2, gx, (gx + b[2]) / 2, b[2]) for py in (b[1], gy, b[3])]
                    if sum(inside(px, py) for px, py in pts) < need:
                        continue
                    if not (lz[0] <= b[0] and b[2] <= lz[2] and lz[1] <= b[1] and b[3] <= lz[3]) or any(overlap(b, f, 3) for f in fixed + text_boxes):
                        continue
                    best = (score, b, gx, gy, need)
            if best:
                break
        if not best:
            continue
        _, b, gx, gy, need = best
        col, fill, _ = LM_KIND[lmk["kind"]]
        halo = fill if need > 1 else "#fff"  # mostly off the tint, it sits on white
        text_boxes.append(b)
        lm_label_boxes.append(b)
        named_inside.add(lmk["name"])
        href = maps_link(lmk["name"], {}, lmk["lat"], lmk["lon"], roads)
        ty = gy - h / 2 + size * 0.95
        spans = "".join(f'<tspan x="{gx:.1f}" y="{ty + i * size * 1.2:.1f}">{esc(l)}</tspan>' for i, l in enumerate(lines))
        sv.append(f'<a class="lm-pl" href="{esc(href)}" target="_blank" rel="noopener" style="--c:{col}">'
                  f'<text class="lmn" font-size="{size}" text-anchor="middle" fill="{col}" stroke="{halo}">{spans}</text></a>')

    # places: landmarks first, then the nearest of each everyday kind
    pframe = core if kind == "wide" else fr
    marks, lm_dots = [], []
    for lmk in LANDMARKS:
        x, y = fr.xy(lmk["lat"], lmk["lon"])
        shape = next((r for a, r in lm_areas if a is lmk), None)
        if shape and not (zone[0] < x < zone[2] and zone[1] < y < zone[3]):
            # an outline reaching into the frame from outside it: its dot
            # goes where the outline comes nearest the house
            pts = [q for r in shape for q in r if zone[0] + 20 < q[0] < zone[2] - 20 and zone[1] + 20 < q[1] < zone[3] - 20]
            if not pts:
                continue
            x, y = min(pts, key=lambda q: math.hypot(q[0] - HX, q[1] - HY))
        elif not pframe.inside(*pframe.xy(lmk["lat"], lmk["lon"]), -40):
            continue
        marks.append(lmk)
        d = math.hypot((lmk["lat"] - lat) * 111000, (lmk["lon"] - lon) * 96000)
        if lmk["name"] not in named_inside and d >= 25:
            lm_dots.append((lmk["tier"], d, lmk, x, y))
    lm_dots.sort(key=lambda t: t[:2])
    places = pick_places(pois, roads, lat, lon, pframe, caps, park_areas(land), marks)
    for rank, (_, d, lmk, x, y) in enumerate(lm_dots[:caps["landmark"] + 3]):
        places.append(dict(name=lmk["name"], hint="", cat="landmark", lk=True, col=LM_KIND[lmk["kind"]][0], lat=lmk["lat"],
                           lon=lmk["lon"], d=d, tags={}, rank=rank, at=(x, y),
                           href=maps_link(lmk["name"], {}, lmk["lat"], lmk["lon"], roads)))
    dots = []
    for p in places:
        x, y = p.get("at") or fr.xy(p["lat"], p["lon"])
        lines = split_long(p["name"])
        dots.append(dict(p, x=x, y=y, lines=lines))
    crowd = lambda d: -sum(1 for o in dots if math.hypot(o["x"] - d["x"], o["y"] - d["y"]) < 90)
    placed, leaders, placed_dots = [], [], []
    kept = {cat: 0 for cat in caps}

    def line_w(i, d):
        w = text_w(d["lines"][i], FS, "inter600" if d.get("lk") else "inter500" if d.get("key") else "inter300")
        if i == len(d["lines"]) - 1 and d["hint"]:
            w += 4 + text_w(d["hint"], HS, "frauncesItalic600")
        return w

    def leader(d, b):
        cx, cy = min(max(d["x"], b[0]), b[2]), min(max(d["y"], b[1]), b[3])
        return (d["x"], d["y"], cx, cy)

    def crosses(seg, box):
        # sample the leader; its two ends sit on its own dot and box
        x1, y1, x2, y2 = seg
        return any(box[0] < x1 + (x2 - x1) * t < box[2] and box[1] < y1 + (y2 - y1) * t < box[3] for t in (i / 12 for i in range(1, 12)))

    def seg_cross(a, b):
        (x1, y1, x2, y2), (x3, y3, x4, y4) = a, b
        den = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
        if abs(den) < 1e-9:
            return False
        t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / den
        u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / den
        return 0 < t < 1 and 0 < u < 1

    house_street_boxes = [l["box"] for l in street_labels if l["keep"]]
    # nearest of each kind first, then the spares in the order they rank
    for d in sorted(dots, key=lambda d: (not d.get("lk"), not d.get("key"), d["rank"], d["d"] > 1500, crowd(d))):
        if kept[d["cat"]] >= caps[d["cat"]]:
            continue
        if any(overlap((d["x"] - 6, d["y"] - 6, d["x"] + 6, d["y"] + 6), q) for q in [q["box"] for q in placed] + lm_label_boxes):
            continue  # its dot would sit under a label already drawn
        w = max(line_w(i, d) for i in range(len(d["lines"]))) + PADX * 2
        h = LH * len(d["lines"]) + 5
        best = None
        for gap in (8, 15, 25, 38, 56):
            for sd in ("E", "W", "NE", "SE", "NW", "SW", "N", "S"):
                b = box_for(d["x"], d["y"], w, h, sd, gap)
                if not in_zone(b):
                    continue
                if any(overlap(b, f, 4) for f in fixed + [q["box"] for q in placed]):
                    continue
                if any(overlap(b, f, 2) for f in text_boxes if f not in street_boxes):
                    continue
                if any(overlap(b, f, 1) for f in house_street_boxes):
                    continue
                if any(overlap(b, (o["x"] - 6, o["y"] - 6, o["x"] + 6, o["y"] + 6)) for o in dots
                       if o is not d and (o["rank"] < caps[o["cat"]] or any(o is q_ for q_ in placed_dots))):
                    continue
                ln = leader(d, b)
                if any(crosses(ln, f) for f in fixed + [q["box"] for q in placed]):
                    continue
                if any(crosses(l, b) or seg_cross(ln, l) for l in leaders):
                    continue
                cost = gap + 60 * sum(overlap(b, s_) for s_ in street_boxes)
                if best is None or cost < best[0]:
                    best = (cost, b)
        if best is None:
            continue  # no room: the next nearest of its kind gets a turn
        placed.append(dict(d, box=best[1]))
        placed_dots.append(d)
        leaders.append(leader(d, best[1]))
        kept[d["cat"]] += 1
    # a place label wins over the street name it lands on; that street's
    # other label (or its neighbours) still name it
    for l in street_labels:
        if not l["keep"] and any(overlap(l["box"], q["box"]) for q in placed):
            sv[l["i"]] = ""
            text_boxes.remove(l["box"])
    for q in placed:
        if q["cat"] == "grocery":
            g = store_outline(q, stores) or (FOOTPRINTS and footprint_at(q["lat"], q["lon"]))
            if g:
                m.append(f'<path d="{path_d([fr.xy(p["lat"], p["lon"]) for p in g], True)}" fill="#CFE5E1" stroke="#7FB3AB" stroke-width="1.2" stroke-linejoin="round"/>')
        elif q["cat"] == "school" and FOOTPRINTS:
            g = footprint_at(q["lat"], q["lon"])
            if g:
                m.append(f'<path d="{path_d([fr.xy(p["lat"], p["lon"]) for p in g], True)}" fill="#DFE6F1" stroke="#AEBFD9" stroke-width="1.2" stroke-linejoin="round"/>')
    for q in placed:
        b, x, y, col = q["box"], q["x"], q["y"], q.get("col") or CATS[q["cat"]]
        cx, cy = min(max(x, b[0]), b[2]), min(max(y, b[1]), b[3])
        key = q.get("key")
        g = [f'<a class="lm-pl{" key" if key else ""}" href="{esc(q["href"])}" target="_blank" rel="noopener" style="--c:{col}">']
        if math.hypot(cx - x, cy - y) > 4:
            g.append(f'<line x1="{x:.1f}" y1="{y:.1f}" x2="{cx:.1f}" y2="{cy:.1f}" stroke="{col}" stroke-width="{1.6 if key else 1.3}"/>')
        g.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{5.8 if key else 4.8}" fill="{col}" stroke="#fff" stroke-width="1.5"/>')
        edge = f'stroke="{col}" stroke-width="1.6"' if key else 'stroke="#6a6a6a" stroke-width="1"'
        g.append(f'<g transform="translate({b[0]:.1f},{b[1]:.1f})"><rect x=".8" y=".8" width="{b[2] - b[0] - 1.6:.1f}" height="{b[3] - b[1] - 1.6:.1f}" fill="#fff" {edge}/>')
        for i, line in enumerate(q["lines"]):
            hint = f'<tspan class="hint" dx="4">{esc(q["hint"])}</tspan>' if q["hint"] and i == len(q["lines"]) - 1 else ""
            g.append(f'<text class="pl{" lk" if q.get("lk") else " key" if key else ""}" x="{PADX}" y="{2.5 + LH * i + LH * 0.74:.1f}">{esc(line)}{hint}</text>')
        g.append("</g></a>")
        text_boxes.append(b)
        sv.append("".join(g))

    # neighbourhood names, wherever they fit
    if kind == "wide":
        for e in pois:
            t = e.get("tags", {})
            if not t.get("place"):
                continue
            x, y = fr.xy(e["lat"], e["lon"])
            w = text_w(t["name"], 25, "frauncesItalic600")
            b = (x - w / 2, y - 22, x + w / 2, y + 6)
            if math.hypot(x - HX, y - HY) > 120 and free(b, 6) and not any(overlap(b, (o["x"] - 8, o["y"] - 8, o["x"] + 8, o["y"] + 8)) for o in placed_dots):
                text_boxes.append(b)
                sv.insert(0, f'<text class="nb" x="{x:.1f}" y="{y:.1f}" text-anchor="middle">{esc(t["name"])}</text>')

    m.extend(sv)
    # the house, linked to its listing
    m.append(f'<a class="lm-home" href="{esc(listing["url"])}" target="_blank" rel="noopener" aria-label="{esc(address)} on Compass">')
    lx = house_label[0] + 10 if house_label[0] > HX else house_label[2] - 10 if house_label[2] < HX else HX
    m.append(f'<line x1="{HX:.1f}" y1="{HY:.1f}" x2="{lx:.1f}" y2="{house_label[3] - 3:.1f}" stroke="#000" stroke-width="2"/>')
    m.append(f'<circle cx="{HX:.1f}" cy="{HY:.1f}" r="14.5" fill="#fff" stroke="#000" stroke-width="1.5"/><circle cx="{HX:.1f}" cy="{HY:.1f}" r="9" fill="#000"/>')
    m.append(f'<g transform="translate({house_label[0]:.1f},{house_label[1]:.1f})"><rect width="{hw:.1f}" height="{hh}" fill="#000"/><text class="hl" x="12" y="18.8">{esc(address)}</text></g></a>')
    if kind == "tall":
        mile = fr.k / 69.05
        half = 0.5 * mile
        bar = f'M0 0v7h{half:.1f}v-7M{half / 2:.1f} 3v4'
        m.append(f'<g transform="translate(22,{fr.ch - 38})"><path d="{bar}" fill="none" stroke="#fff" stroke-width="5" stroke-linecap="square"/><path d="{bar}" fill="none" stroke="#1d1d1d" stroke-width="1.5"/>'
                 f'<text class="sc" x="0" y="23">0</text><text class="sc" x="{half / 2:.1f}" y="23" text-anchor="middle">¼ mile</text><text class="sc" x="{half:.1f}" y="23" text-anchor="middle">½ mile</text></g>')
        m.append(f'<text class="sc" x="{fr.cw - 12}" y="{fr.ch - 10}" text-anchor="end">© OpenStreetMap</text>')
    attrs = ' preserveAspectRatio="xMidYMid slice"' if kind == "wide" else ""
    svg = (f'<svg class="lm-{kind}"{attrs} xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {fr.cw} {fr.ch}" role="img" '
           f'aria-label="Map of the streets around {esc(address)} with nearby parks, schools, restaurants, coffee and grocery">{"".join(m)}</svg>')
    return svg, fr, len(placed)


def build(listing, lat, lon, cache=None):
    hit = cache / f"{lat:.5f},{lon:.5f}.json" if cache else None
    if hit and hit.exists():
        data = tuple(json.loads(hit.read_text(encoding="utf-8")))
        if len(data) == 3:  # cached before store buildings were drawn
            data = (*data, fetch_stores(lat, lon))
            hit.write_text(json.dumps(data), encoding="utf-8")
        if len(data) == 4:  # cached before trails were drawn
            data = (*data, fetch_trails(lat, lon))
            hit.write_text(json.dumps(data), encoding="utf-8")
    else:
        data = fetch(lat, lon)
        if hit:
            hit.write_text(json.dumps(data), encoding="utf-8")
    global FOOTPRINTS
    FOOTPRINTS = load_footprints(lat, lon, cache)
    wide, fr, n_wide = draw(listing, data, lat, lon, "wide")
    tall, _, n_tall = draw(listing, data, lat, lon, "tall")
    # Tall enough that the core fits across the window, never more than 0.8x
    # its drawn size; the sides crop away on narrower windows.
    size = f".lm .lm-wide{{height:min({fr.ch * 0.8:.0f}px,{fr.ch / (CORE_W * 1.04) * 100:.2f}vw)}}"
    html = (f"<style>{CSS}{size}</style>\n"
            f'<div class="lm">{wide}{tall}<span class="lm-credit">© OpenStreetMap</span></div>\n')
    return html, n_wide, n_tall


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--rebuild", action="store_true", help="redraw maps that already exist")
    ap.add_argument("--only", help="just this listing (page slug)")
    ap.add_argument("--cache", type=pathlib.Path, help="keep the raw OpenStreetMap data here and reuse it, for redrawing while tuning")
    args = ap.parse_args()
    listings = json.loads((ROOT / "listings.json").read_text(encoding="utf-8"))
    geo = json.loads((ROOT / "geo.json").read_text(encoding="utf-8"))
    # Hand fixes for Compass's mistakes (see render-listings.py): a corrected
    # address renames the page, a corrected point moves the map.
    cpath = ROOT / "corrections.json"
    corrections = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)
    if args.cache:
        args.cache.mkdir(parents=True, exist_ok=True)
    keep = set()
    failed = 0
    for l in listings:
        fix = corrections.get(geo_key(l.get("url", "")) or "")
        if isinstance(fix, dict):
            for field in ("address", "area"):
                if isinstance(fix.get(field), str):
                    l[field] = fix[field]
            if isinstance(fix.get("geo"), list) and len(fix["geo"]) == 2:
                geo[geo_key(l["url"])] = fix["geo"]
        if not l.get("address"):
            continue
        slug = slug_of(l["address"])
        keep.add(f"{slug}.html")
        if args.only and slug != args.only:
            continue
        out = OUT_DIR / f"{slug}.html"
        if out.exists() and not args.rebuild:
            continue
        ll = geo.get(geo_key(l.get("url", "")) or "")
        if not ll:
            log(f"{slug}: no coordinates in geo.json yet; page goes without a map")
            continue
        lon, lat = ll
        try:
            html, nw, nt = build(l, lat, lon, args.cache)
        except Exception as e:  # one listing's map must not cost the others theirs
            failed += 1
            log(f"{slug}: map failed ({e}); page goes without a map until the next run")
            continue
        out.write_text(html, encoding="utf-8", newline="\n")
        log(f"{slug}: map drawn, {nw} places on the computer map, {nt} on the phone map, {len(html) // 1024} KB")
    if not args.only:
        for f in OUT_DIR.glob("*.html"):
            if f.name not in keep:
                f.unlink()
                log(f"{f.stem}: listing gone, map removed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
