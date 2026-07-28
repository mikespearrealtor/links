"""Resolve listing addresses to coordinates and cache them in geo.json.

The map on the homepage needs a latitude and longitude per address, and
neither listings.json nor sold.json carries one. This fills that gap using
the Census Bureau geocoder - free, no API key, no rate-limit paperwork, and
its data is public domain, which the commercial geocoders' terms are not.

The cache is the point. Addresses are geocoded once and the answer is
committed to the repo, so the daily workflow makes zero network calls on a
day when nothing new listed, and a Census outage can never blank the map.

Keys are the address slug out of the Compass URL - "1204-Willard-St-Houston-
TX-77006" - because that is the only field carrying city, state and ZIP.
The bare "address" in listings.json is just a street line and geocodes to
whichever city guesses first.

An address the geocoder cannot place is cached as null and retried on later
runs, since Census does add addresses over time. Nulls are not an error: the
map simply omits that pin and says how many it omitted.

Stdlib only. Runs before tools/render-listings.py in the daily workflow.

Usage:
    python tools/geocode.py
    python tools/geocode.py --dry-run
    python tools/geocode.py --recheck        # re-resolve everything
"""

import argparse
import json
import pathlib
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

ENDPOINT = "https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
BENCHMARK = "Public_AR_Current"
USER_AGENT = "Mozilla/5.0 (compatible; mikespear.com listings sync)"
TIMEOUT = 30

# A ceiling on new lookups per run. The cache means a normal day needs one or
# two; anything near this many says the key format changed and every address
# now looks new, which should not turn into a few hundred requests at Census.
MAX_LOOKUPS = 40

SLUG_RE = re.compile(r"/homedetails/([^/]+)/")

# Texas and its neighbours, loosely. A "match" outside this box is the
# geocoder having found a same-named street in another state, which would
# drop a pin in Ohio rather than admit it did not know.
LON_RANGE = (-107.0, -93.0)
LAT_RANGE = (25.5, 37.0)


def slug(url: str) -> str | None:
    """The address portion of a Compass listing URL, or None."""
    match = SLUG_RE.search(url or "")
    return match.group(1) if match else None


def one_line(address_slug: str) -> str:
    """"1204-Willard-St-Houston-TX-77006" -> "1204 Willard St Houston TX 77006"."""
    return address_slug.replace("-", " ")


def lookup(address_slug: str) -> list[float] | None:
    """[lon, lat] for one address, or None if it cannot be placed."""
    query = urllib.parse.urlencode({
        "address": one_line(address_slug),
        "benchmark": BENCHMARK,
        "format": "json",
    })
    request = urllib.request.Request(f"{ENDPOINT}?{query}",
                                     headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
        matches = payload["result"]["addressMatches"]
    except (urllib.error.URLError, TimeoutError, OSError, ValueError,
            KeyError, TypeError) as exc:
        print(f"WARNING: geocoder failed for {address_slug!r} ({exc})")
        return None

    if not matches:
        return None
    try:
        point = matches[0]["coordinates"]
        lon, lat = float(point["x"]), float(point["y"])
    except (KeyError, TypeError, ValueError):
        print(f"WARNING: unreadable coordinates for {address_slug!r}")
        return None

    if not (LON_RANGE[0] <= lon <= LON_RANGE[1]
            and LAT_RANGE[0] <= lat <= LAT_RANGE[1]):
        print(f"WARNING: {address_slug!r} geocoded to ({lat:.4f}, {lon:.4f}), "
              "outside Texas; discarding.")
        return None
    # Six decimals is a tenth of a metre - far past what a dot on a 340px map
    # can express, and it keeps geo.json from churning on rounding noise.
    return [round(lon, 6), round(lat, 6)]


def load_json(path: pathlib.Path, expect: type, label: str):
    """Read JSON of an expected shape, treating anything else as empty."""
    empty = expect()
    if not path.exists():
        print(f"WARNING: {path.name} is missing.")
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        print(f"WARNING: {path.name} is unreadable ({exc}).")
        return empty
    if not isinstance(data, expect):
        print(f"WARNING: {path.name} is {type(data).__name__}, "
              f"expected {expect.__name__}.")
        return empty
    return data


def wanted(*record_lists) -> list[str]:
    """Every address slug referenced by the listing files, in page order."""
    seen = []
    for records in record_lists:
        for record in records:
            if not isinstance(record, dict):
                continue
            key = slug(record.get("url", ""))
            if key and key not in seen:
                seen.append(key)
    return seen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--listings", default=str(REPO_ROOT / "listings.json"))
    parser.add_argument("--sold", default=str(REPO_ROOT / "sold.json"))
    parser.add_argument("--cache", default=str(REPO_ROOT / "geo.json"))
    parser.add_argument("--dry-run", action="store_true",
                        help="report what would be looked up, write nothing")
    parser.add_argument("--recheck", action="store_true",
                        help="ignore the cache and re-resolve every address")
    args = parser.parse_args()

    cache_path = pathlib.Path(args.cache)
    cache = load_json(cache_path, dict, "geo.json")
    listings = load_json(pathlib.Path(args.listings), list, "listings.json")
    sales = load_json(pathlib.Path(args.sold), list, "sold.json")

    keys = wanted(listings, sales)
    if not keys:
        print("No listing URLs to geocode; leaving geo.json alone.")
        return

    # A cached null is a previous miss. Retry those - Census fills addresses
    # in over time - but a cached hit is never looked up again.
    todo = [k for k in keys if args.recheck or cache.get(k) is None]
    if len(todo) > MAX_LOOKUPS:
        print(f"WARNING: {len(todo)} addresses need geocoding, over the "
              f"per-run limit of {MAX_LOOKUPS}. Doing the first {MAX_LOOKUPS}; "
              "the rest resolve on the next run.")
        todo = todo[:MAX_LOOKUPS]

    if args.dry_run:
        for key in todo:
            print(f"would look up {one_line(key)}")
        print(f"{len(todo)} lookup(s), {len(keys) - len(todo)} already cached.")
        return

    found = 0
    for key in todo:
        point = lookup(key)
        cache[key] = point
        if point:
            found += 1
            print(f"{one_line(key)} -> {point[1]:.5f}, {point[0]:.5f}")
        else:
            print(f"{one_line(key)} -> not found")

    # Addresses that have dropped off the Compass profile keep no entry: the
    # cache tracks the current lists, so it cannot grow without bound.
    pruned = {k: v for k, v in cache.items() if k in keys}
    removed = len(cache) - len(pruned)
    if removed:
        print(f"Dropped {removed} address(es) no longer on the profile.")

    body = json.dumps(dict(sorted(pruned.items())), indent=2) + "\n"
    if cache_path.exists() and cache_path.read_text(encoding="utf-8") == body:
        print(f"No change - {sum(1 for v in pruned.values() if v)} of "
              f"{len(pruned)} address(es) placed.")
        return

    cache_path.write_text(body, encoding="utf-8", newline="\n")
    placed = sum(1 for v in pruned.values() if v)
    print(f"Wrote {cache_path.name}: {placed} of {len(pruned)} address(es) "
          f"placed ({found} resolved this run).")


if __name__ == "__main__":
    main()
