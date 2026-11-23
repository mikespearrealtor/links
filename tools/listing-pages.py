"""Write a page for every current listing, at /homes/<address>/.

Each page is tools/listing-page.html filled in from listings.json:

  * the photos the scraper mirrors into listing-photos/ (the hero fades
    through all of them; the next four sit beside the facts),
  * price, beds, baths, square feet and stories,
  * the next open house and the days on market, both worked out in the
    visitor's browser so they never go stale between daily runs,
  * the details the scraper reads off the Compass profile: lot size, year
    built, garage, price per square foot, and the notable features,
  * the neighbourhood map from tools/listing-map.py (listing-maps/<slug>.html),
  * Text Mike and Compass buttons (the phone number lives in the template).

Anything the data does not have is left out rather than guessed. Pages for
listings that are no longer on the profile are removed, and the sitemap
lists the pages that exist. Stdlib only; runs after tools/listing-map.py and
before tools/render-listings.py in the daily workflow.

Usage:
    python tools/listing-pages.py
"""

import html
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
SITE = "https://links.mikespear.com"
PAGES = ROOT / "homes"
MAPS = ROOT / "listing-maps"
TEMPLATE = (ROOT / "tools" / "listing-page.html").read_text(encoding="utf-8")


def text_qr() -> str:
    """The home page's "Text Mike" QR code, so both pages always share one."""
    home = (ROOT / "index.html").read_text(encoding="utf-8")
    m = re.search(r"text: \{.*?viewBox: '([^']+)',\s*path: '([^']+)'", home, re.S)
    if not m:
        raise SystemExit("could not find the text QR code in index.html")
    return (f'<svg viewBox="{m.group(1)}" xmlns="http://www.w3.org/2000/svg" shape-rendering="crispEdges" '
            f'role="img" aria-label="QR code to send a text"><path d="{m.group(2)}" fill="#111"/></svg>')


TEXT_QR = text_qr()

TWO_WORD_CITIES = {"League City", "Sugar Land", "Missouri City", "Jersey Village", "Bunker Hill", "Hedwig Village",
                   "Hunters Creek", "Piney Point", "Spring Valley", "Bellaire West", "Pearland West", "Webster City",
                   "Deer Park", "La Porte", "Galena Park", "South Houston", "West University", "Mont Belvieu",
                   "Clear Lake", "Seabrook City", "Texas City", "Fort Bend"}
OUTSIDE = {"Pool", "Corner Lot", "Alley Access", "Outdoor Kitchen", "Balcony", "Roof Deck", "Rooftop Deck",
           "Deck", "Patio", "Porch", "Private Yard", "Generator", "Gated"}

esc = lambda s: html.escape(str(s), quote=True)


def slug_of(address: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", address.lower()).strip("-")


def place_of(url: str) -> tuple[str, str]:
    """City and ZIP from the Compass URL slug, e.g. ...-Houston-TX-77018."""
    m = re.search(r"/homedetails/([^/]+)/", url or "")
    parts = (m.group(1) if m else "").split("-")
    if "TX" not in parts:
        return "Houston", ""
    i = len(parts) - 1 - parts[::-1].index("TX")
    zip_ = parts[i + 1] if i + 1 < len(parts) else ""
    city = parts[i - 1] if i >= 1 else "Houston"
    if i >= 2 and f"{parts[i - 2]} {parts[i - 1]}" in TWO_WORD_CITIES:
        city = f"{parts[i - 2]} {parts[i - 1]}"
    return city, zip_


def num(n) -> str:
    if n is None:
        return ""
    return f"{n:,.0f}" if float(n).is_integer() else f"{n:g}"


def money(n) -> str:
    return f"${n:,.0f}"


KIND_NAMES = {"Single Family": "Single-family home", "Townhouse": "Townhouse", "Condo": "Condo",
              "Multi Family": "Apartment", "Co-op": "Co-op", "Land": "Lot", "Farm & Ranch": "Ranch"}


def kind_of(l: dict) -> str:
    """'house', 'townhouse', 'unit' (condo, co-op, apartment) or '' if unknown.
    Falls back on "Unit" in the address when Compass gives no type."""
    t = (l.get("details") or {}).get("propertyType")
    if t == "Single Family":
        return "house"
    if t == "Townhouse":
        return "townhouse"
    if t in ("Condo", "Co-op", "Multi Family"):
        return "unit"
    return "unit" if re.search(r"\bUnit\b", l.get("address") or "", re.I) else ""


def specs(l: dict, d: dict) -> list[str]:
    out = []
    if l.get("beds"):
        out.append(f"<b>{num(l['beds'])}</b> bd")
    if l.get("baths"):
        out.append(f"<b>{num(l['baths'])}</b> ba")
    if l.get("sqft"):
        out.append(f"<b>{num(l['sqft'])}</b> sq ft")
    # An apartment's "stories" is the building's height, not the unit's.
    if d.get("stories") and d.get("propertyType") != "Multi Family":
        s = d["stories"]
        out.append(f"<b>{num(s)}</b> {'story' if s == 1 else 'stories'}")
    return out


def facts(l: dict, d: dict) -> tuple[list[str], list[str]]:
    """The fact lines and whatever features are left over for the features line."""
    feats = list(d.get("features") or [])
    lines = []
    # A condo's lot is the whole complex's, so units skip the lot line and
    # the lot features with it.
    if kind_of(l) == "unit":
        feats = [f for f in feats if f not in ("Corner Lot", "Alley Access")]
    elif d.get("lotSqft"):
        corner = "Corner Lot" in feats
        acres = d["lotSqft"] / 43560
        lines.append(f"<b>{num(d['lotSqft'])} sq ft</b> {'corner lot' if corner else 'lot'}, {acres:.2f} acre")
        if corner:
            feats.remove("Corner Lot")
    if d.get("yearBuilt"):
        lines.append(f"<b>Built {d['yearBuilt']}</b>" + (", new construction" if d.get("newConstruction") else ""))
    elif d.get("newConstruction"):
        lines.append("<b>New construction</b>")
    if d.get("garage"):
        g = d["garage"]
        alley = "Alley Access" in feats
        lines.append(f"<b>{num(g)}-car garage</b>" + (" off the alley" if alley else ""))
        if alley:
            feats.remove("Alley Access")
    if d.get("pricePerSqft") and not l.get("rental"):
        lines.append(f"<b>{money(d['pricePerSqft'])}</b> per sq ft")
    return lines, feats


def features_line(feats: list[str]) -> str:
    inside = [f.lower() for f in feats if f not in OUTSIDE]
    outside = [f.lower() for f in feats if f in OUTSIDE]
    parts = []
    if inside:
        parts.append(f"<b>Inside:</b> {esc(', '.join(inside))}.")
    if outside:
        parts.append(f"<b>Outside:</b> {esc(', '.join(outside))}.")
    return f'        <p class="feat">{" ".join(parts)}</p>\n' if parts else ""


def photo_src(p: dict) -> str:
    return "/" + p["src"].lstrip("/")


def thumb_src(p: dict) -> str:
    """The scraper's small copy where there is one: a 2048px photo shown
    at ~100px comes out jagged, and costs 20x the bytes."""
    return "/" + (p.get("thumb") or p["src"]).lstrip("/")


def short_alt(alt: str, address: str, i: int) -> str:
    alt = (alt or "").strip()
    return alt if alt else (f"{address}" if i == 0 else f"{address}, photo {i + 1}")


def render(l: dict, map_html: str | None, others: str = "") -> str:
    d = l.get("details") or {}
    address = l["address"]
    slug = slug_of(address)
    city, zip_ = place_of(l.get("url", ""))
    rental = bool(l.get("rental"))
    status = l.get("status") or "Active"
    deal = "for lease" if rental else "for sale"
    noun = KIND_NAMES.get(d.get("propertyType") or "")
    if status != "Active":
        status_text = f"{noun} · {status}" if noun else status
    else:
        status_text = f"{noun} {deal}" if noun else deal.capitalize()
    photos = l.get("photos") or []
    price = money(l["price"]) + ('<span class="per">/mo</span>' if rental else "") if l.get("price") else ""

    # One still hero photo; on phones and tablets the next four sit in the
    # grid beside the facts.
    slides = []
    if photos:
        slides.append(f'      <img class="slide" src="{esc(photo_src(photos[0]))}" alt="{esc(short_alt(photos[0].get("alt"), address, 0))}">')
    elif l.get("photo"):
        slides.append(f'      <img class="slide" src="{esc(l["photo"])}" alt="{esc(address)}">')

    # Tablets show the next four (or two) in a grid beside the facts; phones
    # swipe through all eight, ending on the Compass link when it has more.
    # The cells past the grid's size are marked x and only shown on phones.
    rest = photos[1:9]
    shown = len(rest[:4])
    if shown in (2, 3):
        shown = 2
    url = l.get("url", "")
    grid = ""
    if rest:
        # Each opens the photo viewer at its own place; the Compass link is
        # the fallback when scripts are off.
        cells = "".join(f'<a class="ph{" x" if i >= shown else ""}" href="{esc(url)}" target="_blank" rel="noopener" data-i="{i + 1}"><img src="{esc(thumb_src(p))}" alt="{esc(short_alt(p.get("alt"), address, i + 1))}" loading="lazy"></a>' for i, p in enumerate(rest))
        total_all = l.get("photoCount") or len(photos)
        if total_all > 1 + len(rest):
            cells += (f'<a class="more x" href="{esc(url)}" target="_blank" rel="noopener"><span class="big">All {total_all} photos</span>'
                      f'<span class="sub">on Compass <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8"/></svg></span></a>')
        grid = f'      <div class="g4{" one" if shown == 1 else ""}">{cells}</div>'

    # On a computer the next eight photos run in a row under the hero instead.
    # When Compass has more than that, the last spot links to all of them;
    # a new listing with only a few gets "More photos soon" (the page script
    # drops that tile once the listing is two weeks old, unless it's still
    # Coming Soon).
    row = photos[1:9]
    total = l.get("photoCount") or len(photos)
    end = ""
    if len(row) == 8 and total > len(photos[:9]):
        row = row[:7]
        end = (f'<a class="more" href="{esc(url)}" target="_blank" rel="noopener"><span class="big">All {total} photos</span>'
               f'<span class="sub">on Compass <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 17 17 7M9 7h8v8"/></svg></span></a>')
    elif len(row) < 8 and (photos or l.get("photo")):
        always = " data-always" if status == "Coming Soon" else ""
        end = f'<span class="more soon" data-listed="{esc(l.get("listedDate") or "")}"{always}><span class="big">More photos soon</span></span>'
    strip = ""
    n = len(row) + (1 if end else 0)
    if n:
        cells = "".join(f'<a class="ph" href="{esc(url)}" target="_blank" rel="noopener" data-i="{i + 1}"><img src="{esc(thumb_src(p))}" alt="{esc(short_alt(p.get("alt"), address, i + 1))}" loading="lazy"></a>' for i, p in enumerate(row))
        # Four or more fill the width; one to three would come out huge, so
        # they keep the size of a six-across row and sit centred.
        few = " few" if n < 4 else ""
        strip = f'  <div class="strip{few}" style="--n:{n}">{cells}{end}</div>'

    spec = specs(l, d)
    dot = '<span class="dot">·</span>'
    spec_html = f'<span class="specs">{dot.join(spec)}</span>' if spec else ""
    oh = f'<span class="oh" data-oh="{esc(l["openHouse"])}" hidden></span>' if l.get("openHouse") else ""
    lines, feats = facts(l, d)
    facts_html = ""
    if lines:
        facts_html = '        <ul class="fl">\n' + "".join(f"          <li>{x}</li>\n" for x in lines) + "        </ul>\n"
    area = ", ".join(x for x in (l.get("area"), f"{city} {zip_}".strip()) if x)
    plain_spec = " · ".join(re.sub(r"</?b>", "", s) for s in spec)
    canonical = f"{SITE}/homes/{slug}/"
    og_image = SITE + photo_src(photos[0]) if photos else l.get("photo", "")
    # Every mirrored photo, for the viewer (the grid shows only some).
    gallery = [{"src": photo_src(p), "alt": short_alt(p.get("alt"), address, i)} for i, p in enumerate(photos)]
    if not gallery and l.get("photo"):
        gallery = [{"src": l["photo"], "alt": address}]
    gallery_json = json.dumps(gallery, ensure_ascii=False).replace("</", r"<\/")
    map_block = ""
    if map_html:
        # Phones swipe across the computer map now, so the separate phone
        # map (~30 KB) is left out of the page.
        map_html = re.sub(r'<svg class="lm-tall".*?</svg>', "", map_html, flags=re.S)
        map_block = f'\n  <section class="hood">\n{map_html}  </section>\n'

    page = TEMPLATE
    fills = {
        "title": esc(f"{address} · {l.get('area') or city} · Mike Spear"),
        "description": esc(f"{plain_spec} in {l.get('area') or city}, {city}. {status_text}, listed by Mike Spear, Compass."),
        "canonical": esc(canonical),
        "og_title": esc(f"{address} · {re.sub('<[^>]+>', '', price)}"),
        "og_description": esc(" · ".join(x for x in (plain_spec, l.get("area")) if x)),
        "og_image": esc(og_image),
        "slides": "\n".join(slides),
        "status": esc(status_text),
        "address": esc(address),
        "area": esc(area),
        "solo": "" if grid else " solo",
        "price": price,
        "specs": spec_html,
        "open_house": oh,
        "listed": esc(l.get("listedDate") or ""),
        "facts": facts_html,
        "features": features_line(feats),
        "text_qr": TEXT_QR,
        "compass": esc(url),
        "grid": grid,
        "strip": strip,
        "gallery": gallery_json,
        "map": map_block,
        "others": others,
    }
    for k, v in fills.items():
        page = page.replace("{{" + k + "}}", v)
    leftover = re.findall(r"\{\{\w+\}\}", page)
    if leftover:
        raise SystemExit(f"template placeholders left unfilled: {leftover}")
    return page


def others_block(l: dict, listings: list[dict]) -> str:
    """All of Mike's other current listings, for sale and for lease together,
    priciest first, as small tiles to their own pages. Nothing pending; no
    section at all when there's nothing to show."""
    pool = [o for o in listings if o is not l
            and (o.get("status") or "Active") in ("Active", "Coming Soon")
            and o.get("price") and o.get("address")]
    if not pool:
        return ""
    pool.sort(key=lambda o: -o["price"])
    tiles = []
    for o in pool:
        bits = [o.get("area") or ""]
        if o.get("beds"):
            bits.append(f"{num(o['beds'])} bd")
        if o.get("baths"):
            bits.append(f"{num(o['baths'])} ba")
        if o.get("sqft"):
            bits.append(f"{num(o['sqft'])} sq ft")
        sp = " &middot; ".join(f"<span>{esc(b)}</span>" for b in bits if b)
        shots = o.get("photos") or ([{"src": o["photo"]}] if o.get("photo") else [])
        img = f'<img src="{esc(thumb_src(shots[0]))}" alt="" loading="lazy">' if shots else ""
        price = money(o["price"]) + ("/mo" if o.get("rental") else "")
        tiles.append(f'<a href="/homes/{slug_of(o["address"])}/">{img}<div class="tx"><div class="ad">{esc(o["address"])}</div>'
                     f'<div class="pr">{price}</div><div class="sp">{sp}</div></div></a>')
    return ('\n  <section class="others" aria-label="Mike Spear’s other listings">\n'
            f'    <h2>Mike&rsquo;s other listings</h2>\n    <div class="ot">{"".join(tiles)}</div>\n  </section>\n')


def update_sitemap(slugs: list[str]) -> None:
    path = ROOT / "sitemap.xml"
    s = path.read_text(encoding="utf-8")
    s = re.sub(r"\s*<url>\s*<loc>https://links\.mikespear\.com/homes/[^<]*</loc>.*?</url>", "", s, flags=re.S)
    lastmod = re.search(r"<lastmod>([^<]+)</lastmod>", s)
    entries = "".join(
        f"\n  <url>\n    <loc>{SITE}/homes/{slug}/</loc>\n" + (f"    <lastmod>{lastmod.group(1)}</lastmod>\n" if lastmod else "")
        + "    <changefreq>daily</changefreq>\n    <priority>0.8</priority>\n  </url>" for slug in slugs)
    s = s.replace("\n</urlset>", entries + "\n</urlset>")
    path.write_text(s, encoding="utf-8", newline="\n")


def main() -> None:
    listings = json.loads((ROOT / "listings.json").read_text(encoding="utf-8"))
    # Hand fixes for Compass's mistakes, keyed by the Compass address slug
    # (see render-listings.py). Only the words matter here; tools/listing-map.py
    # applies any corrected point.
    cpath = ROOT / "corrections.json"
    corrections = json.loads(cpath.read_text(encoding="utf-8")) if cpath.exists() else {}
    PAGES.mkdir(exist_ok=True)
    keep = []
    for l in listings:
        m = re.search(r"/homedetails/([^/]+)/", l.get("url") or "")
        fix = corrections.get(m.group(1) if m else "")
        if isinstance(fix, dict):
            for field in ("address", "area"):
                if isinstance(fix.get(field), str):
                    l[field] = fix[field]
    # Every listing gets a page in this same run, so the tiles can link to
    # each other's pages before they're written.
    listings = [l for l in listings if l.get("address")]
    for l in listings:
        slug = slug_of(l["address"])
        mp = MAPS / f"{slug}.html"
        page = render(l, mp.read_text(encoding="utf-8") if mp.exists() else None, others_block(l, listings))
        out = PAGES / slug / "index.html"
        out.parent.mkdir(exist_ok=True)
        old = out.read_text(encoding="utf-8") if out.exists() else None
        if page != old:
            out.write_text(page, encoding="utf-8", newline="\n")
            print(f"{slug}: page {'written' if old is None else 'updated'}" + ("" if mp.exists() else " (no map yet)"))
        keep.append(slug)
    for d in PAGES.iterdir():
        if d.is_dir() and d.name not in keep:
            for f in d.iterdir():
                f.unlink()
            d.rmdir()
            print(f"{d.name}: listing gone, page removed")
    update_sitemap(keep)
    print(f"{len(keep)} listing page(s)")


if __name__ == "__main__":
    main()
