"""
CHAMELEON resize engine.

Re-lays-out a design for a new canvas instead of stretching it ("move the
furniture, don't stretch the photo"). The pipeline is:

  1. Analyse  - infer a role for every element from geometry alone
                (background, decoration, logo, hero image, headline, body,
                legal, button/badge groups, badge rows, rotated ribbons,
                backing panels, missing assets).
  2. Lay out  - try several layout families (vertical stack, two columns,
                horizontal banner row). Each one searches for the largest
                type scale that fits, measuring text with the real brand
                fonts so wrap/line counts match the renderer.
  3. Choose   - score every candidate (type size kept, hero size kept, no
                dropped content, aspect-ratio prior) and keep the best.
  4. Finish   - place backgrounds, decorations and panels, then clamp
                everything inside the canvas.

Hard rules honoured: every id returned exactly once, no asset pixels
touched, images keep their aspect ratio (only full-bleed backgrounds crop),
font_family/content never change, nothing leaves the canvas.
"""

from __future__ import annotations

import copy
import math
import statistics
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

MIN_FONT = 10           # px, smallest body text we will emit
MIN_HEADLINE_FONT = 12  # px, smallest headline we will emit
ABS_MIN_FONT = 7        # px, last resort when a single word cannot fit

_REPO_ROOT = Path(__file__).resolve().parent.parent
_FONT_DIRS = [Path("brand_kit/fonts"), _REPO_ROOT / "brand_kit" / "fonts"]
_ASSET_DIRS = [Path("brand_kit/assets"), _REPO_ROOT / "brand_kit" / "assets"]


# ---------------------------------------------------------------------------
# Text measurement (mirrors renderer/converter/wrap.py + cr-renderer)
# ---------------------------------------------------------------------------

class _Measurer:
    """Measures glyph advances with the same fonts the renderer uses.

    Prefers skia (exactly what cr-renderer draws with), falls back to
    Pillow, and finally to a character-width heuristic so the engine never
    crashes because a font is missing."""

    def __init__(self) -> None:
        self._fonts: dict[tuple, Any] = {}
        self._widths: dict[tuple, float] = {}
        try:
            import skia  # type: ignore
            self._skia = skia
        except Exception:  # pragma: no cover - depends on system libs
            self._skia = None
        try:
            from PIL import ImageFont  # type: ignore
            self._pil = ImageFont
        except Exception:  # pragma: no cover
            self._pil = None

    @staticmethod
    def _style(el: dict) -> tuple[bool, bool]:
        bold = str(el.get("font_weight", "regular")).lower() in ("bold", "semibold", "extrabold", "black")
        italic = el.get("font_style") == "italic" or el.get("italic") is True
        return bold, italic

    def _font_file(self, family: str, bold: bool, italic: bool) -> Optional[Path]:
        stem = "".join(str(family).split())
        if bold and italic:
            suffixes = ["BoldItalic", "Bold", "Italic", "Regular"]
        elif bold:
            suffixes = ["Bold", "Regular"]
        elif italic:
            suffixes = ["Italic", "Regular"]
        else:
            suffixes = ["Regular", "Bold"]
        for d in _FONT_DIRS:
            for suf in suffixes:
                for ext in (".ttf", ".otf"):
                    p = d / f"{stem}-{suf}{ext}"
                    if p.is_file():
                        return p
        return None

    def _font(self, el: dict, size: int):
        bold, italic = self._style(el)
        key = (el.get("font_family", ""), bold, italic, size)
        if key in self._fonts:
            return self._fonts[key]
        path = self._font_file(el.get("font_family", ""), bold, italic)
        font = None
        if path is not None:
            if self._skia is not None:
                try:
                    tf = self._skia.Typeface.MakeFromFile(str(path))
                    if tf is not None:
                        font = ("skia", self._skia.Font(tf, size))
                except Exception:
                    font = None
            if font is None and self._pil is not None:
                try:
                    font = ("pil", self._pil.truetype(str(path), size))
                except Exception:
                    font = None
        self._fonts[key] = font
        return font

    def char_widths(self, el: dict, size: int, text: str) -> list[float]:
        font = self._font(el, size)
        if font is None:
            bold, _ = self._style(el)
            k = 0.60 if bold else 0.55
            return [size * (0.3 if c == " " else k) for c in text]
        kind, f = font
        if kind == "skia":
            return list(f.getWidths(f.textToGlyphs(text)))
        out = []
        for c in text:
            key = (id(f), c)
            w = self._widths.get(key)
            if w is None:
                w = float(f.getlength(c))
                self._widths[key] = w
            out.append(w)
        return out

    def metrics(self, el: dict, size: int) -> tuple[float, float]:
        """(ascent, descent) in px, both positive."""
        font = self._font(el, size)
        if font is None:
            return 0.95 * size, 0.30 * size
        kind, f = font
        if kind == "skia":
            m = f.getMetrics()
            return -m.fAscent, m.fDescent
        a, d = f.getmetrics()
        return float(a), float(d)


_M = _Measurer()


def _wrap_stats(widths: list[float], text: str, max_width: float) -> tuple[int, float, bool]:
    """Greedy wrap identical to the renderer's. Returns
    (line_count, widest_line, a_word_overflows)."""
    if not text:
        return 1, 0.0, False
    chars = list(text)
    line_w = 0.0
    last_space = None
    for i, c in enumerate(chars):
        if c == "\n":
            line_w, last_space = 0.0, None
            continue
        w = widths[i] if i < len(widths) else 0.0
        if line_w + w > max_width and last_space is not None:
            chars[last_space] = "\n"
            line_w = sum(widths[j] for j in range(last_space + 1, i + 1) if chars[j] != "\n")
            last_space = None
        else:
            line_w += w
        if c == " ":
            last_space = i
    widest, n, cur = 0.0, 1, 0.0
    for c, w in zip(chars, widths):
        if c == "\n":
            widest, cur, n = max(widest, cur), 0.0, n + 1
        else:
            cur += w
    widest = max(widest, cur)
    return n, widest, widest > max_width + 0.5


def _text_metrics(el: dict, size: int, width: float) -> tuple[int, float, bool, float]:
    """(lines, widest_line, overflow, box_height) for `el` at `size` wrapped
    into `width` (measured slightly narrower than the box so our line count
    is never lower than the renderer's)."""
    content = str(el.get("content", ""))
    widths = _M.char_widths(el, size, content)
    ls = float(el.get("letter_spacing", 0) or 0)
    if ls:
        widths = [w + ls for w in widths]
    n, widest, overflow = _wrap_stats(widths, content, width * 0.97)
    asc, desc = _M.metrics(el, size)
    lh = float(el.get("line_height", 1.2) or 1.2)
    height = (n - 1) * size * lh + asc + desc + 2
    return n, widest, overflow, height


def _single_line_width(el: dict, size: int) -> float:
    content = str(el.get("content", ""))
    ls = float(el.get("letter_spacing", 0) or 0)
    return sum(_M.char_widths(el, size, content)) + ls * max(0, len(content) - 1)


def _longest_word_width(el: dict, size: int) -> float:
    content = str(el.get("content", ""))
    widths = _M.char_widths(el, size, content)
    best = cur = 0.0
    for c, w in zip(content, widths):
        if c in " \n":
            best, cur = max(best, cur), 0.0
        else:
            cur += w
    return max(best, cur)


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------

def _box(el: dict) -> tuple[float, float, float, float]:
    x, y = float(el.get("x", 0)), float(el.get("y", 0))
    return x, y, x + float(el.get("width", 0)), y + float(el.get("height", 0))


def _rot_extent(w: float, h: float, deg: float) -> tuple[float, float]:
    r = math.radians(deg or 0.0)
    c, s = abs(math.cos(r)), abs(math.sin(r))
    return w * c + h * s, w * s + h * c


def _rot_box(x: float, y: float, w: float, h: float, deg: float) -> tuple[float, float, float, float]:
    rw, rh = _rot_extent(w, h, deg)
    cx, cy = x + w / 2, y + h / 2
    return cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2


def _union(boxes) -> tuple[float, float, float, float]:
    boxes = list(boxes)
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def _inter(a, b) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _area(b) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _contains(outer, inner, tol: float = 2.0) -> bool:
    return (inner[0] >= outer[0] - tol and inner[1] >= outer[1] - tol and
            inner[2] <= outer[2] + tol and inner[3] <= outer[3] + tol)


def _alpha(color: Any) -> float:
    try:
        s = str(color)
        if s.startswith("rgba"):
            return float(s[s.index("(") + 1:s.index(")")].split(",")[3])
    except Exception:
        pass
    return 1.0


def _asset_missing(asset_id: Optional[str]) -> bool:
    if not asset_id:
        return True
    dirs = [d for d in _ASSET_DIRS if d.is_dir()]
    if not dirs:  # cannot tell - assume present
        return False
    return not any((d / asset_id).is_file() for d in dirs)


# ---------------------------------------------------------------------------
# Layout units
# ---------------------------------------------------------------------------

class Lay:
    """A sized unit ready to be placed: w x h, plus a placer that returns
    {element_id: {property: value}} for a top-left position."""

    def __init__(self, w: float, h: float, placer: Callable[[float, float], dict]):
        self.w, self.h, self._placer = w, h, placer

    def place(self, x: float, y: float) -> dict:
        return self._placer(x, y)


class Item:
    role = "item"
    priority = 50

    def __init__(self, elems: list[dict]):
        self.elems = elems
        self.ids = [e["id"] for e in elems]
        self.src = _union(_box(e) for e in elems)
        self.order = ((self.src[1] + self.src[3]) / 2, (self.src[0] + self.src[2]) / 2)
        self.anchor = "middle"

    @property
    def src_w(self) -> float:
        return self.src[2] - self.src[0]

    @property
    def src_h(self) -> float:
        return self.src[3] - self.src[1]

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        raise NotImplementedError


class TextItem(Item):
    def __init__(self, el: dict, role: str = "body"):
        super().__init__([el])
        self.el = el
        self.role = role
        self.priority = {"headline": 100, "body": 60, "legal": 30}.get(role, 55)
        n, _, _, _ = _text_metrics(el, int(round(el["font_size"])), el["width"])
        self.src_lines = n

    def min_font(self) -> int:
        return MIN_HEADLINE_FONT if self.role == "headline" else MIN_FONT

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        el = self.el
        src_fs = float(el["font_size"])
        fs = int(round(max(self.min_font(), min(src_fs * t, src_fs * 4))))
        if fill:
            w = maxw
        else:
            # room for a comfortable measure (~35 characters) when available
            w = min(maxw, max(self.src_w * t * 1.1, 0.6 * maxw, 17.5 * fs))
        # make sure the widest word fits, shrinking type if it really must
        while fs > ABS_MIN_FONT and _longest_word_width(el, fs) > w * 0.97:
            if w < maxw:
                w = min(maxw, _longest_word_width(el, fs) / 0.97 + 2)
                continue
            fs -= 1
        n, widest, overflow, h = _text_metrics(el, fs, w)
        max_lines = el.get("max_lines")
        while max_lines and n > max_lines and fs > self.min_font():
            fs -= 1
            n, widest, overflow, h = _text_metrics(el, fs, w)
        # a short single-line label should not wrap if it could fit wider
        if self.src_lines == 1 and n > 1 and _single_line_width(el, fs) / 0.97 + 2 <= maxw:
            w = _single_line_width(el, fs) / 0.97 + 2
            n, widest, overflow, h = _text_metrics(el, fs, w)
        eid = el["id"]
        fs_final, w_final, h_final = fs, w, h

        def placer(x, y):
            return {eid: {"x": x, "y": y, "width": w_final, "height": h_final,
                          "font_size": fs_final}}
        lay = Lay(w, h, placer)
        lay.font_size = fs
        return lay


class ImageItem(Item):
    def __init__(self, el: dict, role: str = "image"):
        super().__init__([el])
        self.el = el
        self.role = role
        self.aspect = max(1e-3, float(el["width"])) / max(1e-3, float(el["height"]))
        self.priority = {"logo": 95, "hero": 85}.get(role, 40)

    def layout_box(self, w: float, h: float) -> Lay:
        """Largest aspect-correct box inside w x h."""
        if w / max(h, 1e-6) > self.aspect:
            w = h * self.aspect
        else:
            h = w / self.aspect
        eid = self.el["id"]
        return Lay(w, h, lambda x, y: {eid: {"x": x, "y": y, "width": w, "height": h}})

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        cap = 0.6 if self.role == "logo" else 0.9
        w = min(self.src_w * t, maxw * cap)
        return self.layout_box(w, w / self.aspect)


class ShapeItem(Item):
    """A free-standing (non-decorative) shape such as a divider line."""

    def __init__(self, el: dict):
        super().__init__([el])
        self.el = el
        self.role = "divider"
        self.priority = 20

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        w = min(maxw, self.src_w * t)
        h = max(1.0, self.src_h * t)
        eid = self.el["id"]
        return Lay(w, h, lambda x, y: {eid: {"x": x, "y": y, "width": w, "height": h}})


class GroupItem(Item):
    """Designer-intended unit (button, badge, ribbon...). Scaled uniformly;
    labels sitting on a shape are re-fitted so they stay on one line and
    vertically centred on it."""

    def __init__(self, elems: list[dict], role: str = "group"):
        super().__init__(elems)
        self.role = role
        self.rotation = max((float(e.get("rotation", 0) or 0) for e in elems), key=abs, default=0.0)
        self.priority = {"button": 90, "badge": 50, "ribbon": 45}.get(role, 50)
        shapes = [e for e in elems if e["type"] == "shape"]
        self.base = max(shapes, key=lambda e: e["width"] * e["height"]) if shapes else None
        self.labels = []
        for e in elems:
            if e["type"] == "text" and self.base is not None and _contains(_box(self.base), _box(e), tol=6):
                n, _, _, _ = _text_metrics(e, int(round(e["font_size"])), e["width"])
                self.labels.append((e, n))

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        s = t
        for _ in range(4):
            lay = self._layout_scale(s)
            if lay.w <= maxw + 0.5:
                return lay
            s *= maxw / lay.w * 0.98
        return self._layout_scale(s)

    def layout_height(self, h: float, maxw: float) -> Lay:
        s = h / max(self.src_h, 1e-6)
        lay = self._layout_scale(s)
        if lay.w > maxw:
            lay = self.layout(s * maxw / lay.w, maxw)
        return lay

    def _layout_scale(self, s: float) -> Lay:
        gx, gy = self.src[0], self.src[1]
        rel: dict[str, dict] = {}
        for e in self.elems:
            x0, y0, x1, y1 = _box(e)
            props = {"x": (x0 - gx) * s, "y": (y0 - gy) * s,
                     "width": (x1 - x0) * s, "height": (y1 - y0) * s}
            if e["type"] == "text":
                props["font_size"] = max(MIN_FONT, int(round(e["font_size"] * s)))
            if e["type"] == "shape":
                if e.get("corner_radius"):
                    props["corner_radius"] = e["corner_radius"] * s
                if e.get("stroke_width"):
                    props["stroke_width"] = max(1.0, e["stroke_width"] * s)
            rel[e["id"]] = props

        if self.base is not None and self.labels:
            b = rel[self.base["id"]]
            bsrc = _box(self.base)
            for lab, src_lines in self.labels:
                p = rel[lab["id"]]
                fs = p["font_size"]
                lsrc = _box(lab)
                # horizontal padding the designer left around the glyphs
                glyph_w_src = min(_single_line_width(lab, int(round(lab["font_size"]))), lsrc[2] - lsrc[0])
                pad_src = max(0.0, ((bsrc[2] - bsrc[0]) - glyph_w_src) / 2)
                pad = max(pad_src * s, 0.6 * fs)
                need = _single_line_width(lab, fs) / 0.97 + 2 * pad if src_lines == 1 else 0
                if need > b["width"]:
                    grow = need - b["width"]
                    b["x"] -= grow / 2
                    b["width"] = need
                if b.get("corner_radius"):
                    b["corner_radius"] = min(b["corner_radius"], b["height"] / 2)
                # vertical: centre the label on the shape when it was centred
                n, _, _, th = _text_metrics(lab, fs, max(1.0, b["width"] - 2 * pad))
                lcy = (lsrc[1] + lsrc[3]) / 2
                bcy = (bsrc[1] + bsrc[3]) / 2
                if th + 4 > b["height"]:
                    grow = th + 4 - b["height"]
                    b["y"] -= grow / 2
                    b["height"] += grow
                if abs(lcy - bcy) < 0.25 * (bsrc[3] - bsrc[1]):
                    p["y"] = b["y"] + b["height"] / 2 - th / 2
                p["height"] = th
                p["x"], p["width"] = b["x"] + pad, max(1.0, b["width"] - 2 * pad)

        # free text in the group (e.g. a caption): never narrower than its
        # longest word, and as tall as its wrapped lines really are
        label_ids = {lab["id"] for lab, _ in self.labels}
        for e in self.elems:
            if e["type"] != "text" or e["id"] in label_ids:
                continue
            p = rel[e["id"]]
            need = _longest_word_width(e, p["font_size"]) / 0.97 + 2
            if p["width"] < need:
                p["x"] -= (need - p["width"]) / 2
                p["width"] = need
            p["height"] = _text_metrics(e, p["font_size"], p["width"])[3]

        # normalise to the (rotated) bounding box
        boxes = [_rot_box(p["x"], p["y"], p["width"], p["height"],
                          float(self._el(eid).get("rotation", 0) or 0)) for eid, p in rel.items()]
        ub = _union(boxes)
        for p in rel.values():
            p["x"] -= ub[0]
            p["y"] -= ub[1]
        w, h = ub[2] - ub[0], ub[3] - ub[1]

        def placer(x, y):
            return {eid: {**p, "x": p["x"] + x, "y": p["y"] + y} for eid, p in rel.items()}
        lay = Lay(w, h, placer)
        lay.scale = s
        return lay

    def _el(self, eid: str) -> dict:
        for e in self.elems:
            if e["id"] == eid:
                return e
        return {}


class RowItem(Item):
    """Several similar units side by side (e.g. a set of badges). Flows
    onto extra lines when the row does not fit the available width."""

    def __init__(self, children: list[Item]):
        super().__init__([e for c in children for e in c.elems])
        self.children = sorted(children, key=lambda c: c.src[0])
        self.role = "row"
        self.priority = max(c.priority for c in children) - 5
        gaps = [b.src[0] - a.src[2] for a, b in zip(self.children, self.children[1:])]
        self.src_gap = max(4.0, statistics.median(gaps)) if gaps else 16.0

    def layout(self, t: float, maxw: float, fill: bool = False) -> Optional[Lay]:
        lays = [c.layout(t, maxw) for c in self.children]
        gap = max(6.0, self.src_gap * t)
        # prefer shrinking a little over wrapping onto a second line
        one_line = sum(l.w for l in lays) + gap * (len(lays) - 1)
        if one_line > maxw and t * maxw / one_line >= 0.6 * t:
            k = maxw / one_line
            for _ in range(3):
                lays = [c.layout(t * k, maxw) for c in self.children]
                gap = max(6.0, self.src_gap * t * k)
                one_line = sum(l.w for l in lays) + gap * (len(lays) - 1)
                if one_line <= maxw:
                    break
                k *= maxw / one_line * 0.99
        lines: list[list[Lay]] = [[]]
        cur = 0.0
        for lay in lays:
            add = lay.w + (gap if lines[-1] else 0)
            if lines[-1] and cur + add > maxw:
                lines.append([lay])
                cur = lay.w
            else:
                lines[-1].append(lay)
                cur += add
        line_ws = [sum(l.w for l in ln) + gap * (len(ln) - 1) for ln in lines]
        line_hs = [max(l.h for l in ln) for ln in lines]
        w = max(line_ws)
        h = sum(line_hs) + gap * (len(lines) - 1)

        def placer(x, y):
            out = {}
            yy = y
            for ln, lw, lh in zip(lines, line_ws, line_hs):
                xx = x + (w - lw) / 2
                for lay in ln:
                    out.update(lay.place(xx, yy + (lh - lay.h) / 2))
                    xx += lay.w + gap
                yy += lh + gap
            return out
        return Lay(w, h, placer)


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

class Model:
    def __init__(self, source: dict):
        self.W = float(source["canvas"]["width"])
        self.H = float(source["canvas"]["height"])
        self.elements = source["elements"]
        self.by_id = {e["id"]: e for e in self.elements}
        self.backgrounds: list[dict] = []
        self.decor: list[dict] = []
        self.hidden: list[dict] = []
        self.panels: list[tuple[dict, list[str]]] = []
        self.flow: list[Item] = []        # items that take part in layout
        self.overlays: list[Item] = []    # corner-pinned small units
        self.hero: Optional[ImageItem] = None
        self.logo: Optional[ImageItem] = None
        self.align = "center"
        self._analyse()

    def _analyse(self) -> None:
        W, H = self.W, self.H
        canvas_area = W * H
        canvas_box = (0, 0, W, H)
        remaining = []
        for e in self.elements:
            b = _box(e)
            cover = _inter(b, canvas_box) / canvas_area
            if e["type"] in ("image", "shape") and cover > 0.85 and not e.get("group_id"):
                self.backgrounds.append(e)
            else:
                remaining.append(e)

        # missing assets are hidden gracefully
        rest = []
        for e in remaining:
            if e["type"] == "image" and _asset_missing(e.get("asset_id")):
                self.hidden.append(e)
            else:
                rest.append(e)
        remaining = rest

        # explicit groups
        groups: dict[str, list[dict]] = {}
        singles: list[dict] = []
        for e in remaining:
            gid = e.get("group_id")
            if gid:
                groups.setdefault(gid, []).append(e)
            else:
                singles.append(e)

        # implicit groups / panels: a shape that contains other elements
        shapes = [e for e in singles if e["type"] == "shape"]
        taken: set[str] = set()
        group_list: list[list[dict]] = [g for g in groups.values()]
        for sh in sorted(shapes, key=lambda e: e["width"] * e["height"]):
            sb = _box(sh)
            solid = float(sh.get("opacity", 1) or 1) * _alpha(sh.get("fill_color")) >= 0.5
            on_canvas = sb[0] >= -1 and sb[1] >= -1 and sb[2] <= W + 1 and sb[3] <= H + 1
            if not (solid and on_canvas):
                continue
            inside = [e for e in singles if e is not sh and e["id"] not in taken
                      and e.get("z_index", 0) >= sh.get("z_index", 0)
                      and _contains(sb, _box(e), tol=4)]
            inside_groups = [g for g in group_list if all(_contains(sb, _box(m), tol=4) for m in g)]
            if len(inside) == 1 and not inside_groups and inside[0]["type"] == "text":
                group_list.append([sh, inside[0]])
                taken.update({sh["id"], inside[0]["id"]})
            elif inside or inside_groups:
                ids = [e["id"] for e in inside] + [m["id"] for g in inside_groups for m in g]
                self.panels.append((sh, ids))
                taken.add(sh["id"])
        singles = [e for e in singles if e["id"] not in taken]

        # decorations: free shapes that are faint, or bleed off the canvas
        rest = []
        for e in singles:
            if e["type"] == "shape":
                b = _box(e)
                faint = float(e.get("opacity", 1) or 1) * _alpha(e.get("fill_color")) < 0.5
                bleeds = b[0] < 0 or b[1] < 0 or b[2] > W or b[3] > H
                thin = min(e["width"], e["height"]) <= max(6, 0.01 * min(W, H))
                if thin and not faint and not bleeds:
                    rest.append(e)  # divider line, takes part in the flow
                else:
                    self.decor.append(e)
            else:
                rest.append(e)
        singles = rest

        # captions: a text hugging the top/bottom of an image, centred on it
        texts_free = [e for e in singles if e["type"] == "text"]
        imgs_free = [e for e in singles if e["type"] == "image"]
        head_fs = max((e["font_size"] for e in texts_free), default=0)
        paired: set[str] = set()
        for im in imgs_free:
            ib = _box(im)
            iw = ib[2] - ib[0]
            best = None
            for tx in texts_free:
                if tx["id"] in paired or tx["font_size"] >= head_fs:
                    continue
                tb = _box(tx)
                if abs((tb[0] + tb[2]) / 2 - (ib[0] + ib[2]) / 2) > 0.15 * iw:
                    continue
                if (tb[2] - tb[0]) > 1.4 * iw:
                    continue
                gap = tb[1] - ib[3] if tb[1] >= ib[3] - 4 else ib[1] - tb[3]
                if -4 <= gap <= max(24.0, 1.5 * tx["font_size"]):
                    if best is None or gap < best[0]:
                        best = (gap, tx)
            if best is not None:
                paired.update({im["id"], best[1]["id"]})
                group_list.append([im, best[1]])
        singles = [e for e in singles if e["id"] not in paired]

        # roles for images
        imgs = [e for e in singles if e["type"] == "image"]
        items: list[Item] = []
        # similar images sharing one horizontal band read as a row (gallery)
        band_rows: list[list[dict]] = []
        for e in sorted(imgs, key=lambda e: e["x"]):
            for row in band_rows:
                r0 = row[0]
                ov = min(_box(r0)[3], _box(e)[3]) - max(r0["y"], e["y"])
                if ov > 0.6 * min(r0["height"], e["height"]) and \
                        0.75 < (e["width"] * e["height"]) / max(1.0, r0["width"] * r0["height"]) < 1.33:
                    row.append(e)
                    break
            else:
                band_rows.append([e])
        for row in band_rows:
            if len(row) > 1 and all(_area(_box(e)) < 0.2 * canvas_area for e in row):
                items.append(RowItem([ImageItem(e, "image") for e in row]))
                imgs = [e for e in imgs if e not in row]
        if imgs:
            def logo_score(e):
                b = _box(e)
                a = e["width"] / max(1, e["height"])
                s = 0.0
                if "logo" in str(e.get("asset_id", "")).lower() or "logo" in e["id"].lower():
                    s += 3
                if (b[1] + b[3]) / 2 < 0.25 * H:
                    s += 1
                if a > 1.6:
                    s += 1
                if e["width"] * e["height"] < 0.12 * canvas_area:
                    s += 1
                return s
            logo = max(imgs, key=logo_score)
            if logo_score(logo) >= 3 or (len(imgs) > 1 and logo_score(logo) >= 2):
                self.logo = ImageItem(logo, "logo")
                items.append(self.logo)
            others = [e for e in imgs if self.logo is None or e is not self.logo.el]
            if others:
                hero = max(others, key=lambda e: e["width"] * e["height"])
                if hero["width"] * hero["height"] > 0.04 * canvas_area:
                    self.hero = ImageItem(hero, "hero")
                    items.append(self.hero)
                for e in others:
                    if self.hero is None or e is not self.hero.el:
                        items.append(ImageItem(e, "image"))

        # roles for text
        texts = [e for e in singles if e["type"] == "text"]
        if texts:
            head = max(texts, key=lambda e: (e["font_size"], e["width"] * e["height"]))
            for e in texts:
                b = _box(e)
                if e is head:
                    role = "headline"
                elif e["font_size"] <= max(14, 0.35 * head["font_size"]) \
                        and e["font_size"] < head["font_size"] and (b[1] + b[3]) / 2 > 0.8 * H:
                    role = "legal"
                else:
                    role = "body"
                items.append(TextItem(e, role))

        for e in singles:
            if e["type"] == "shape":
                items.append(ShapeItem(e))

        # groups: button = biggest group with a label, ribbons are rotated
        gitems = []
        for g in group_list:
            rot = any(abs(float(m.get("rotation", 0) or 0)) > 1 for m in g)
            gitems.append(GroupItem(g, "ribbon" if rot else "badge"))
        labelled = [g for g in gitems if g.role != "ribbon" and g.labels]
        if labelled:
            btn = max(labelled, key=lambda g: (_area(g.src), g.src[1]))
            btn.role, btn.priority = "button", 90

        # clusters of similar groups in one horizontal band become a row
        pool = [g for g in gitems if g.role == "badge"]  # badges and captioned images
        used: set[int] = set()
        for i, g in enumerate(pool):
            if i in used:
                continue
            cluster = [g]
            for j in range(i + 1, len(pool)):
                h = pool[j]
                if j in used:
                    continue
                ov = min(g.src[3], h.src[3]) - max(g.src[1], h.src[1])
                if ov > 0.5 * min(g.src_h, h.src_h) and 0.65 < g.src_h / max(h.src_h, 1) < 1.55:
                    cluster.append(h)
                    used.add(j)
            if len(cluster) > 1:
                used.add(i)
                items.append(RowItem(cluster))
        for i, g in enumerate(pool):
            if i not in used:
                items.append(g)
        items.extend(g for g in gitems if g.role in ("button", "ribbon"))

        # small, off-centre units hugging a corner stay pinned to that corner
        for it in items:
            corner = self._corner(it)
            if isinstance(it, GroupItem) and (it.role == "ribbon" or corner) and it.role != "button":
                it.corner = corner or self._nearest_corner(it.src)
                self.overlays.append(it)
            else:
                self.flow.append(it)
        self.flow.sort(key=lambda it: it.order)

        # anchors: leading top-band items stick to the top, trailing bottom
        # items to the bottom; everything else is centred between them
        for it in self.flow:
            cy = (it.src[1] + it.src[3]) / 2
            if cy < 0.22 * H and it is not self.hero:
                it.anchor = "top"
            elif cy > 0.86 * H and it is not self.hero:
                it.anchor = "bottom"

        # dominant text alignment
        tw: dict[str, float] = {}
        for e in self.elements:
            if e["type"] == "text" and not e.get("group_id"):
                a = e.get("text_align", "left")
                tw[a] = tw.get(a, 0) + e["width"] * e["height"]
        if tw:
            self.align = max(tw, key=tw.get)
            if self.align == "right":
                self.align = "center"

    def _corner(self, it: Item) -> Optional[str]:
        W, H = self.W, self.H
        cx, cy = (it.src[0] + it.src[2]) / 2, (it.src[1] + it.src[3]) / 2
        if _area(it.src) > 0.08 * W * H or abs(cx - W / 2) < 0.2 * W:
            return None
        if isinstance(it, TextItem):
            return None
        vert = "t" if cy < 0.25 * H else "b" if cy > 0.75 * H else None
        hor = "l" if cx < 0.3 * W else "r" if cx > 0.7 * W else None
        return vert + hor if vert and hor else None

    def _nearest_corner(self, b) -> str:
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        return ("t" if cy < self.H / 2 else "b") + ("l" if cx < self.W / 2 else "r")


# ---------------------------------------------------------------------------
# Layout families
# ---------------------------------------------------------------------------

class Result:
    def __init__(self, kind: str):
        self.kind = kind
        self.props: dict[str, dict] = {}
        self.t = 0.0
        self.dropped: list[Item] = []
        self.hero_frac = 0.0
        self.flow_boxes: list[tuple] = []
        self.score = -1e9


def _search(fn: Callable[[float], Optional[Result]], lo: float, hi: float) -> Optional[Result]:
    r = fn(hi)
    if r is not None:
        return r
    best = fn(lo)
    if best is None:
        return None
    for _ in range(14):
        mid = (lo + hi) / 2
        r = fn(mid)
        if r is not None:
            lo, best = mid, r
        else:
            hi = mid
    return best


def _stack(items: list[Item], region, t: float, align: str, fill: bool,
           hero: Optional[ImageItem] = None, hero_min: float = 0.0,
           hero_max: float = 1e9, gap_ref: float = 20.0) -> Optional[tuple[dict, list, float]]:
    """Vertical stack inside `region`. Returns (props, boxes, hero_area)."""
    x0, y0, x1, y1 = region
    rw, rh = x1 - x0, y1 - y0
    if rw <= 4 or rh <= 4:
        return None
    gap = min(max(4.0, gap_ref * t), 0.06 * rh)
    lays: list[Optional[Lay]] = []
    for it in items:
        if it is hero:
            lays.append(None)
            continue
        lay = it.layout(t, rw, fill)
        if lay is None or lay.w > rw + 0.5:
            return None
        lays.append(lay)
    fixed = sum(l.h for l in lays if l is not None) + gap * (len(items) - 1)
    hero_area = 0.0
    if hero is not None and hero in items:
        rem = rh - fixed
        hh = min(rem, rw / hero.aspect, hero_max)
        need = min(max(hero_min, t * hero.src_h), 0.95 * rw / hero.aspect, hero_max)
        if hh < need or hh <= 4:
            return None
        hl = hero.layout_box(hh * hero.aspect, hh)
        lays[items.index(hero)] = hl
        hero_area = hl.w * hl.h
        fixed += hl.h
    elif fixed > rh + 0.5:
        return None

    # anchored placement
    idx = list(range(len(items)))
    top = []
    for i in idx:
        if items[i].anchor == "top":
            top.append(i)
        else:
            break
    bottom = []
    for i in reversed(idx[len(top):]):
        if items[i].anchor == "bottom":
            bottom.insert(0, i)
        else:
            break
    middle = [i for i in idx if i not in top and i not in bottom]
    free = rh - fixed
    # spend some free space on breathing room between units
    extra = min(free * 0.5 / max(1, len(items) - 1), gap * 0.75) if len(items) > 1 else 0.0
    g = gap + extra
    props: dict[str, dict] = {}
    boxes = []

    def put(i, y):
        lay = lays[i]
        if align == "left" and items[i] is not hero:
            x = x0
        else:
            x = x0 + (rw - lay.w) / 2
        props.update(lay.place(x, y))
        boxes.append((x, y, x + lay.w, y + lay.h))
        return y + lay.h

    def block_h(ix):
        return sum(lays[i].h for i in ix) + g * max(0, len(ix) - 1)

    if not top and not bottom:
        y = y0 + (rh - block_h(middle)) / 2
        for i in middle:
            y = put(i, y) + g
    else:
        y = y0
        for i in top:
            y = put(i, y) + g
        top_end = y - g if top else y0
        yb = y1 - block_h(bottom)
        bottom_start = yb
        for i in bottom:
            yb = put(i, yb) + g
        if middle:
            lo = top_end + (g if top else 0)
            hi = bottom_start - (g if bottom else 0)
            y = lo + max(0.0, (hi - lo - block_h(middle)) / 2)
            for i in middle:
                y = put(i, y) + g
    return props, boxes, hero_area


class Engine:
    def __init__(self, source: dict, target: dict):
        self.m = Model(source)
        self.W = float(target["width"])
        self.H = float(target["height"])
        self.sw, self.sh = self.m.W, self.m.H
        self.t_ref = math.sqrt((self.W * self.H) / (self.sw * self.sh))
        gaps = []
        flow = sorted(self.m.flow, key=lambda it: it.src[1])
        for a, b in zip(flow, flow[1:]):
            d = b.src[1] - a.src[3]
            if d > 0:
                gaps.append(d)
        self.gap_ref = min(max(statistics.median(gaps) if gaps else 24.0, 12.0), 40.0)

    # ---- helpers ---------------------------------------------------------
    def _droppable(self, items: list[Item]) -> list[Item]:
        # logo / hero have high priority, so they only go as a last resort
        protected = {"headline", "button"}
        return sorted([it for it in items if it.role not in protected],
                      key=lambda it: (it.priority, -it.order[0]))

    def _with_drops(self, kind: str, solve: Callable[[list[Item]], Optional[Result]],
                    items: list[Item]) -> Optional[Result]:
        dropped: list[Item] = []
        cands = self._droppable(items)
        while True:
            res = solve(dropped)
            if res is not None:
                res.dropped = list(dropped)
                break
            if not cands:
                return None
            dropped.append(cands.pop(0))
        # it fits - but would letting one or two more minor items go read
        # better (bigger type, no cramped columns)? keep whichever scores best
        best, best_score = res, self._score(res, 0.0)
        minor = [it for it in cands if it.role not in ("logo", "hero")]
        for _ in range(2):
            if not minor:
                break
            dropped = dropped + [minor.pop(0)]
            res = solve(dropped)
            if res is None:
                break
            res.dropped = list(dropped)
            sc = self._score(res, 0.0)
            if sc > best_score + 0.05:
                best, best_score = res, sc
        return best

    def _overlays(self, t: float, margin: float, items: list[Item]) -> tuple[dict, list]:
        props, boxes = {}, []
        used: dict[str, float] = {}
        for it in items:
            lay = it.layout(max(t, 0.2), self.W * 0.45)
            if lay.h > self.H * 0.3:
                lay = it.layout(max(t, 0.2) * self.H * 0.3 / lay.h, self.W * 0.45)
            c = it.corner
            off = used.get(c, 0.0)
            x = margin if c[1] == "l" else self.W - margin - lay.w
            y = margin + off if c[0] == "t" else self.H - margin - lay.h - off
            props.update(lay.place(x, y))
            boxes.append((x, y, x + lay.w, y + lay.h))
            used[c] = off + lay.h + margin * 0.5
        return props, boxes

    def _score(self, res: Result, prior: float) -> float:
        typ = min(res.t / self.t_ref, 1.15)
        hero = 0.0
        if self.m.hero is not None:
            src_frac = (self.m.hero.src_w * self.m.hero.src_h) / (self.sw * self.sh)
            tgt_frac = res.hero_frac
            hero = min(1.0, tgt_frac / max(src_frac, 1e-6))
            if self.m.hero in res.dropped:
                hero = -1
        drop = sum(0.12 + it.priority / 300 for it in res.dropped)
        # typographic measure: many short lines (a "word column") read badly
        cramped = 0.0
        for e in self.m.elements:
            p = res.props.get(e["id"])
            if e["type"] != "text" or p is None or any(e["id"] in it.ids for it in res.dropped):
                continue
            n = _text_metrics(e, int(p.get("font_size", e["font_size"])), p["width"])[0]
            if n >= 6 and len(str(e.get("content", ""))) / n < 16:
                cramped += 0.5
        return typ + 0.5 * hero - drop - cramped + prior

    # ---- layout families -------------------------------------------------
    def stack(self) -> Optional[Result]:
        m = self.m
        margin = round(0.055 * min(self.W, self.H))
        items = list(m.flow)
        hero = m.hero if m.hero in items else None
        hero_min = 0.0
        if hero is not None:
            hero_min = 0.55 * (hero.src_h / self.sh) * (self.H - 2 * margin)

        def solve(dropped):
            its = [it for it in items if it not in dropped]
            region = [margin, margin, self.W - margin, self.H - margin]
            res = None
            for _ in range(3):
                reg = tuple(region)

                def fn(t):
                    out = _stack(its, reg, t, m.align, False, hero if hero in its else None,
                                 hero_min if hero in its else 0.0,
                                 (reg[3] - reg[1]) * 0.6, self.gap_ref)
                    if out is None:
                        return None
                    r = Result("stack")
                    r.props, r.flow_boxes, ha = out
                    r.t, r.hero_frac = t, ha / (self.W * self.H)
                    return r
                res = _search(fn, 0.05, self.t_ref * 1.1)
                if res is None:
                    return None
                ov_props, ov_boxes = self._overlays(res.t, margin, m.overlays)
                hit = False
                for ob in ov_boxes:
                    if any(_inter(ob, fb) > 0 for fb in res.flow_boxes):
                        hit = True
                        if (ob[1] + ob[3]) / 2 < self.H / 2:
                            region[1] = max(region[1], ob[3] + margin * 0.4)
                        else:
                            region[3] = min(region[3], ob[1] - margin * 0.4)
                if not hit:
                    break
            res.props.update(ov_props)
            if any(_inter(ob, fb) > 0 for ob in ov_boxes for fb in res.flow_boxes):
                return None
            return res
        return self._with_drops("stack", solve, items + list(m.overlays))

    def columns(self) -> Optional[Result]:
        m = self.m
        if m.hero is None:
            return None
        margin = round(0.06 * min(self.W, self.H))
        hero = m.hero
        text_items = [it for it in m.flow if it is not hero]
        gutter = max(16.0, 0.04 * self.W)
        # hero on the side it leans to in the source, default right
        txt_cx = statistics.mean([(it.src[0] + it.src[2]) / 2 for it in text_items]) if text_items else self.sw / 2
        hero_cx = (hero.src[0] + hero.src[2]) / 2
        hero_left = hero_cx < txt_cx - 0.1 * self.sw

        def solve(dropped):
            its = [it for it in text_items if it not in dropped]
            region = [margin, margin, self.W - margin, self.H - margin]
            res = None
            for _ in range(3):
                x0, y0, x1, y1 = region
                rw, rh = x1 - x0, y1 - y0
                img_w = min(rh * 0.92 * hero.aspect, 0.46 * rw)
                text_w = rw - img_w - gutter
                if text_w < 0.32 * rw:
                    img_w = 0.68 * rw - gutter
                    text_w = rw - img_w - gutter
                hl = hero.layout_box(img_w, rh * 0.92)
                if hero_left:
                    hx, tx0 = x0 + (img_w - hl.w) / 2, x0 + img_w + gutter
                else:
                    hx, tx0 = x1 - img_w + (img_w - hl.w) / 2, x0
                hy = y0 + (rh - hl.h) / 2
                treg = (tx0, y0, tx0 + text_w, y1)

                def fn(t):
                    out = _stack(its, treg, t, m.align, False, gap_ref=self.gap_ref)
                    if out is None:
                        return None
                    r = Result("columns")
                    r.props, r.flow_boxes, _ = out
                    r.t = t
                    return r
                res = _search(fn, 0.05, self.t_ref * 1.1)
                if res is None:
                    return None
                res.props.update(hl.place(hx, hy))
                res.flow_boxes.append((hx, hy, hx + hl.w, hy + hl.h))
                res.hero_frac = hl.w * hl.h / (self.W * self.H)
                ov_props, ov_boxes = self._overlays(res.t, margin * 0.6, m.overlays)
                hit = False
                for ob in ov_boxes:
                    if any(_inter(ob, fb) > 0 for fb in res.flow_boxes):
                        hit = True
                        if (ob[1] + ob[3]) / 2 < self.H / 2:
                            region[1] = max(region[1], ob[3] + margin * 0.4)
                        else:
                            region[3] = min(region[3], ob[1] - margin * 0.4)
                if not hit:
                    break
            res.props.update(ov_props)
            if any(_inter(ob, fb) > 0 for ob in ov_boxes for fb in res.flow_boxes):
                return None
            return res
        return self._with_drops("columns", solve, text_items + list(m.overlays))

    def row(self) -> Optional[Result]:
        m = self.m
        my = max(4.0, round(0.1 * self.H))
        mx = max(my, round(0.025 * self.W))
        x0, y0, x1, y1 = mx, my, self.W - mx, self.H - my
        rw, rh = x1 - x0, y1 - y0
        gx = max(10.0, 0.022 * self.W)

        left: list[Item] = []
        if m.logo is not None:
            left.append(m.logo)
        if m.hero is not None:
            left.append(m.hero)
        left += [it for it in m.flow if isinstance(it, ImageItem) and it not in left]
        right: list[Item] = [it for it in m.overlays if getattr(it, "role", "") == "ribbon"]
        right += [it for it in m.flow if isinstance(it, GroupItem) and it.role == "button"]
        col = [it for it in m.flow if it not in left and it not in right]
        col += [it for it in m.overlays if it not in right]
        col.sort(key=lambda it: it.order)
        everything = left + right + col

        def solve(dropped):
            props: dict[str, dict] = {}
            boxes = []
            hero_area = 0.0
            x = x0
            for it in left:
                if it in dropped:
                    continue
                if it.role == "logo":
                    w = min(0.2 * rw, rh * 0.62 * it.aspect)
                    lay = it.layout_box(w, rh * 0.62)
                elif it.role == "hero":
                    lay = it.layout_box(0.24 * rw, rh)
                    hero_area = lay.w * lay.h
                else:
                    lay = it.layout_box(0.14 * rw, rh * 0.7)
                y = y0 + (rh - lay.h) / 2
                props.update(lay.place(x, y))
                boxes.append((x, y, x + lay.w, y + lay.h))
                x += lay.w + gx
            xr = x1
            for it in reversed(right):
                if it in dropped:
                    continue
                if it.role == "button":
                    target_h = min(rh * 0.62, it.src_h * self.t_ref * 1.25)
                    lay = it.layout_height(max(target_h, 1.0), 0.3 * rw)
                else:
                    lay = it.layout(1.0, 0.22 * rw)
                    if lay.h > rh * 0.8:
                        lay = it.layout(rh * 0.8 / lay.h, 0.22 * rw)
                xr -= lay.w
                y = y0 + (rh - lay.h) / 2
                props.update(lay.place(xr, y))
                boxes.append((xr, y, xr + lay.w, y + lay.h))
                xr -= gx
            tx0, tx1 = x, xr
            if tx1 - tx0 < 0.2 * rw:
                return None
            its = [it for it in col if it not in dropped]
            for it in its:
                it._saved_anchor, it.anchor = it.anchor, "middle"
            try:
                def fn(t):
                    out = _stack(its, (tx0, y0, tx1, y1), t, m.align, True, gap_ref=self.gap_ref * 0.5)
                    if out is None:
                        return None
                    r = Result("row")
                    r.props, r.flow_boxes, _ = out
                    r.t = t
                    return r
                res = _search(fn, 0.05, self.t_ref * 1.3) if its else Result("row")
            finally:
                for it in its:
                    it.anchor = it._saved_anchor
            if res is None:
                return None
            if not its:
                res.t = self.t_ref
            # centre the text column's content horizontally in its slot
            res.props.update(props)
            res.flow_boxes += boxes
            res.hero_frac = hero_area / (self.W * self.H)
            return res
        return self._with_drops("row", solve, everything)

    def faithful(self) -> Optional[Result]:
        """Same shape as the source: the designer's own composition is the
        best layout there is, so keep it and only scale it - provided every
        visible text stays readable at the new scale."""
        rs, rt = self.sw / self.sh, self.W / self.H
        if abs(rt / rs - 1) > 0.06:
            return None
        s = min(self.W / self.sw, self.H / self.sh)
        ox, oy = (self.W - self.sw * s) / 2, (self.H - self.sh * s) / 2
        res = Result("faithful")
        for it in list(self.m.flow) + list(self.m.overlays):
            for e in it.elems:
                p = {"x": ox + e["x"] * s, "y": oy + e["y"] * s,
                     "width": e["width"] * s, "height": e["height"] * s}
                if e["type"] == "text":
                    fs = int(round(e["font_size"] * s))
                    if fs < MIN_FONT:
                        return None
                    p["font_size"] = fs
                    # the box hugs the real text so it claims no phantom space
                    p["height"] = _text_metrics(e, fs, p["width"])[3]
                    if e.get("group_id") or any(e in it.elems and isinstance(it, GroupItem)
                                                and it.labels for it in self.m.flow + self.m.overlays):
                        # labels stay vertically centred on their shape
                        cy = oy + (e["y"] + e["height"] / 2) * s
                        p["y"] = cy - p["height"] / 2
                if e["type"] == "shape":
                    for k in ("corner_radius", "stroke_width"):
                        if e.get(k):
                            p[k] = e[k] * s
                res.props[e["id"]] = p
        # the source itself must be collision-free to be worth keeping
        units = [[i for i in it.ids] for it in list(self.m.flow) + list(self.m.overlays)]
        boxes = {eid: (p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"])
                 for eid, p in res.props.items()}
        for i, a in enumerate(units):
            for b in units[i + 1:]:
                for ea in a:
                    for eb in b:
                        if _inter(boxes[ea], boxes[eb]) > 4:
                            return None
        res.t = s
        return res

    # ---- driver ----------------------------------------------------------
    def run(self) -> Result:
        same_shape = self.faithful()
        if same_shape is not None:
            return same_shape
        r = self.W / self.H
        cands: list[Result] = []
        for kind, fn, prior in (
            ("stack", self.stack, 0.35 if r <= 1.35 else (0.0 if r <= 2.0 else -0.6)),
            ("columns", self.columns, 0.35 if 1.25 < r <= 2.6 else (0.0 if r > 2.6 else -0.3)),
            ("row", self.row, 0.45 if r > 2.6 else (-0.2 if r > 1.8 else -0.8)),
        ):
            try:
                res = fn()
            except Exception:
                res = None
            if res is not None:
                res.score = self._score(res, prior)
                cands.append(res)
        if not cands:
            return None  # type: ignore
        return max(cands, key=lambda c: c.score)


# ---------------------------------------------------------------------------
# Finishing passes
# ---------------------------------------------------------------------------

def _corner_anchor(el: dict, sw: float, sh: float, W: float, H: float, s: float) -> dict:
    """Scale an element by `s` and keep it hugging the corner it hugged."""
    x0, y0, x1, y1 = _box(el)
    w, h = (x1 - x0) * s, (y1 - y0) * s
    cap = 0.7 * min(W, H)
    if max(w, h) > cap:
        k = cap / max(w, h)
        w, h = w * k, h * k
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    if cx < sw / 2:
        x = x0 * s
    else:
        x = W - (sw - x1) * s - w
    if cy < sh / 2:
        y = y0 * s
    else:
        y = H - (sh - y1) * s - h
    if abs(cx - sw / 2) < 0.1 * sw:
        x = W / 2 - w / 2
    if abs(cy - sh / 2) < 0.1 * sh:
        y = H / 2 - h / 2
    return {"x": x, "y": y, "width": w, "height": h}


def _place_decor(decor: list[dict], props: dict, by_id: dict, skip: set,
                 sw: float, sh: float, W: float, H: float, s: float) -> dict[str, dict]:
    """Free-space search for decorative shapes (idea from the `saanvi`
    branch). Starts from the corner-anchored spot, also tries a grid of
    spots at 100/75/50% size, and keeps the one that covers the least text
    and buttons. Pictures may be lightly layered over; decorations also
    keep apart from each other."""
    blockers = []
    for eid, p in props.items():
        if eid in skip or eid not in by_id:
            continue
        weight = 0.4 if by_id[eid]["type"] == "image" else 1.0
        blockers.append(((p["x"], p["y"], p["x"] + p["width"], p["y"] + p["height"]), weight))
    out: dict[str, dict] = {}
    for e in decor:
        home = _corner_anchor(e, sw, sh, W, H, s)
        w0, h0 = home["width"], home["height"]
        best = None
        for f in (1.0, 0.75, 0.5):
            w, h = w0 * f, h0 * f
            spots = [(home["x"] + (w0 - w) / 2, home["y"] + (h0 - h) / 2)]
            spots += [(i / 4 * (W - w), j / 4 * (H - h)) for i in range(5) for j in range(5)]
            for px, py in spots:
                px, py = min(max(px, 0.0), max(0.0, W - w)), min(max(py, 0.0), max(0.0, H - h))
                rect = (px, py, px + w, py + h)
                cost = sum(wt * _inter(rect, b) for b, wt in blockers)
                cost += 0.2 * (1 - f) * w0 * h0          # prefer keeping its size
                cost += 1e-3 * math.hypot(px - home["x"], py - home["y"])  # and its corner
                if best is None or cost < best[0]:
                    best = (cost, px, py, w, h)
        _, px, py, w, h = best
        out[e["id"]] = {"x": px, "y": py, "width": w, "height": h}
        blockers.append(((px, py, px + w, py + h), 0.3))
    return out


def _clamp(el: dict, W: float, H: float) -> None:
    w, h = float(el["width"]), float(el["height"])
    rot = float(el.get("rotation", 0) or 0)
    rw, rh = _rot_extent(w, h, rot)
    need_w, need_h = max(w, rw), max(h, rh)
    if need_w > W or need_h > H:
        k = min(W / need_w, H / need_h) * 0.999
        if el["type"] == "image" or el["type"] == "text":
            w, h = w * k, h * k
        else:
            w, h = min(w, W), min(h, H)
        if el["type"] == "text":
            el["font_size"] = max(ABS_MIN_FONT, int(el["font_size"] * k))
        rw, rh = _rot_extent(w, h, rot)
        need_w, need_h = max(w, rw), max(h, rh)
    cx = float(el["x"]) + w / 2
    cy = float(el["y"]) + h / 2
    cx = min(max(cx, need_w / 2), W - need_w / 2)
    cy = min(max(cy, need_h / 2), H - need_h / 2)
    el["x"], el["y"], el["width"], el["height"] = cx - w / 2, cy - h / 2, w, h


def _transparent(color: Any) -> str:
    try:
        s = str(color)
        if s.startswith("rgb"):
            parts = [p.strip() for p in s[s.index("(") + 1:s.index(")")].split(",")][:3]
            return f"rgba({parts[0]},{parts[1]},{parts[2]},0)"
    except Exception:
        pass
    return "rgba(0,0,0,0)"


def _hide(el: dict) -> None:
    """Gracefully drop an element that cannot be shown legibly (or whose
    asset is missing): it keeps its id and an in-canvas, in-proportion box
    but becomes fully transparent. cr-renderer ignores `opacity` on text,
    so text colours are made transparent as well."""
    el["opacity"] = 0
    if el["type"] == "text":
        el["color"] = _transparent(el.get("color"))
        for run in el.get("style_runs") or []:
            if "color" in run:
                run["color"] = _transparent(run["color"])


def _round(el: dict) -> None:
    for k in ("x", "y", "width", "height", "corner_radius", "stroke_width"):
        if k in el and isinstance(el[k], (int, float)):
            el[k] = round(float(el[k]), 2)
    if "font_size" in el:
        el["font_size"] = int(round(el["font_size"]))


def _uniform_fallback(source: dict, W: float, H: float) -> dict[str, dict]:
    """Last resort: letterbox the whole design (never distorts anything)."""
    sw, sh = float(source["canvas"]["width"]), float(source["canvas"]["height"])
    s = min(W / sw, H / sh)
    ox, oy = (W - sw * s) / 2, (H - sh * s) / 2
    out = {}
    for e in source["elements"]:
        p = {"x": ox + e["x"] * s, "y": oy + e["y"] * s,
             "width": e["width"] * s, "height": e["height"] * s}
        if e["type"] == "text":
            p["font_size"] = max(MIN_FONT, int(round(e["font_size"] * s)))
        out[e["id"]] = p
    return out


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _run_engine(source: dict, target_canvas: dict):
    """Normal pass first; if nothing fits (tiny canvas, huge copy), retry
    once with relaxed minimum type sizes before giving up."""
    global MIN_FONT, MIN_HEADLINE_FONT
    saved = MIN_FONT, MIN_HEADLINE_FONT
    try:
        # readable floor grows with the canvas (idea from the `aditi` branch):
        # 10/12 px on small canvases, up to 15/18 px on posters
        k = max(1.0, min(1.5, min(target_canvas["width"], target_canvas["height"]) / 720.0))
        scaled = (round(saved[0] * k), round(saved[1] * k))
        for mins in (scaled, saved, (8, 8), (ABS_MIN_FONT, ABS_MIN_FONT)):
            MIN_FONT, MIN_HEADLINE_FONT = mins
            try:
                eng = Engine(source, target_canvas)
                res = eng.run()
            except Exception:
                res = None
            if res is not None:
                return res, eng.m
        return None, None
    finally:
        MIN_FONT, MIN_HEADLINE_FONT = saved


def resize(source: dict[str, Any], target_canvas: dict[str, Any]) -> dict[str, Any]:
    W = int(target_canvas["width"])
    H = int(target_canvas["height"])
    sw, sh = float(source["canvas"]["width"]), float(source["canvas"]["height"])
    out_elems = [copy.deepcopy(e) for e in source["elements"]]
    by_id = {e["id"]: e for e in out_elems}

    props: dict[str, dict] = {}
    hidden_ids: set[str] = set()
    res, m = _run_engine(source, target_canvas)

    if res is None or m is None:
        props = _uniform_fallback(source, W, H)
        for e in source["elements"]:
            if e["type"] == "image" and len(source["elements"]) > 1 and \
                    _inter(_box(e), (0, 0, sw, sh)) / (sw * sh) > 0.85:
                props[e["id"]] = {"x": 0, "y": 0, "width": W, "height": H}
            if e["type"] == "image" and _asset_missing(e.get("asset_id")):
                hidden_ids.add(e["id"])
    else:
        props = dict(res.props)
        s_dec = min(W / sw, H / sh) if min(W / sw, H / sh) > 0.25 else math.sqrt(W * H / (sw * sh))

        for bg in m.backgrounds:
            props[bg["id"]] = {"x": 0, "y": 0, "width": W, "height": H}
            if bg["type"] == "image":
                props[bg["id"]]["fit"] = "cover"
        for it in res.dropped:
            hidden_ids.update(it.ids)
        hidden_ids.update(e["id"] for e in m.hidden)
        for eid in hidden_ids:
            if eid in props and eid not in {e["id"] for e in m.hidden}:
                continue
            props[eid] = _corner_anchor(by_id[eid], sw, sh, W, H, min(W / sw, H / sh))
        for it in res.dropped:
            for eid in it.ids:
                props[eid] = _corner_anchor(by_id[eid], sw, sh, W, H, min(W / sw, H / sh))
                if by_id[eid]["type"] == "text":
                    props[eid]["font_size"] = max(MIN_FONT, int(by_id[eid]["font_size"] * min(W / sw, H / sh)))
        # panels wrap whatever they used to contain, with scaled padding
        for sh_el, ids in m.panels:
            boxes = [(props[i]["x"], props[i]["y"], props[i]["x"] + props[i]["width"],
                      props[i]["y"] + props[i]["height"]) for i in ids if i in props and i not in hidden_ids]
            if not boxes:
                props[sh_el["id"]] = _corner_anchor(sh_el, sw, sh, W, H, s_dec)
                continue
            src_inner = _union(_box(by_id[i]) for i in ids)
            pb = _box(sh_el)
            pad = max(4.0, min(src_inner[0] - pb[0], src_inner[1] - pb[1],
                               pb[2] - src_inner[2], pb[3] - src_inner[3]) * res.t)
            u = _union(boxes)
            props[sh_el["id"]] = {"x": u[0] - pad, "y": u[1] - pad,
                                  "width": u[2] - u[0] + 2 * pad, "height": u[3] - u[1] + 2 * pad}
        # decorations go last, into free space around the finished layout
        bg_ids = {e["id"] for e in m.backgrounds}
        props.update(_place_decor(m.decor, props, by_id, hidden_ids | bg_ids, sw, sh, W, H, s_dec))

    for e in out_elems:
        p = props.get(e["id"])
        if p is None:  # safety net: keep relative position, uniform scale
            p = _corner_anchor(e, sw, sh, W, H, min(W / sw, H / sh))
        e.update(p)
        if e["id"] in hidden_ids:
            _hide(e)
        _clamp(e, W, H)
        _round(e)

    return {"canvas": {"width": W, "height": H}, "elements": out_elems}
