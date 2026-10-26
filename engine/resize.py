"""Stage 1: parse -> roles -> rigid groups -> proportional anchor baseline.

No assets are opened and only geometry/font_size are written back. Subsequent
stages can reuse these helpers for templates, text fitting and candidate scores.
"""

from copy import deepcopy
from dataclasses import dataclass
from math import cos, hypot, isfinite, radians, sin
import re
from typing import Any

BACKGROUND_COVERAGE = 0.90
LOGO_MAX_AREA = 0.08
EDGE_BAND = 0.22
CENTER_TOLERANCE = 0.08
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
        if len(unit.members) == 1 and first.role == "background" and (
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


def validate_output(source: dict, output: dict, target_canvas: dict) -> list[str]:
    """Check structure only; readability and collision repair are later stages."""
    problems = []
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
        if element.type == "text":
            font = result.get("font_size", 0)
            if not isinstance(font, (int, float)) or not isfinite(font) or font < 1:
                problems.append(f"{element.id}: invalid font size")
        crop_background = (_is_background(element, canvas, lowest_z)
                           and original.get("fit") == "cover")
        if element.type == "image" and not crop_background and element.w > 0 and element.h > 0:
            if w <= 0 or h <= 0 or abs((w / h) / (element.w / element.h) - 1) > 0.05:
                problems.append(f"{element.id}: image aspect ratio changed")
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
        output = anchor_baseline(source, target_canvas, canvas, units)
        if not validate_output(source, output, target_canvas):
            return output
    except Exception:
        # Keep the fallback independent of role/group heuristics and ranking.
        pass
    return safe_baseline(source, target_canvas)
