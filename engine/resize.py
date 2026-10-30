"""Offline design adaptation: roles, rigid groups, templates, fitting, scoring.

Only geometry/font_size are written back. No image assets are opened, and the
renderer remains responsible for pixels. All layout decisions are deterministic.
"""

from copy import deepcopy
from dataclasses import dataclass, replace
from functools import lru_cache
from math import cos, hypot, isfinite, log, radians, sin, sqrt
from pathlib import Path
import re
from typing import Any

BACKGROUND_COVERAGE = 0.90
LOGO_MAX_AREA = 0.08
EDGE_BAND = 0.22
CENTER_TOLERANCE = 0.08
TALL_RATIO = 1.2
BANNER_RATIO = 3.0
STRATEGY_BOUNDARY_WINDOW = 0.18
FONT_DIRECTORY = Path(__file__).resolve().parents[1] / "brand_kit" / "fonts"
ROLE_WEIGHTS = {
    "background": 0, "decoration": 10, "fine_print": 20, "body": 30,
    "badge": 40, "subhead": 50, "hero": 60, "cta": 70,
    "headline": 80, "logo": 90,
}


@dataclass
class Element:
    index: int
    original: dict[str, Any]
    id: str
    type: str
    x: float
    y: float
    w: float
    h: float
    z_index: float
    font_size: float
    content: str
    font_family: str
    role: str = "decoration"


@dataclass
class Unit:
    members: list[Element]
    x: float
    y: float
    w: float
    h: float
    role: str
    priority: float


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if isfinite(result) else default
    except (TypeError, ValueError, OverflowError):
        return default


def parse_source(source: dict) -> tuple[tuple[float, float], list[Element]]:
    """Read schema fields; retain original dictionaries without mutating them."""
    canvas = source["canvas"]
    dimensions = (max(1.0, _number(canvas["width"], 1)),
                  max(1.0, _number(canvas["height"], 1)))
    elements = []
    for index, original in enumerate(source["elements"]):
        elements.append(Element(
            index, original, original["id"], original["type"],
            _number(original["x"]), _number(original["y"]),
            max(0.0, _number(original["width"])),
            max(0.0, _number(original["height"])),
            _number(original.get("z_index", 0)),
            max(1.0, _number(original.get("font_size", 1), 1)),
            original.get("content", ""), original.get("font_family", ""),
        ))
    return dimensions, elements


def _contains(outer: Element, inner: Element) -> bool:
    return (outer.x <= inner.x + 1e-8 and outer.y <= inner.y + 1e-8
            and inner.x + inner.w <= outer.x + outer.w + 1e-8
            and inner.y + inner.h <= outer.y + outer.h + 1e-8)


def _intersection(a: Element, b: Element) -> float:
    return (max(0.0, min(a.x + a.w, b.x + b.w) - max(a.x, b.x))
            * max(0.0, min(a.y + a.h, b.y + b.h) - max(a.y, b.y)))


def _is_background(element: Element, canvas: tuple[float, float], lowest_z: float) -> bool:
    width, height = canvas
    covered = (max(0.0, min(width, element.x + element.w) - max(0.0, element.x))
               * max(0.0, min(height, element.y + element.h) - max(0.0, element.y)))
    return (element.type in {"image", "shape"} and element.z_index == lowest_z
            and covered >= BACKGROUND_COVERAGE * width * height)


def _container_for(element: Element, elements: list[Element]) -> Element | None:
    # Decorative circles and full-canvas backgrounds are not button containers.
    containers = [candidate for candidate in elements
                  if candidate.type == "shape" and candidate.role != "background"
                  and candidate.original.get("shape_type") == "rectangle"
                  and candidate.z_index <= element.z_index and _contains(candidate, element)]
    return min(containers, key=lambda item: (item.w * item.h, item.index), default=None)


def detect_roles(elements: list[Element], canvas: tuple[float, float]) -> None:
    """Infer roles from geometry and typography, never element or asset ids."""
    width, height = canvas
    lowest_z = min((element.z_index for element in elements), default=0)
    for element in elements:
        element.role = "decoration"
        if _is_background(element, canvas, lowest_z):
            element.role = "background"
        elif element.type == "image":
            cx, cy = (element.x + element.w / 2) / width, (element.y + element.h / 2) / height
            edge_distance = min(cx, cy, 1 - cx, 1 - cy)
            element.role = ("logo" if element.w * element.h <= LOGO_MAX_AREA * width * height
                            and edge_distance <= EDGE_BAND else "hero")

    logos = [element for element in elements if element.role == "logo"]
    if logos:
        primary_logo = max(logos, key=lambda element: element.w * element.h / (
            1 + max(0.0, element.y / height)))
        for element in logos:
            if element is not primary_logo:
                element.role = "badge"

    plain_texts = []
    for element in elements:
        if element.type != "text":
            continue
        container = _container_for(element, elements)
        if container is not None and len(element.content.split()) <= 10:
            cx = (container.x + container.w / 2) / width
            cy = (container.y + container.h / 2) / height
            element.role = ("cta" if cy >= 0.55 and abs(cx - 0.5) <= 0.20
                            and container.h >= 0.055 * height else "badge")
            container.role = element.role
        else:
            plain_texts.append(element)
    tiers = sorted({element.font_size for element in plain_texts}, reverse=True)
    for element in plain_texts:
        if element.font_size == tiers[0]:
            element.role = "headline"
        elif len(tiers) > 1 and element.font_size == tiers[1]:
            element.role = "subhead"
        else:
            element.role = "fine_print" if element.font_size <= tiers[0] * 0.35 else "body"
    # Some designs use a short accent-colored action line without a button.
    # Infer it from a compact box below the headline, without inspecting copy.
    headlines = [element for element in plain_texts if element.role == "headline"]
    if headlines and not any(element.role == "cta" for element in elements):
        headline = max(headlines, key=lambda element: element.w * element.h)
        actions = [element for element in plain_texts
                   if element.role != "headline" and 0 < len(element.content.split()) <= 8
                   and element.y >= headline.y + headline.h
                   and element.y + element.h / 2 >= 0.55 * height
                   and element.h <= 2.2 * element.font_size
                   and element.original.get("color") != headline.original.get("color")]
        if actions:
            max(actions, key=lambda element: (element.font_size, -element.index)).role = "cta"


def priority_score(element: Element, canvas: tuple[float, float]) -> float:
    """Tunable role/area/font/contrast/centrality priority, with bounded bonuses."""
    width, height = canvas
    area = min(1.0, element.w * element.h / (width * height))
    font = min(1.0, element.font_size / max(1.0, min(width, height) * 0.1)) if element.type == "text" else 0
    centrality = max(0.0, 1 - hypot((element.x + element.w / 2) / width - 0.5,
                                   (element.y + element.h / 2) / height - 0.5) * 2)
    color = element.original.get("color", element.original.get("fill_color", ""))
    channels = re.findall(r"[\d.]+", color or "")
    # A contrast proxy only: no image pixels are read in stage 1.
    luminance = (sum(min(1.0, _number(c) / 255) * weight
                     for c, weight in zip(channels[:3], (0.2126, 0.7152, 0.0722)))
                 if len(channels) >= 3 else 0.5)
    contrast = abs(luminance - 0.5) * 2
    return ROLE_WEIGHTS[element.role] + 3 * area + 2 * font + contrast + centrality


def _visual_bounds(element: Element) -> tuple[float, float, float, float]:
    angle = radians(_number(element.original.get("rotation", 0)))
    width = max(element.w, abs(cos(angle)) * element.w + abs(sin(angle)) * element.h)
    height = max(element.h, abs(sin(angle)) * element.w + abs(cos(angle)) * element.h)
    return (element.x + (element.w - width) / 2,
            element.y + (element.h - height) / 2, width, height)


def _make_unit(members: list[Element], canvas: tuple[float, float]) -> Unit:
    bounds = [_visual_bounds(element) for element in members]
    x, y = min(box[0] for box in bounds), min(box[1] for box in bounds)
    w = max(box[0] + box[2] for box in bounds) - x
    h = max(box[1] + box[3] for box in bounds) - y
    primary = max(members, key=lambda element: priority_score(element, canvas))
    return Unit(members, x, y, w, h, primary.role, priority_score(primary, canvas))


def group_elements(elements: list[Element], canvas: tuple[float, float]) -> list[Unit]:
    """Union explicit groups and inferred text/logo-on-band containment."""
    parents = list(range(len(elements)))

    def root(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left: int, right: int) -> None:
        parents[root(right)] = root(left)

    explicit: dict[str, int] = {}
    for element in elements:
        group_id = element.original.get("group_id")
        if group_id is not None:
            if group_id in explicit:
                union(explicit[group_id], element.index)
            else:
                explicit[group_id] = element.index
        if element.type == "text" or element.role == "logo":
            container = _container_for(element, elements)
            if container is not None:
                union(container.index, element.index)
    # Layered artwork travels together. Do not absorb text/buttons just because
    # their source boxes happen to intersect a decorative circle.
    heroes = [element for element in elements if element.role == "hero"]
    for element in elements:
        if element.type == "shape" and element.role == "decoration":
            overlaps = [hero for hero in heroes if _intersection(element, hero) > 0]
            if overlaps:
                hero = max(overlaps, key=lambda candidate: _intersection(element, candidate))
                union(hero.index, element.index)
    groups: dict[int, list[Element]] = {}
    for element in elements:
        groups.setdefault(root(element.index), []).append(element)
    return [_make_unit(members, canvas) for members in groups.values()]


def source_overlaps(units: list[Unit]) -> set[tuple[int, int]]:
    """Remember intentional cross-unit overlaps for later candidate scoring."""
    return {(i, j) for i, left in enumerate(units) for j in range(i + 1, len(units))
            if any(a.role != "background" and b.role != "background" and _intersection(a, b) > 0
                   for a in left.members for b in units[j].members)}


def _anchor(position: float, size: float, old_extent: float,
            new_extent: float, new_size: float) -> float:
    center = position + size / 2
    ratio = new_extent / old_extent
    if abs(center / old_extent - 0.5) <= CENTER_TOLERANCE:
        result = new_extent / 2 + (center - old_extent / 2) * ratio - new_size / 2
    elif center < old_extent / 2:
        result = position * ratio
    else:
        result = new_extent - (old_extent - position - size) * ratio - new_size
    return min(max(0.0, result), max(0.0, new_extent - new_size))


def anchor_baseline(source: dict, target_canvas: dict,
                    canvas: tuple[float, float], units: list[Unit]) -> dict:
    """Scale units uniformly and anchor to nearby edges/centers, then clamp.

    Existing cover backgrounds use a canvas-sized crop viewport. An image
    without cover must be contained: preserving its ratio, staying on-canvas
    and full coverage are otherwise incompatible. Explicit groups take priority
    over the special background treatment.
    """
    result = deepcopy(source)
    result["canvas"] = deepcopy(target_canvas)
    width, height = target_canvas["width"], target_canvas["height"]
    scale = min(width / canvas[0], height / canvas[1])
    for unit in sorted(units, key=lambda item: -item.priority):
        first = unit.members[0]
        if (len(unit.members) == 1 and first.role == "background"
                and _number(first.original.get("rotation", 0)) % 180 == 0) and (
                first.type == "shape" or first.original.get("fit") == "cover"):
            result["elements"][first.index].update(x=0.0, y=0.0, width=float(width), height=float(height))
            continue
        factor = min(scale, width / unit.w if unit.w else scale,
                     height / unit.h if unit.h else scale)
        # Avoid boundary roundoff without rounding members independently.
        factor *= 1 - 1e-12
        x = _anchor(unit.x, unit.w, canvas[0], width, unit.w * factor)
        y = _anchor(unit.y, unit.h, canvas[1], height, unit.h * factor)
        for element in unit.members:
            output = result["elements"][element.index]
            output.update(x=x + (element.x - unit.x) * factor,
                          y=y + (element.y - unit.y) * factor,
                          width=element.w * factor, height=element.h * factor)
            if element.type == "text":
                output["font_size"] = max(1.0, element.font_size * factor)
    return result


def select_strategies(canvas: dict) -> list[str]:
    ratio = canvas["width"] / canvas["height"]
    strategies = ["stack" if ratio <= TALL_RATIO else "split" if ratio < BANNER_RATIO else "row"]
    for threshold, neighbors in ((TALL_RATIO, ("stack", "split")),
                                 (BANNER_RATIO, ("split", "row"))):
        if abs(ratio / threshold - 1) <= STRATEGY_BOUNDARY_WINDOW:
            strategies.extend(strategy for strategy in neighbors if strategy not in strategies)
    return strategies


def readable_minimum(canvas: dict) -> float:
    return 11.0 * max(0.75, min(1.5, min(canvas.values()) / 720.0))


@lru_cache(maxsize=64)
def _load_font(family: str, weight: str, style: str):
    """Load local fonts relative to the repo, never relative to the caller cwd."""
    try:
        from PIL import ImageFont
        normalized = re.sub(r"[^a-z0-9]", "", family.lower())
        suffix = ("Bold" if weight == "bold" else "") + ("Italic" if style == "italic" else "")
        suffix = suffix or "Regular"
        files = sorted(path for path in FONT_DIRECTORY.glob("*") if path.suffix.lower() in {".ttf", ".otf"})
        matching = [path for path in files if re.sub(r"[^a-z0-9]", "", path.stem.split("-")[0].lower()) == normalized]
        matching.sort(key=lambda path: (not path.stem.endswith("-" + suffix), not path.stem.endswith("-Regular"), path.name))
        return ImageFont.truetype(str(matching[0]), 128) if matching else None
    except (OSError, ImportError, ValueError):
        return None


@lru_cache(maxsize=1024)
def _glyph_widths(content: str, family: str, weight: str, style: str,
                  runs: tuple) -> tuple[float, ...]:
    font = _load_font(family, weight, style)
    widths = {character: (float(font.getlength(character)) / 128 if font else
                         0.34 if character.isspace() else 1.0 if ord(character) > 0x2fff else 0.65)
              for character in set(content) if character != "\n"}
    result = [widths.get(character, 0.0) for character in content]
    for start, end, run_weight, run_style in runs:
        run_font = _load_font(family, run_weight or weight, run_style or style)
        if run_font:
            for index in range(max(0, start), min(len(content), end)):
                if content[index] != "\n":
                    result[index] = max(result[index], float(run_font.getlength(content[index])) / 128)
    return tuple(result)


def text_extent(element: dict, size: float, box_width: float) -> tuple[float, float, int]:
    """Measure greedy wrapping with real advances; long tokens never get split.

    Mirrors the renderer's space-only wrapping without changing the copy.
    Conservative advance padding covers Pillow/Skia rounding differences.
    """
    content = element.get("content", "")
    if not content:
        return 0.0, 0.0, 0
    runs = tuple((run["start"], run["end"], run.get("font_weight"), run.get("font_style"))
                 for run in element.get("style_runs", []))
    advances = _glyph_widths(content, element.get("font_family", ""),
                             element.get("font_weight", "regular"), element.get("font_style", "normal"), runs)
    spacing = _number(element.get("letter_spacing", 0))
    widths = [max(0.0, advance * size * 1.06 + spacing) for advance in advances]
    lines, line_width, widest, last_space, before_space = 1, 0.0, 0.0, None, 0.0
    for index, character in enumerate(content):
        if character == "\n":
            widest = max(widest, line_width)
            lines, line_width, last_space = lines + 1, 0.0, None
            continue
        if line_width + widths[index] > box_width and last_space is not None:
            widest = max(widest, before_space)
            line_width = sum(widths[last_space + 1:index + 1])
            lines, last_space = lines + 1, None
        else:
            line_width += widths[index]
        if character == " ":
            last_space, before_space = index, line_width - widths[index]
    line_height = max(0.1, _number(element.get("line_height", 1.2), 1.2))
    # Even unusually tight leading must leave room for the final glyph row.
    height = size * (max(1.15, line_height) + (lines - 1) * line_height)
    return max(widest, line_width), height, lines


def fit_font(element: dict, maximum: float, floor: float) -> float:
    def fits(size: float) -> bool:
        width, height, lines = text_extent(element, size, element["width"])
        max_lines = element.get("max_lines")
        return (width <= element["width"] and height <= element["height"]
                and (max_lines is None or lines <= max_lines))

    maximum = max(1.0, maximum)
    if fits(maximum):
        return maximum
    low, high = min(maximum, floor), maximum
    if not fits(low):
        # Emergency only: an extreme banner can be physically unable to carry
        # all copy at the readable floor. Keep every word, penalize the result.
        low, high = 1.0, low
    if not fits(low):
        return 1.0
    for _ in range(30):
        middle = (low + high) / 2
        if fits(middle):
            low = middle
        else:
            high = middle
    return low


def fit_text(result: dict, units: list[Unit], canvas: tuple[float, float], reflow: bool = True) -> dict:
    floor = readable_minimum(result["canvas"])
    texts = [element for unit in units for element in unit.members if element.type == "text"]
    font_caps = {}
    for unit in units:
        for element in unit.members:
            if element.type != "text":
                continue
            output = result["elements"][element.index]
            flexible = len(unit.members) == 1 and not element.original.get("rotation", 0)
            cap = preferred_font(element, canvas, result["canvas"])
            if not flexible or not reflow:
                cap = min(cap, output["font_size"])
            font_caps[element.index] = fit_font(output, cap, floor)
    headline_size = max((font_caps[e.index] for e in texts if e.role == "headline"), default=None)
    subhead_size = max((min(font_caps[e.index], headline_size * 0.80 if headline_size else float("inf"))
                        for e in texts if e.role == "subhead"), default=None)
    for unit in units:
        for element in unit.members:
            if element.type != "text":
                continue
            output = result["elements"][element.index]
            size = font_caps[element.index]
            if headline_size and element.role != "headline":
                size = min(size, headline_size * 0.80)
            if subhead_size and element.role in {"body", "fine_print"}:
                size = min(size, subhead_size * 0.85)
            output["font_size"] = max(1.0, size)
            if reflow and len(unit.members) == 1 and not element.original.get("rotation", 0):
                _, used, _ = text_extent(output, output["font_size"], output["width"])
                used = min(output["height"], max(1e-6, used))
                output["y"] += (output["height"] - used) / 2
                output["height"] = used
    return result


def preferred_font(element: Element, source_canvas: tuple[float, float], target: dict) -> float:
    scale = sqrt(target["width"] * target["height"] / (source_canvas[0] * source_canvas[1]))
    floor = readable_minimum(target)
    multiplier = {"headline": 2.4, "subhead": 1.25, "cta": 1.15}.get(element.role, 1.0)
    return max(floor * multiplier, element.font_size * scale)


def _place_unit(result: dict, unit: Unit, slot: tuple[float, float, float, float],
                maximum_scale: float | None = None) -> None:
    x, y, w, h = slot
    if len(unit.members) == 1 and unit.members[0].type == "text" and not unit.members[0].original.get("rotation", 0):
        # A standalone text box can reflow; a label inside a group stays rigid.
        result["elements"][unit.members[0].index].update(x=x, y=y, width=w, height=h)
        return
    factor = min(w / unit.w if unit.w else 1, h / unit.h if unit.h else 1) * (1 - 1e-12)
    if maximum_scale is not None:
        factor = min(factor, maximum_scale)
    left, top = x + (w - unit.w * factor) / 2, y + (h - unit.h * factor) / 2
    for element in unit.members:
        output = result["elements"][element.index]
        output.update(x=left + (element.x - unit.x) * factor,
                      y=top + (element.y - unit.y) * factor,
                      width=element.w * factor, height=element.h * factor)
        if element.type == "text":
            output["font_size"] = max(1.0, element.font_size * factor)


def _stack_slots(result: dict, units: list[Unit], rect: tuple[float, float, float, float],
                 gap: float, art_fraction: float, canvas: tuple[float, float]) -> None:
    if not units:
        return
    x, y, w, h = rect
    target = result["canvas"]
    scale = sqrt(target["width"] * target["height"] / (canvas[0] * canvas[1]))
    heights = []
    for unit in units:
        if len(unit.members) == 1 and unit.members[0].type == "text":
            element = unit.members[0]
            font = preferred_font(element, canvas, target)
            _, desired, _ = text_extent(element.original, font, w)
            desired *= 1.10
        else:
            desired = unit.h * min(scale, w / max(unit.w, 1e-9))
        cap = {"hero": art_fraction, "logo": 0.18}.get(unit.role, 0.36)
        heights.append(max(1e-6, min(desired, h * cap)))
    gap = min(gap, h / max(1, 3 * len(units)))
    available = max(1e-6, h - gap * (len(units) - 1))
    total = sum(heights)
    factor = min(1.0, available / total)
    heights = [height * factor for height in heights]
    top = y + max(0.0, (available - sum(heights)) / 2)
    for unit, height in zip(units, heights):
        _place_unit(result, unit, (x, top, w, height))
        top += height + gap


def _row_slots(result: dict, units: list[Unit], rect: tuple[float, float, float, float],
               gap: float, canvas: tuple[float, float]) -> None:
    if not units:
        return
    x, y, w, h = rect
    gap = min(gap, w / max(1, 3 * len(units)))
    weights = [sqrt(max(1, sum(len(e.content) for e in unit.members)))
               if any(e.type == "text" for e in unit.members) else 3 for unit in units]
    available = max(1e-6, w - gap * (len(units) - 1))
    natural_scale = sqrt(result["canvas"]["width"] * result["canvas"]["height"] / (canvas[0] * canvas[1]))
    for unit, weight in zip(units, weights):
        width = available * weight / sum(weights)
        _place_unit(result, unit, (x, y, width, h), maximum_scale=natural_scale)
        x += width + gap


def template_layout(source: dict, target: dict, canvas: tuple[float, float], units: list[Unit],
                    strategy: str, margin_fraction: float = 0.045, gap_fraction: float = 0.025,
                    balance: float = 0.44) -> dict:
    result = anchor_baseline(source, target, canvas, units)
    width, height = target["width"], target["height"]
    margin, gap = min(width, height) * margin_fraction, min(width, height) * gap_fraction
    x, y, w, h = margin, margin, width - 2 * margin, height - 2 * margin
    order = {"logo": 0, "hero": 1, "headline": 2, "subhead": 3, "body": 4,
             "cta": 5, "badge": 6, "fine_print": 7, "decoration": 8}
    foreground = sorted((unit for unit in units if unit.role != "background"),
                        key=lambda unit: (order[unit.role], unit.y, unit.x))
    extras = [unit for unit in foreground if unit.role in {"badge", "fine_print", "decoration"}]
    main = [unit for unit in foreground if unit not in extras]
    if strategy == "row":
        # Small secondary copy stays present in a footer rather than stealing
        # the headline's main row. Every element still receives a real box.
        secondary = [unit for unit in main if unit.role in {"subhead", "body"}]
        extras = secondary + extras
        main = [unit for unit in main if unit not in secondary]
    if extras:
        footer_h = h * (0.16 if strategy == "row" else 0.13)
        _row_slots(result, extras, (x, y + h - footer_h, w, footer_h), gap, canvas)
        h -= footer_h + gap
    if strategy == "stack":
        _stack_slots(result, main, (x, y, w, h), gap, balance, canvas)
    elif strategy == "split":
        art = [unit for unit in main if unit.role in {"logo", "hero"}]
        copy = [unit for unit in main if unit not in art]
        art_w = (w - gap) * balance if art and copy else w if art else 0
        if art:
            _stack_slots(result, art, (x, y, art_w, h), gap, 0.78, canvas)
        if copy:
            copy_x = x + art_w + gap if art else x
            _stack_slots(result, copy, (copy_x, y, w - art_w - gap if art else w, h), gap, balance, canvas)
    else:
        art = [unit for unit in main if unit.role in {"logo", "hero"}]
        actions = [unit for unit in main if unit.role == "cta"]
        copy = [unit for unit in main if unit not in art and unit not in actions]
        columns = [(art, balance * 0.65), (copy, 0.50), (actions, 0.26)]
        columns = [(column, weight) for column, weight in columns if column]
        available = w - gap * max(0, len(columns) - 1)
        weight_sum = sum(weight for _, weight in columns)
        for column, weight in columns:
            column_w = available * weight / weight_sum
            _stack_slots(result, column, (x, y, column_w, h), gap, 0.60, canvas)
            x += column_w + gap
    return result


def generate_candidates(source: dict, target: dict, canvas: tuple[float, float], units: list[Unit]):
    """One preservation candidate plus 24 deterministic variants per strategy."""
    yield fit_text(anchor_baseline(source, target, canvas, units), units, canvas, reflow=False)
    for strategy in select_strategies(target):
        for margin in (0.035, 0.065):
            for gap in (0.015, 0.025, 0.04):
                for balance in (0.36, 0.42, 0.48, 0.54):
                    result = template_layout(source, target, canvas, units, strategy, margin, gap, balance)
                    yield fit_text(result, units, canvas)


def score_layout(source: dict, output: dict, units: list[Unit], canvas: tuple[float, float]) -> float:
    """Lower is better. Structural violations dwarf presentation penalties."""
    target = output["canvas"]
    problems = validate_output(source, output, target)
    if problems:
        return 1e12 * len(problems)
    width, height = target["width"], target["height"]
    _, placed = parse_source(output)
    original = {element.index: element for unit in units for element in unit.members}
    membership = {element.index: index for index, unit in enumerate(units) for element in unit.members}
    floor = readable_minimum(target)
    score = 0.0
    important = []
    ratio_change = abs(log((width / height) / (canvas[0] / canvas[1])))
    for result in placed:
        element = original[result.index]
        if element.role == "background":
            continue
        weight = ROLE_WEIGHTS[element.role] / 40
        if element.role in {"logo", "headline", "hero", "cta"}:
            important.append(result)
        if element.type == "text":
            measured_w, measured_h, lines = text_extent(result.original, result.font_size, result.w)
            overflow = max(0.0, measured_w / max(result.w, 1e-9) - 1 - 1e-6)
            overflow += max(0.0, measured_h / max(result.h, 1e-9) - 1 - 1e-6)
            max_lines = element.original.get("max_lines")
            if max_lines is not None:
                overflow += max(0, lines - max_lines)
            score += 1e6 * overflow
            score += 10000 * weight * max(0, 1 - result.font_size / floor) ** 2
            desired = preferred_font(element, canvas, target)
            score += 250 * weight * max(0, 1 - result.font_size / desired) ** 2
        elif element.role in {"logo", "hero"}:
            natural_area = element.w * element.h * width * height / (canvas[0] * canvas[1])
            score += 200 * weight * max(0, 1 - sqrt(result.w * result.h / max(1, natural_area))) ** 2
        # Preserve familiar placement most strongly for nearby aspect ratios.
        movement = ((result.x + result.w / 2) / width - (element.x + element.w / 2) / canvas[0]) ** 2
        movement += ((result.y + result.h / 2) / height - (element.y + element.h / 2) / canvas[1]) ** 2
        score += 1000 * movement / (1 + 8 * ratio_change) / max(1, len(placed) - 1)
        if element.role != "decoration":
            margin = min(result.x, result.y, width - result.x - result.w, height - result.y - result.h)
            score += 10 * max(0, 1 - margin / max(1, min(width, height) * 0.025))

    # Check members, not just bounding groups: a circle's intended overlap
    # with text must not excuse a newly overlapping product image in that unit.
    for i, left in enumerate(placed):
        a = original[left.index]
        if a.role == "background":
            continue
        for right in placed[i + 1:]:
            b = original[right.index]
            if b.role == "background" or membership[a.index] == membership[b.index]:
                continue
            overlap = _intersection(left, right)
            if overlap > 0 and _intersection(a, b) == 0:
                fraction = overlap / max(1e-9, min(left.w * left.h, right.w * right.h))
                busy = {a.type, b.type} == {"text", "image"}
                score += (60000 if busy else 30000) * fraction

    if important:
        left, top = min(e.x for e in important), min(e.y for e in important)
        right, bottom = max(e.x + e.w for e in important), max(e.y + e.h for e in important)
        if width / height > TALL_RATIO:
            score += 800 * max(0, 1 - (right - left) / (width * 0.80)) ** 2
        score += 20 * (abs((left + right) / width - 1) + abs((top + bottom) / height - 1))
        # Reward shared edges or centers, using the nearest aligned neighbor.
        for element in important:
            distances = [min(abs(element.x - other.x), abs(element.x + element.w - other.x - other.w),
                             abs(element.x + element.w / 2 - other.x - other.w / 2),
                             abs(element.y + element.h / 2 - other.y - other.h / 2))
                         for other in important if other is not element]
            if distances:
                score += 5 * min(1, min(distances) / max(1, min(width, height) * 0.1))
    reading_order = {"logo": 0, "hero": 1, "headline": 2, "cta": 3}
    ordered = sorted(important, key=lambda element: reading_order[original[element.index].role])
    for left, right in zip(ordered, ordered[1:]):
        if width / height > TALL_RATIO:
            backwards = (left.x + left.w / 2 - right.x - right.w / 2) / width
        else:
            backwards = (left.y + left.h / 2 - right.y - right.h / 2) / height
        score += 300 * max(0, backwards)
    return score


def validate_output(source: dict, output: dict, target_canvas: dict) -> list[str]:
    """Check ids/order, field preservation, finite bounds, fonts and image ratios."""
    problems = []
    if set(source) != set(output):
        problems.append("top-level fields changed")
    if output.get("canvas") != target_canvas:
        problems.append("target canvas mismatch")
    originals, results = source["elements"], output.get("elements", [])
    if [item.get("id") for item in originals] != [item.get("id") for item in results]:
        return problems + ["element count, ids, or order changed"]
    canvas, parsed = parse_source(source)
    lowest_z = min((element.z_index for element in parsed), default=0)
    width, height = target_canvas["width"], target_canvas["height"]
    for original, result, element in zip(originals, results, parsed):
        allowed = {"x", "y", "width", "height"}
        if original["type"] == "text":
            allowed.add("font_size")
        if ({key: value for key, value in original.items() if key not in allowed}
                != {key: value for key, value in result.items() if key not in allowed}):
            problems.append(f"{element.id}: protected field changed")
        geometry = [result.get(key) for key in ("x", "y", "width", "height")]
        if not all(isinstance(value, (int, float)) and isfinite(value) and value >= 0 for value in geometry):
            problems.append(f"{element.id}: invalid geometry")
            continue
        x, y, w, h = geometry
        if x + w > width + 1e-8 or y + h > height + 1e-8:
            problems.append(f"{element.id}: off canvas")
        vx, vy, vw, vh = _visual_bounds(replace(element, x=x, y=y, w=w, h=h))
        if vx < -1e-8 or vy < -1e-8 or vx + vw > width + 1e-8 or vy + vh > height + 1e-8:
            problems.append(f"{element.id}: rotated bounds off canvas")
        if element.type == "text":
            font = result.get("font_size", 0)
            if not isinstance(font, (int, float)) or not isfinite(font) or font < 1:
                problems.append(f"{element.id}: invalid font size")
        crop_background = (_is_background(element, canvas, lowest_z)
                           and original.get("fit") == "cover")
        if element.type == "image" and not crop_background and element.w > 0 and element.h > 0:
            if w <= 0 or h <= 0 or abs((w / h) / (element.w / element.h) - 1) > 0.05:
                problems.append(f"{element.id}: image aspect ratio changed")
    if problems:
        return problems
    explicit = {}
    for index, element in enumerate(originals):
        if element.get("group_id") is not None:
            explicit.setdefault(element["group_id"], []).append(index)
    for group_id, indices in explicit.items():
        if len(indices) < 2:
            continue
        reference = next(((index, axis) for index in indices for axis in ("width", "height")
                          if originals[index][axis] > 0), None)
        if reference is None:
            continue
        index, axis = reference
        factor = results[index][axis] / originals[index][axis]
        dx = results[index]["x"] - originals[index]["x"] * factor
        dy = results[index]["y"] - originals[index]["y"] * factor
        for member in indices:
            expected = {"x": originals[member]["x"] * factor + dx,
                        "y": originals[member]["y"] * factor + dy,
                        "width": originals[member]["width"] * factor,
                        "height": originals[member]["height"] * factor}
            if any(abs(results[member][key] - value) > 1e-6 for key, value in expected.items()):
                problems.append(f"{group_id}: rigid group changed")
                break
    return problems


def safe_baseline(source: dict, target_canvas: dict) -> dict:
    """Fallback: contain the whole foreground as one rigid scene.

    This bypasses role heuristics and grouping while preserving every explicit
    group and source overlap. Rotated/off-canvas bounds are included.
    """
    canvas, elements = parse_source(source)
    lowest_z = min((element.z_index for element in elements), default=0)
    units, foreground = [], []
    for element in elements:
        if _is_background(element, canvas, lowest_z):
            element.role = "background"
        if (element.role == "background" and element.original.get("group_id") is None
                and _number(element.original.get("rotation", 0)) % 180 == 0
                and (element.type == "shape" or element.original.get("fit") == "cover")):
            units.append(Unit([element], element.x, element.y, element.w, element.h, "background", 0))
        else:
            foreground.append(element)
    if foreground:
        bounds = [_visual_bounds(element) for element in foreground]
        x, y = min(box[0] for box in bounds), min(box[1] for box in bounds)
        w = max(box[0] + box[2] for box in bounds) - x
        h = max(box[1] + box[3] for box in bounds) - y
        units.append(Unit(foreground, x, y, w, h, "body", 1))
    return anchor_baseline(source, target_canvas, canvas, units)


def resize(source: dict, target_canvas: dict) -> dict:
    """Return a new design; missing assets do not affect layout."""
    try:
        canvas, elements = parse_source(source)
        detect_roles(elements, canvas)
        units = group_elements(elements, canvas)
        candidates = list(generate_candidates(source, target_canvas, canvas, units))
        candidates.sort(key=lambda output: score_layout(source, output, units, canvas))
        for output in candidates:
            if not validate_output(source, output, target_canvas):
                return output
    except Exception:
        # Keep the fallback independent of role/group heuristics and ranking.
        pass
    return safe_baseline(source, target_canvas)
