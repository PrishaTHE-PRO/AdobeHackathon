"""
CHAMELEON resize engine.

Approach: move the furniture, don't stretch it.

1. Roles: the full-bleed element is the background (locked, stretched to cover).
   Shapes that are faint or hang off the canvas are decoration. Elements that
   share a group_id, or a text sitting inside a shape, become one rigid unit.
   Everything else (images, text) is its own unit.
2. Layout: units keep their source reading order and are split into 1-4 columns
   (a single column is a vertical stack, several columns are a horizontal row).
   Every composition is tried with a few text boosts and wrap widths.
3. Fitting: for each candidate the largest uniform scale that fits is found by
   binary search. Text is measured with the real Solstice fonts (Pillow) and
   wrapped; only font_size, x, y, width and height are ever changed.
4. Scoring: candidates are scored on size, balance, and readability, and the
   best one wins. Decorations are then placed in free corners.
5. Safety net: every element is clamped inside the canvas at the end.
"""

import math
from functools import lru_cache
from itertools import combinations
from pathlib import Path
from typing import Any

try:
    from PIL import ImageFont
except ImportError:  # pragma: no cover
    ImageFont = None

FONT_DIR = Path(__file__).resolve().parent.parent / "brand_kit" / "fonts"
BOOSTS = (1.0, 1.15, 1.3, 1.5)  # extra growth allowed for text vs. pictures
WRAPS = (1.0, 0.6)  # preferred text width as a fraction of its natural width
MAX_COLS = 4


# ---------------------------------------------------------------- text ----
@lru_cache(maxsize=None)
def _font(family: str, weight: str, style: str):
    if ImageFont is None:
        return None
    base = family.replace(" ", "")
    variant = ("Bold" if weight == "bold" else "") + ("Italic" if style == "italic" else "")
    for name in (f"{base}-{variant or 'Regular'}.ttf", f"{base}-Regular.ttf"):
        path = FONT_DIR / name
        if path.exists():
            return ImageFont.truetype(str(path), 100)
    return None


@lru_cache(maxsize=None)
def _word100(spec: tuple, word: str) -> float:
    font = _font(*spec)
    return font.getlength(word) if font else 52.0 * len(word)


def _spec(el: dict) -> tuple:
    return (el["font_family"], el.get("font_weight", "regular"), el.get("font_style", "normal"))


def _wlen(el: dict, word: str, size: float) -> float:
    return _word100(_spec(el), word) * size / 100 + el.get("letter_spacing", 0) * len(word)


def _single_width(el: dict, size: float) -> float:
    best = 0.0
    for para in el["content"].split("\n"):
        words = para.split()
        total = sum(_wlen(el, w, size) for w in words) + _wlen(el, " ", size) * max(0, len(words) - 1)
        best = max(best, total)
    return best


def _longest_word(el: dict, size: float) -> float:
    return max((_wlen(el, w, size) for w in el["content"].split()), default=0.0)


def _wrap(el: dict, size: float, width: float) -> tuple[int, bool]:
    """Greedy word wrap. Returns (line count, fits_without_breaking_a_word)."""
    space = _wlen(el, " ", size)
    lines, ok = 0, True
    for para in el["content"].split("\n"):
        cur, n = 0.0, 1
        for word in para.split():
            w = _wlen(el, word, size)
            if w > width:
                ok = False
            if cur == 0:
                cur = w
            elif cur + space + w <= width:
                cur += space + w
            else:
                n, cur = n + 1, w
        lines += n
    cap = el.get("max_lines")
    if cap and lines > cap:
        ok = False
    return lines, ok


def _line_h(el: dict, size: float, lines: int) -> float:
    return lines * size * el.get("line_height", 1.2) * 1.04


# --------------------------------------------------------------- units ----
def _bbox(el: dict) -> tuple[float, float, float, float]:
    """Axis-aligned bounds of an element, including its rotation."""
    cx, cy = el["x"] + el["width"] / 2, el["y"] + el["height"] / 2
    a = math.radians(el.get("rotation", 0) or 0)
    c, s = abs(math.cos(a)), abs(math.sin(a))
    hw = (el["width"] * c + el["height"] * s) / 2
    hh = (el["width"] * s + el["height"] * c) / 2
    return cx - hw, cy - hh, cx + hw, cy + hh


class Unit:
    """One rigid block of elements that moves and scales together."""

    def __init__(self, members: list[dict]):
        self.members = members
        boxes = [_bbox(m) for m in members]
        self.x0 = min(b[0] for b in boxes)
        self.y0 = min(b[1] for b in boxes)
        self.x1 = max(b[2] for b in boxes)
        self.y1 = max(b[3] for b in boxes)
        self.fw, self.fh = self.x1 - self.x0, self.y1 - self.y0
        self.has_text = any(m["type"] == "text" for m in members)
        self.is_text = len(members) == 1 and members[0]["type"] == "text"


def _coverage(el: dict, W: int, H: int) -> float:
    w = min(el["x"] + el["width"], W) - max(el["x"], 0)
    h = min(el["y"] + el["height"], H) - max(el["y"], 0)
    return max(w, 0) * max(h, 0) / (W * H)


def _inside(inner: dict, outer: dict) -> bool:
    cx, cy = inner["x"] + inner["width"] / 2, inner["y"] + inner["height"] / 2
    return outer["x"] <= cx <= outer["x"] + outer["width"] and outer["y"] <= cy <= outer["y"] + outer["height"]


def _is_decor(el: dict, W: int, H: int) -> bool:
    off = el["x"] < 0 or el["y"] < 0 or el["x"] + el["width"] > W or el["y"] + el["height"] > H
    return el["type"] == "shape" and (el.get("opacity", 1) < 0.5 or off)


def _build_units(source: dict) -> tuple[list[dict], list[dict], list[Unit]]:
    W, H = source["canvas"]["width"], source["canvas"]["height"]
    els = source["elements"]
    backgrounds = [e for e in els if e["type"] in ("image", "shape") and _coverage(e, W, H) >= 0.85
                   and not e.get("group_id")]
    rest = [e for e in els if e not in backgrounds]

    groups: dict[str, list[dict]] = {}
    for e in rest:
        if e.get("group_id"):
            groups.setdefault(e["group_id"], []).append(e)
    loose = [e for e in rest if not e.get("group_id")]

    decor = [e for e in loose if _is_decor(e, W, H)]
    loose = [e for e in loose if e not in decor]

    # a text sitting inside a shape (no group_id) travels with that shape
    shapes = [e for e in loose if e["type"] == "shape"]
    glued: dict[int, list[dict]] = {}
    for t in [e for e in loose if e["type"] == "text"]:
        hosts = [s for s in shapes if _inside(t, s)]
        if hosts:
            host = min(hosts, key=lambda s: s["width"] * s["height"])
            glued.setdefault(id(host), [host]).append(t)
    used = {id(m) for members in glued.values() for m in members}

    blocks = list(groups.values()) + list(glued.values()) + [[e] for e in loose if id(e) not in used]
    units = sorted((Unit(b) for b in blocks), key=lambda u: (u.y0, u.x0))
    return backgrounds, decor, units


# ------------------------------------------------------------- layout -----
def _natural_width(u: Unit, k: float, boost: float, wf: float) -> float:
    if u.is_text:
        el = u.members[0]
        size = el["font_size"] * k * boost
        single = _single_width(el, size) * 1.06
        return max(_longest_word(el, size) * 1.03, min(single, u.fw * k * boost) * wf)
    return u.fw * (k * boost if u.has_text else k)


def _plan(units, comp, k, boost, wf, W, H, g, mx, my):
    """Sizes for one candidate, or None if it does not fit the canvas."""
    cols = []
    for idxs in comp:
        us = [units[i] for i in idxs]
        cw = max(_natural_width(u, k, boost, wf) for u in us)
        items, hsum = [], 0.0
        for u in us:
            if u.is_text:
                el = u.members[0]
                size = el["font_size"] * k * boost
                lines, ok = _wrap(el, size, cw * 0.98)
                if not ok:
                    return None
                items.append((u, cw, _line_h(el, size, lines), size))
            else:
                s = k * boost if u.has_text else k
                items.append((u, u.fw * s, u.fh * s, s))
            hsum += items[-1][2]
        if hsum + g * (len(us) - 1) > H - 2 * my:
            return None
        cols.append((cw, items, hsum))
    if sum(c[0] for c in cols) + 2 * g * (len(cols) - 1) > W - 2 * mx:
        return None
    return cols


def _max_scale(units, comp, boost, wf, khi, W, H, g, mx, my):
    args = (W, H, g, mx, my)
    if _plan(units, comp, khi, boost, wf, *args):
        return khi
    lo, hi = 0.02, khi
    if not _plan(units, comp, lo, boost, wf, *args):
        return None
    for _ in range(22):
        mid = (lo + hi) / 2
        if _plan(units, comp, mid, boost, wf, *args):
            lo = mid
        else:
            hi = mid
    return lo


def _min_font(cols) -> float:
    fonts = []
    for _, items, _ in cols:
        for u, _, _, s in items:
            for m in u.members:
                if m["type"] == "text":
                    fonts.append(s if u.is_text else m["font_size"] * s)
    return min(fonts, default=99.0)


def _score(cols, k, khi, boost, wf, W, H, g, min_ok) -> float:
    used_w = sum(c[0] for c in cols) + 2 * g * (len(cols) - 1)
    used_h = max(c[2] + g * (len(c[1]) - 1) for c in cols)
    balance = min(used_w / W, used_h / H)
    small = max(0.0, (min_ok - _min_font(cols)) / min_ok)
    return 10 * k / khi + 6 * balance - 4 * abs(math.log2(boost)) - 1.5 * (1 - wf) - 30 * small


def _compositions(n: int):
    for c in range(1, min(n, MAX_COLS) + 1):
        for cuts in combinations(range(1, n), c - 1):
            edges = (0, *cuts, n)
            yield [list(range(edges[i], edges[i + 1])) for i in range(c)]


def _gap(a: Unit, b: Unit, k: float, g: float) -> float:
    """Vertical gap between two stacked units, following the source rhythm."""
    return max(g, (b.y0 - a.y1) * k) if b.y0 >= a.y0 else g


def _place(cols, k, units, source, W, H, g, mx, my) -> dict[str, dict]:
    SH = source["canvas"]["height"]
    out: dict[str, dict] = {}
    n = len(cols)
    sumw = sum(c[0] for c in cols)
    gx = min((W - sumw) / (n + 1), 0.1 * W) if n > 1 else 0
    x = (W - (sumw + gx * (n - 1))) / 2
    avail = H - 2 * my

    # a single column keeps the source's top/bottom balance, rows are centered
    t = 0.5
    if n == 1 and units:
        top_slack, bottom_slack = units[0].y0, SH - max(u.y1 for u in units)
        if top_slack + bottom_slack > 0:
            t = min(1.0, max(0.0, top_slack / (top_slack + bottom_slack)))

    for cw, items, hsum in cols:
        want = [_gap(items[i][0], items[i + 1][0], k, g) for i in range(len(items) - 1)]
        room, total = avail - hsum, sum(want)
        if total <= room:
            ge = min(1.5, room / total) if total else 1.0
            gaps = [d * ge for d in want]
        else:
            floor = g * len(want)
            lam = max(0.0, (room - floor) / (total - floor)) if total > floor else 0.0
            gaps = [g + (d - g) * lam for d in want]
        y = my + (avail - (hsum + sum(gaps))) * t
        if n > 1:
            y = my + (avail - (hsum + sum(gaps))) / 2
        cx = x + cw / 2
        for i, (u, w, h, s) in enumerate(items):
            if u.is_text:
                out[u.members[0]["id"]] = {"x": x, "y": y, "width": cw, "height": h, "font_size": s}
            else:
                ux = cx - w / 2
                for m in u.members:
                    mcx = ux + (m["x"] + m["width"] / 2 - u.x0) * s
                    mcy = y + (m["y"] + m["height"] / 2 - u.y0) * s
                    upd = {"width": m["width"] * s, "height": m["height"] * s}
                    upd["x"], upd["y"] = mcx - upd["width"] / 2, mcy - upd["height"] / 2
                    if m["type"] == "text":
                        upd["font_size"] = max(1.0, m["font_size"] * s)
                    out[m["id"]] = upd
            y += h + (gaps[i] if i < len(gaps) else 0)
        x += cw + gx
    return out


# --------------------------------------------------------- decorations ----
def _overlap(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(w, 0) * max(h, 0)


def _place_decor(decor, placed, source, k, W, H) -> dict[str, dict]:
    """Put decorative shapes in the free corner closest to where they started."""
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]
    kinds = {e["id"]: e["type"] for e in source["elements"]}
    # text and buttons must stay clear; pictures may be lightly layered over
    blockers = [((v["x"], v["y"], v["x"] + v["width"], v["y"] + v["height"]),
                 0.4 if kinds[i] == "image" else 1.0) for i, v in placed.items()]
    out = {}
    for e in decor:
        f0 = min(k, 0.6 * min(W, H) / max(e["width"], e["height"]))
        ax = (e["x"] + e["width"] / 2) / SW * W
        ay = (e["y"] + e["height"] / 2) / SH * H
        best = None
        for f in (f0, f0 * 0.75, f0 * 0.5):
            w, h = e["width"] * f, e["height"] * f
            spots = [(ax - w / 2, ay - h / 2)]
            spots += [(i / 4 * (W - w), j / 4 * (H - h)) for i in range(5) for j in range(5)]
            for px, py in spots:
                px, py = min(max(px, 0), W - w), min(max(py, 0), H - h)
                rect = (px, py, px + w, py + h)
                cost = sum(wt * _overlap(rect, b) for b, wt in blockers)
                cost += 0.2 * (1 - f / f0) * e["width"] * e["height"] * f0 ** 2
                cost += 1e-3 * math.hypot(px + w / 2 - ax, py + h / 2 - ay)
                if best is None or cost < best[0]:
                    best = (cost, px, py, w, h)
        _, px, py, w, h = best
        out[e["id"]] = {"x": px, "y": py, "width": w, "height": h}
        blockers.append(((px, py, px + w, py + h), 0.3))  # keep decorations apart too
    return out


# ------------------------------------------------------------ assembly ----
def _clamp(el: dict, W: int, H: int) -> None:
    """Final safety net: keep the element (with rotation) inside the canvas."""
    x0, y0, x1, y1 = _bbox(el)
    bw, bh = x1 - x0, y1 - y0
    if bw > W or bh > H:
        f = min(W / bw, H / bh)
        cx, cy = el["x"] + el["width"] / 2, el["y"] + el["height"] / 2
        el["width"], el["height"] = el["width"] * f, el["height"] * f
        el["x"], el["y"] = cx - el["width"] / 2, cy - el["height"] / 2
        x0, y0, x1, y1 = _bbox(el)
    el["x"] += -x0 if x0 < 0 else (W - x1 if x1 > W else 0)
    el["y"] += -y0 if y0 < 0 else (H - y1 if y1 > H else 0)


def _uniform_fallback(source: dict, W: int, H: int) -> dict[str, dict]:
    s = min(W / source["canvas"]["width"], H / source["canvas"]["height"])
    out = {}
    for e in source["elements"]:
        upd = {"x": e["x"] * s, "y": e["y"] * s, "width": e["width"] * s, "height": e["height"] * s}
        if e["type"] == "text":
            upd["font_size"] = max(1.0, e["font_size"] * s)
        out[e["id"]] = upd
    return out


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    W, H = int(target_canvas["width"]), int(target_canvas["height"])
    SW, SH = source["canvas"]["width"], source["canvas"]["height"]
    backgrounds, decor, units = _build_units(source)

    base = min(W, H)
    g = max(6.0, 0.03 * base)
    mx = my = max(8.0, 0.05 * base)
    min_ok = max(11.0, 0.017 * math.sqrt(W * H))
    khi = math.sqrt(W * H / (SW * SH))

    best = None
    for comp in _compositions(len(units)):
        for boost in BOOSTS:
            for wf in WRAPS:
                k = _max_scale(units, comp, boost, wf, khi, W, H, g, mx, my)
                if k is None:
                    continue
                cols = _plan(units, comp, k, boost, wf, W, H, g, mx, my)
                score = _score(cols, k, khi, boost, wf, W, H, g, min_ok)
                if best is None or score > best[0]:
                    best = (score, k, cols)

    if best is None:
        updates, k = _uniform_fallback(source, W, H), khi
    else:
        _, k, cols = best
        updates = _place(cols, k, units, source, W, H, g, mx, my)
        updates.update(_place_decor(decor, updates, source, k, W, H))
    for bg in backgrounds:  # locked: stretch to cover the new canvas
        updates[bg["id"]] = {"x": 0, "y": 0, "width": W, "height": H}

    out_elements = []
    for e in source["elements"]:
        el = dict(e)
        el.update(updates.get(e["id"], {}))
        el.pop("_note", None)
        el.pop("_adversarial_note", None)
        _clamp(el, W, H)
        for key in ("x", "y", "width", "height", "font_size"):
            if key in el:
                el[key] = round(float(el[key]), 2)
        out_elements.append(el)

    return {"canvas": {"width": W, "height": H}, "elements": out_elements}
