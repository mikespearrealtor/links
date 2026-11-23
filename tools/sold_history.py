"""Dress the rendered sold section in every sale since 2006.

render-listings.py draws the sold map from sold.json, which only reaches back
a year or two. This adds the long history from sold-history.json on top of
that drawing, as a post-pass over the section it rendered:

- one dot per address ever sold (a white core in a black ring), drawn under
  the recent-sale circles, which stay for row hover but only show when lit;
- up to fifteen SOLD tags for the opening sequence in index.html, picked from
  the newest sales inside the opening frame (Montrose at 3.2x);
- the legend becomes the all-time count, the "outside this view" note goes
  (it counted only the rows, and next to an all-time count it misleads), the
  map's own hover discs go (hundreds of dots pile the targets up and the wrong
  one lights; rows still light their dot), and the HAR row is reworded.

The opening sequence and the dot styles live in index.html's script and CSS;
everything here is markup. Kept apart from render-listings.py so that file
still reads as "the page from the scraped data", and this as the one layer
that is not scraped.
"""
import json
import math
import pathlib
import re

Z0, C0 = 3.2, (156.0, 128.0)   # the opening frame; index.html uses the same
N_TAGS = 15
# The tag drawing, in map units before the opening's 1/sqrt(Z0) scale.
W, H, R, P, PW = 27, 10.5, 1.4, 2.8, 2.0


def load(path: pathlib.Path) -> list:
    """[(lon, lat, year), ...], or [] if the file is missing or malformed."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return [(float(lon), float(lat), int(yr)) for lon, lat, yr in data["points"]]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"WARNING: {path.name} unusable ({exc}); the sold map shows recent sales only.")
        return []


def decorate(sec: str, history: list, frame) -> str:
    """The sold section with the history layered in. Returns it unchanged if
    there is no history or no map to add it to."""
    if not history or '<svg class="map"' not in sec or '<circle class="map-sold' not in sec:
        return sec
    VW, VH = frame.width, frame.height
    recent = [(h, float(x), float(y), "is-off" in c) for c, h, x, y in re.findall(
        r'<circle class="map-sold([^"]*)" data-home="([^"]+)"[^>]*? cx="([-\d.]+)" cy="([-\d.]+)"', sec)]
    row_order = re.findall(r'<a class="row" data-home="([^"]+)"', sec)

    hist = []
    for lon, lat, yr in history:
        x, y = frame.project(lon, lat)
        if 2 < x < VW - 2 and 2 < y < VH - 2:
            hist.append((x, y, yr))
    # A recent sale is already drawn; its history twin would double the dot.
    pts = [p for p in hist
           if all(math.hypot(p[0] - rx, p[1] - ry) > 0.4 for _, rx, ry, _ in recent)]
    # The recent sales get a dot too (off-map ones included, so a row's reveal
    # still finds one).
    allpts = [(x, y, ("hist", x, y)) for x, y, _ in pts] + [(rx, ry, h) for h, rx, ry, _ in recent]
    # Homes, not dots: condo units in one building share a dot but each count.
    total = len(hist) + sum(1 for _, rx, ry, off in recent if not off and
                            all(math.hypot(x - rx, y - ry) > 0.4 for x, y, _ in hist))
    since = min(yr for *_, yr in history)

    # The opening tags: the newest sales in view, the rows' own first, then the
    # history newest-first. They may overlap; skip only one that would bury
    # more than about half of another.
    bw, bh = VW / Z0, VH / Z0
    bx, by = C0[0] - bw / 2, C0[1] - bh / 2
    s0 = 1 / math.sqrt(Z0)
    inb = lambda x, y: bx + 10 < x < bx + bw - 10 and by + 16 < y < by + bh - 4
    on = {h: (x, y) for h, x, y, off in recent if not off and inb(x, y)}
    cands = [(h, *on[h]) for h in [h for h in row_order if h in on] + [h for h in on if h not in row_order]]
    cands += [(("hist", x, y), x, y) for x, y, yr in sorted(pts, key=lambda p: (-p[2], p[1])) if inb(x, y)]
    first = []
    for h, x, y in cands:
        if all(abs(x - p[1]) > 11 or abs(y - p[2]) > 5 for p in first):
            first.append((h, x, y))
        if len(first) == N_TAGS:
            break
    fidx = {h: i for i, (h, *_) in enumerate(first)}

    x0, x1, y1 = -W / 2, W / 2, -P
    y0 = y1 - H
    tag_d = (f"M0,0 L{-PW},{y1} L{x0 + R},{y1} Q{x0},{y1} {x0},{y1 - R} L{x0},{y0 + R} "
             f"Q{x0},{y0} {x0 + R},{y0} L{x1 - R},{y0} Q{x1},{y0} {x1},{y0 + R} L{x1},{y1 - R} "
             f"Q{x1},{y1} {x1 - R},{y1} L{PW},{y1} Z")
    allpts.sort(key=lambda p: p[1])
    dots = "".join(
        f'<g class="hd" data-x="{x:.1f}" data-y="{y:.1f}"' + (f' data-first="{fidx[h]}"' if h in fidx else "") +
        f'><path class="o" d="M{x:.1f},{y:.1f}h0"/><path class="f" d="M{x:.1f},{y:.1f}h0"/></g>'
        for x, y, h in allpts)
    tags = "".join(
        f'<g class="map-tag-pos" data-i="{i}" data-x="{x:.1f}" transform="translate({x:.1f},{y:.1f}) scale({s0:.3f})"><g class="map-tag">'
        f'<path class="bx" d="{tag_d}"/><text x="0" y="{y0 + H / 2 + 0.35:.2f}">SOLD</text></g></g>'
        for i, (h, x, y) in enumerate(first))
    indent = re.search(r'\n( *)<circle class="map-sold', sec).group(1)
    layer = (f'<g class="map-history" aria-hidden="true">{dots}</g>\n'
             f'{indent}<g class="map-tags" aria-hidden="true">{tags}</g>\n{indent}')
    at = sec.index('<circle class="map-sold')
    sec = sec[:at] + layer + sec[at:]

    sec = re.sub(r'\n *<circle class="map-hit"[^>]*/>', '', sec)
    sec = re.sub(r'showing \d+ recent sales?\.', f'showing {total} sales since {since}.', sec)
    sec = re.sub(r'(<i class="map-dot is-sold"></i>)[^<]*', rf'\g<1>{total} sales since {since}', sec)
    sec = re.sub(r'<span class="map-off">[^<]*</span>', "", sec)
    sec = re.sub(r'<span class="row-main">and \d+ more recently sold</span>',
                 '<span class="row-main">Browse past sales</span>', sec)
    return sec
