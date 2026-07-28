"""Build basemap.json: the geometry the homepage map is drawn on.

A rounded rectangle labelled "610" is not a map of Houston. What makes the
city legible at a glance is the real shape of the Loop, the spokes coming off
it - I-10, 45, 59/69, 288, 290 and the tollways - and the water: Buffalo Bayou
running through downtown and the Ship Channel widening out to the east. This
fetches all of that from the Census Bureau's TIGERweb service, projects it
into the map's frame, clips it, simplifies it, and writes the result as
ready-made SVG path strings.

Roads come out in four tiers rather than one, because a single grey weight for
every road is what makes a drawing look like a diagram. 610 is heaviest, the
interstates and US highways next, then the tollways, then the state highways
that break up the ground in between.

The Loop is also emitted as a closed ring, not just a line. That is what lets
the page wash the inside of it with a tint - the map's whole point is that
the work is concentrated in there.

Run by hand, not in CI. Freeways do not move, so the output is committed and
the daily workflow just reads it.

basemap.json also carries the frame bounds and viewBox, and is the single
source of truth for both: tools/render-listings.py projects its dots using
the frame recorded here, so the geometry and the dots cannot drift out of
register. Change a bound below and re-run this, or the two disagree.

TIGER/Line is US Census work product and therefore public domain - no API
key, no attribution requirement, no terms that a commercial tile provider
would attach.

Stdlib only.

Usage:
    python tools/basemap.py
    python tools/basemap.py --dry-run
"""

import argparse
import json
import math
import pathlib
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# ── The frame ──────────────────────────────────────────────────────────────
# Cropped tight around 610, which fills about 60% of the width here. A wider
# frame leaves the Loop as a small shape in a lot of empty land and the map
# stops being about the thing it is meant to be about; much tighter and the
# spokes have no room to read as spokes. The far suburbs fall out either way -
# Katy, Manvel and Clear Lake are each a good half-hour past the edge, and
# widening the box to hold them would squeeze the inner loop into a smudge.
# render-listings.py reads these back out of basemap.json.
# The north/south bounds are cropped tighter than the east/west ones so the
# frame comes out about 1.4:1. Two of these now sit on the page where one
# photo used to, and at the natural 1.25:1 they ate more of the scroll than
# the rows they belong to. This costs no listings at all - the nearest one to
# an edge is still comfortably inside - and 610 keeps a couple of miles of
# margin top and bottom.
MAP_WEST, MAP_EAST = -95.545, -95.215
MAP_SOUTH, MAP_NORTH = 29.652, 29.856
MAP_W = 340

TIGERWEB = "https://tigerweb.geo.census.gov/arcgis/rest/services/TIGERweb"
ROADS = (f"{TIGERWEB}/Transportation/MapServer", 2)      # Primary Roads
ARTERIALS = (f"{TIGERWEB}/Transportation/MapServer", 4)  # Secondary Roads
WATER_LINES = (f"{TIGERWEB}/Hydro/MapServer", 0)         # Linear Hydrography
WATER_AREAS = (f"{TIGERWEB}/Hydro/MapServer", 1)         # Areal Hydrography

USER_AGENT = "Mozilla/5.0 (compatible; mikespear.com basemap build)"
TIMEOUT = 120

# TIGER splits one physical freeway across many names, so 610 arrives as
# "I- 610" plus each of its four named sides.
LOOP_NAMES = {"i- 610", "w loop fwy", "n loop fwy", "s loop fwy", "e loop fwy"}

# Anything whose name carries one of these is a tollway or a state spur, drawn
# lighter than the free freeways. Matching on tokens rather than an exhaustive
# list of names means a road TIGER renames next year still lands somewhere
# sensible instead of vanishing.
TOLL_TOKENS = ("toll", "pkwy", "beltway", "loop 8", "spur",
               "hwy 99", "hwy 249", "hwy 6")

# HOV lanes, access roads and bridge decks are separate features that trace
# the freeway they belong to. Drawn, they just thicken lines by a random
# amount depending on how TIGER happened to split that stretch.
SKIP_TOKENS = ("hov", "acc", "brg", "ramp")

# The bayous a Houstonian would recognise. TIGER's linear hydrography in this
# frame is 1,197 features, almost all of them unnamed drainage ditches; drawing
# them all would bury the roads under blue lint.
BAYOUS = ("buffalo byu", "white oak byu", "little white oak byu",
          "little whiteoak byu", "brays byu", "sims byu", "greens byu",
          "hunting byu", "halls byu", "carpenters byu")

# Areal water worth drawing is the wide stuff - the Ship Channel and the lower
# bayous, which TIGER files as river/stream polygons.
WATER_AREA_CLASSES = {"H3010", "H3020"}

# H2030 is "lake or pond", which in Houston means 1,700 subdivision retention
# ponds - but it is also, unnamed and unremarked, how TIGER stores Buffalo
# Bayou for the eleven miles from Memorial Park through River Oaks and Allen
# Parkway to downtown, picking up again as a named river polygon exactly where
# this one ends. Excluding the class outright put an eight-mile hole in the
# most recognisable water in the city.
#
# Size tells them apart with room to spare: that polygon is 11.4 miles across
# and the next largest pond anywhere near downtown is 0.09 miles, so anything
# reaching more than about a mile and a half is a waterway, not a pond.
WATER_AREA_MIN_SPAN = 25.0

# Douglas-Peucker tolerance in SVG units. At 0.7 the simplification is
# invisible at the size this renders and roughly halves the byte count.
SIMPLIFY = 0.7
WATER_SIMPLIFY = 0.9
# How far outside the frame a vertex may sit before the line is cut. A little
# slack keeps roads bleeding past the edge instead of stopping short of it.
BLEED = 6.0

# The Loop ring is rebuilt as one radius per angular step around its middle.
# 610 is convex enough for that to trace it faithfully, and it turns 17
# overlapping TIGER fragments into a single closed shape that can be filled.
RING_STEPS = 240

MID_LAT_COS = math.cos(math.radians((MAP_SOUTH + MAP_NORTH) / 2))
MAP_H = round(MAP_W * (MAP_NORTH - MAP_SOUTH)
              / ((MAP_EAST - MAP_WEST) * MID_LAT_COS))


def project(lon: float, lat: float) -> tuple[float, float]:
    x = (lon - MAP_WEST) / (MAP_EAST - MAP_WEST) * MAP_W
    y = (MAP_NORTH - lat) / (MAP_NORTH - MAP_SOUTH) * MAP_H
    return x, y


def runs_inside(points: list) -> list[list]:
    """Split a projected line into the stretches that fall within the frame.

    Dropping whole features that poke outside would lose I-10 entirely; this
    keeps the part that shows and cuts the rest.
    """
    runs, current = [], []
    for x, y in points:
        if -BLEED <= x <= MAP_W + BLEED and -BLEED <= y <= MAP_H + BLEED:
            current.append((x, y))
        else:
            if len(current) > 1:
                runs.append(current)
            current = []
    if len(current) > 1:
        runs.append(current)
    return runs


def simplify(points: list, tolerance: float) -> list:
    """Douglas-Peucker, iteratively - TIGER lines are long enough to blow the
    recursion limit on a bad day."""
    if len(points) < 3:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        start, end = stack.pop()
        x1, y1 = points[start]
        x2, y2 = points[end]
        dx, dy = x2 - x1, y2 - y1
        span = math.hypot(dx, dy)
        worst, index = 0.0, start
        for i in range(start + 1, end):
            x, y = points[i]
            if span:
                gap = abs(dy * x - dx * y + x2 * y1 - y2 * x1) / span
            else:
                gap = math.hypot(x - x1, y - y1)
            if gap > worst:
                worst, index = gap, i
        if worst > tolerance:
            keep[index] = True
            stack.append((start, index))
            stack.append((index, end))
    return [p for p, k in zip(points, keep) if k]


def fetch(service: tuple[str, int], fields: str) -> list[dict]:
    """Every feature of one layer intersecting the frame, as GeoJSON."""
    base, layer = service
    envelope = {
        "xmin": MAP_WEST - 0.05, "ymin": MAP_SOUTH - 0.05,
        "xmax": MAP_EAST + 0.05, "ymax": MAP_NORTH + 0.05,
        "spatialReference": {"wkid": 4326},
    }
    query = urllib.parse.urlencode({
        "geometry": json.dumps(envelope),
        "geometryType": "esriGeometryEnvelope",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "where": "1=1",
        "outFields": fields,
        "returnGeometry": "true",
        "outSR": "4326",
        "resultRecordCount": "6000",
        "f": "geojson",
    })
    url = f"{base}/{layer}/query?{query}"
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(payload["error"].get("message", "TIGERweb error"))
    return payload.get("features", [])


def lines_of(feature: dict) -> list[list]:
    """Every ring or line in one GeoJSON feature, flattened."""
    geometry = feature.get("geometry") or {}
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates") or []
    if kind == "LineString":
        return [coordinates]
    if kind == "MultiLineString":
        return coordinates
    if kind == "Polygon":
        return coordinates
    if kind == "MultiPolygon":
        return [ring for polygon in coordinates for ring in polygon]
    return []


def collect(features: list[dict], keep, tolerance: float) -> list[list]:
    """Project, clip and simplify every line of every feature `keep` accepts.

    `keep` is handed the whole feature, not just its properties, because the
    areal water filter has to ask how big something is and TIGER will not say.
    """
    runs = []
    for feature in features:
        if not keep(feature):
            continue
        for line in lines_of(feature):
            projected = [project(float(c[0]), float(c[1])) for c in line]
            for run in runs_inside(projected):
                runs.append(simplify(run, tolerance))
    return runs


def span_of(feature: dict) -> float:
    """The diagonal of a feature's projected bounding box, in SVG units."""
    points = [project(float(c[0]), float(c[1]))
              for line in lines_of(feature) for c in line]
    if not points:
        return 0.0
    xs = [x for x, _ in points]
    ys = [y for _, y in points]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def loop_ring(vertices: list) -> list:
    """The 610 fragments rebuilt as one closed ring around their middle.

    TIGER hands back the Loop as seventeen overlapping pieces under five
    different names, which cannot be filled and which stack up into a lumpy
    line where the pieces double back over each other. Every point on 610 is
    visible from the middle of it, so sweeping around the centre and taking
    the outermost vertex in each angular step traces the real thing and closes
    it. `--dry-run` reports how far the result strays from the raw geometry.
    """
    centre_x = sum(x for x, _ in vertices) / len(vertices)
    centre_y = sum(y for _, y in vertices) / len(vertices)

    reach: list[float] = [0.0] * RING_STEPS
    for x, y in vertices:
        dx, dy = x - centre_x, y - centre_y
        step = int((math.atan2(dy, dx) + math.pi) / (2 * math.pi) * RING_STEPS)
        step = min(step, RING_STEPS - 1)
        reach[step] = max(reach[step], math.hypot(dx, dy))

    # A step with nothing in it would collapse the ring to its centre. Carry
    # the last real reading across the gap instead.
    if not any(reach):
        return []
    for _ in range(2):
        for i in range(RING_STEPS):
            if not reach[i]:
                reach[i] = reach[i - 1] or reach[(i + 1) % RING_STEPS]

    # One pass of neighbour averaging, to keep a single outlying vertex from
    # putting a spike in an otherwise smooth stretch of freeway.
    smoothed = [(reach[i - 1] + 2 * reach[i] + reach[(i + 1) % RING_STEPS]) / 4
                for i in range(RING_STEPS)]

    ring = []
    for i, radius in enumerate(smoothed):
        angle = (i + 0.5) / RING_STEPS * 2 * math.pi - math.pi
        ring.append((centre_x + radius * math.cos(angle),
                     centre_y + radius * math.sin(angle)))
    return ring


def ring_error(ring: list, vertices: list) -> float:
    """Worst distance from a real 610 vertex to the rebuilt ring, in units.

    Measured against the ring's segments, not its corner points: the ring is
    sampled every few units, so comparing to the nearest corner would report
    that spacing as error even for a perfect trace.
    """
    segments = list(zip(ring, ring[1:] + ring[:1]))
    worst = 0.0
    for x, y in vertices:
        near = min(point_to_segment(x, y, *a, *b) for a, b in segments)
        worst = max(worst, near)
    return worst


def point_to_segment(x: float, y: float, x1: float, y1: float,
                     x2: float, y2: float) -> float:
    dx, dy = x2 - x1, y2 - y1
    span = dx * dx + dy * dy
    if not span:
        return math.hypot(x - x1, y - y1)
    along = max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / span))
    return math.hypot(x - (x1 + along * dx), y - (y1 + along * dy))


def to_path(runs: list[list], close: bool = False) -> str:
    """SVG path data. One decimal is a third of a pixel at display size."""
    suffix = "Z" if close else ""
    return "".join(
        "M" + " ".join(f"{x:.1f},{y:.1f}" for x, y in run) + suffix
        for run in runs if len(run) > 1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(REPO_ROOT / "basemap.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="report sizes without writing the file")
    args = parser.parse_args()

    roads = fetch(ROADS, "NAME")
    print(f"TIGERweb returned {len(roads)} primary road feature(s).")
    if not roads:
        raise SystemExit("No road geometry came back; refusing to write an "
                         "empty basemap.")

    def named(feature: dict) -> str:
        return ((feature.get("properties") or {}).get("NAME") or "").lower()

    def drivable(feature: dict) -> bool:
        return not any(token in named(feature) for token in SKIP_TOKENS)

    def is_loop(feature: dict) -> bool:
        return drivable(feature) and named(feature) in LOOP_NAMES

    def is_toll(feature: dict) -> bool:
        name = named(feature)
        return (drivable(feature) and name not in LOOP_NAMES
                and any(token in name for token in TOLL_TOKENS))

    def is_freeway(feature: dict) -> bool:
        return (drivable(feature) and named(feature) not in LOOP_NAMES
                and not is_toll(feature))

    # The ring is built from unclipped, unsimplified vertices: clipping first
    # would open a gap in the ring wherever the frame cut it, and the ring has
    # to close to be fillable.
    loop_vertices = [project(float(c[0]), float(c[1]))
                     for feature in roads if is_loop(feature)
                     for line in lines_of(feature) for c in line]
    if not loop_vertices:
        raise SystemExit("No 610 geometry found; TIGER's naming has changed "
                         "and LOOP_NAMES needs updating.")
    ring = loop_ring(loop_vertices)
    if len(ring) < 3:
        raise SystemExit("The 610 ring came out degenerate.")
    print(f"  610 ring: {len(ring)} points from {len(loop_vertices)} vertices, "
          f"worst stray {ring_error(ring, loop_vertices):.2f} units")

    loops = collect(roads, is_loop, SIMPLIFY)
    freeways = collect(roads, is_freeway, SIMPLIFY)
    tollways = collect(roads, is_toll, SIMPLIFY)

    # State highways and spurs - Old Spanish Trail, Wayside, Hwy 3. Not many,
    # but they break up the empty ground between the freeways, which is most
    # of what separates a map from a diagram of one.
    try:
        arterials = collect(fetch(ARTERIALS, "NAME"), drivable, SIMPLIFY)
    except (urllib.error.URLError, TimeoutError, RuntimeError,
            json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: no secondary roads ({exc}).")
        arterials = []

    # Water is a nicety, not a requirement: if the Hydro service is down the
    # map is still a map, so this does not take the build with it.
    water_lines, water_areas = [], []
    try:
        lines = fetch(WATER_LINES, "NAME,MTFCC")
        areas = fetch(WATER_AREAS, "NAME,MTFCC")
        print(f"TIGERweb returned {len(lines)} water line(s) and "
              f"{len(areas)} water area(s).")
        def is_bayou(feature: dict) -> bool:
            return named(feature) in BAYOUS

        def is_open_water(feature: dict) -> bool:
            mtfcc = (feature.get("properties") or {}).get("MTFCC")
            return (mtfcc in WATER_AREA_CLASSES
                    or span_of(feature) >= WATER_AREA_MIN_SPAN)

        water_lines = collect(lines, is_bayou, WATER_SIMPLIFY)
        water_areas = collect(areas, is_open_water, WATER_SIMPLIFY)

        # Worth reporting: these are the polygons kept purely because of their
        # size, so if the threshold ever starts letting ponds through, this is
        # the line that says so.
        big = sorted(
            ((span_of(f), (f.get("properties") or {}).get("NAME") or "unnamed")
             for f in areas
             if (f.get("properties") or {}).get("MTFCC") not in WATER_AREA_CLASSES
             and span_of(f) >= WATER_AREA_MIN_SPAN), reverse=True)
        listed = ", ".join(f"{name} ({span:.0f}u)" for span, name in big)
        print(f"  {len(big)} unclassed water polygon(s) kept on size"
              + (f": {listed}" if big else ""))
    except (urllib.error.URLError, TimeoutError, RuntimeError,
            json.JSONDecodeError, OSError) as exc:
        print(f"WARNING: no water geometry ({exc}); drawing roads only.")

    basemap = {
        "frame": {"west": MAP_WEST, "east": MAP_EAST,
                  "south": MAP_SOUTH, "north": MAP_NORTH},
        "viewBox": [MAP_W, MAP_H],
        "source": "US Census Bureau TIGER/Line (public domain)",
        # The Loop twice over. The line drawn on the map is TIGER's own
        # geometry, kinks and all, because the rebuilt ring is smooth enough
        # to look sketched when you stroke it. The ring is only ever the fill
        # underneath, where a unit or two of give never shows.
        "loop": to_path(loops),
        "loopFill": to_path([ring], close=True),
        "freeways": to_path(freeways),
        "tollways": to_path(tollways),
        "arterials": to_path(arterials),
        "water": to_path(water_lines),
        "waterArea": to_path(water_areas, close=True),
    }
    body = json.dumps(basemap, indent=2) + "\n"

    for key in ("loop", "loopFill", "freeways", "tollways", "arterials",
                "water", "waterArea"):
        print(f"  {key:>10}: {len(basemap[key]):6,} bytes")
    print(f"  viewBox 0 0 {MAP_W} {MAP_H}")

    if args.dry_run:
        print("(dry run - nothing written)")
        return

    out = pathlib.Path(args.out)
    out.write_text(body, encoding="utf-8", newline="\n")
    print(f"Wrote {out.name} ({len(body):,} bytes)")


if __name__ == "__main__":
    main()
