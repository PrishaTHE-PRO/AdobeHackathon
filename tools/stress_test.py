"""
Stress-test the engine: every sample design x a catalogue of real-world
canvas sizes. For each pair it runs resize(), checks the hard rules plus a
few quality heuristics, renders a PNG, and finally builds one contact sheet
per source design so you can eyeball everything at once.

    python -m tools.stress_test                 # all samples, all sizes
    python -m tools.stress_test --sizes story,leaderboard
    python -m tools.stress_test --no-render     # checks only (fast)

Results land in output/stress/. Exit code is 1 if any hard rule fails.
"""

import argparse
import json
import math
import sys
from pathlib import Path

from engine.resize import resize

CATALOGUE = {
    "ig_post": (1080, 1080),
    "ig_portrait": (1080, 1350),
    "story": (1080, 1920),
    "yt_thumb": (1280, 720),
    "fb_post": (1200, 630),
    "twitter_header": (1500, 500),
    "linkedin_banner": (1584, 396),
    "fb_cover": (820, 312),
    "leaderboard": (728, 90),
    "billboard": (970, 250),
    "mobile_banner": (320, 50),
    "med_rect": (300, 250),
    "half_page": (300, 600),
    "skyscraper": (160, 600),
    "pinterest": (1000, 1500),
    "a4_poster": (2480, 3508),
    "slide_16x9": (1920, 1080),
    "web_wide": (1600, 400),
}

INPUT_DIR = Path("input")
OUT_DIR = Path("output/stress")
ASSETS_DIR = Path("brand_kit/assets")
MIN_READABLE = 9


def _box(e):
    return e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]


def _rot_box(e):
    r = math.radians(e.get("rotation", 0) or 0)
    w, h = e["width"], e["height"]
    rw = w * abs(math.cos(r)) + h * abs(math.sin(r))
    rh = w * abs(math.sin(r)) + h * abs(math.cos(r))
    cx, cy = e["x"] + w / 2, e["y"] + h / 2
    return cx - rw / 2, cy - rh / 2, cx + rw / 2, cy + rh / 2


def _inter(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


def _visible(e):
    if (e.get("opacity", 1) or 0) <= 0.01:
        return False
    if e["type"] == "text" and str(e.get("color", "")).replace(" ", "").endswith(",0)"):
        return False
    return True


def check(source, target, out):
    """Returns (hard_failures, warnings)."""
    hard, warn = [], []
    W, H = target["width"], target["height"]
    if out.get("canvas") != {"width": W, "height": H}:
        hard.append(f"canvas is {out.get('canvas')}, expected {W}x{H}")
    src = {e["id"]: e for e in source["elements"]}
    ids = [e["id"] for e in out["elements"]]
    if sorted(ids) != sorted(src):
        hard.append("element ids differ from source")
    for e in out["elements"]:
        s = src.get(e["id"], {})
        for k in ("content", "font_family", "asset_id", "type"):
            if s.get(k) != e.get(k):
                hard.append(f"{e['id']}: '{k}' changed")
        for b in (_box(e), _rot_box(e)):
            if b[0] < -0.5 or b[1] < -0.5 or b[2] > W + 0.5 or b[3] > H + 0.5:
                hard.append(f"{e['id']}: off-canvas {tuple(round(v) for v in b)}")
                break
        if e["type"] == "image" and s.get("fit") != "cover" and e.get("fit") != "cover":
            a0 = s["width"] / s["height"]
            a1 = e["width"] / max(e["height"], 1e-6)
            if abs(a1 / a0 - 1) > 0.05:
                hard.append(f"{e['id']}: aspect changed {a0:.2f} -> {a1:.2f}")
        if e["type"] == "text" and _visible(e) and e["font_size"] < MIN_READABLE:
            warn.append(f"{e['id']}: font {e['font_size']}px")

    # collisions between visible text and other visible foreground content
    canvas_area = W * H
    fg = [e for e in out["elements"] if _visible(e)
          and _inter(_box(e), (0, 0, W, H)) < 0.85 * canvas_area
          and not (e["type"] == "shape" and (e.get("opacity", 1) or 1) < 0.5)]
    texts = [e for e in fg if e["type"] == "text"]
    for t in texts:
        for o in fg:
            if o is t:
                continue
            if t.get("group_id") and t.get("group_id") == o.get("group_id"):
                continue
            if o["type"] == "shape" and _inter(_box(t), _box(o)) >= 0.9 * t["width"] * t["height"]:
                continue  # text sitting on its own backing shape
            if o["type"] == "text" and ids.index(o["id"]) < ids.index(t["id"]):
                continue  # report each text pair once
            if _inter(_box(t), _box(o)) > 4:
                warn.append(f"collision: {t['id']} x {o['id']}")
    dropped = [e["id"] for e in out["elements"] if not _visible(e)]
    if dropped:
        warn.append(f"hidden: {', '.join(dropped)}")
    return hard, warn


def contact_sheet(pngs, path, cell=320):
    from PIL import Image, ImageDraw
    cols = 6
    rows = math.ceil(len(pngs) / cols)
    sheet = Image.new("RGB", (cols * cell, rows * (cell + 20)), "#f1ece4")
    draw = ImageDraw.Draw(sheet)
    for i, (label, p) in enumerate(pngs):
        im = Image.open(p).convert("RGB")
        im.thumbnail((cell - 12, cell - 12))
        cx, cy = (i % cols) * cell, (i // cols) * (cell + 20)
        sheet.paste(im, (cx + (cell - im.width) // 2, cy + (cell - im.height) // 2))
        draw.text((cx + 6, cy + cell + 2), label, fill="#333")
    sheet.save(path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", help="comma-separated catalogue keys or WxH")
    ap.add_argument("--no-render", action="store_true")
    args = ap.parse_args()

    sizes = CATALOGUE
    if args.sizes:
        sizes = {}
        for s in args.sizes.split(","):
            if "x" in s and s.replace("x", "").isdigit():
                w, h = s.split("x")
                sizes[s] = (int(w), int(h))
            else:
                sizes[s] = CATALOGUE[s]

    render = None
    if not args.no_render:
        try:
            from renderer.converter.convert import render_design_to_png as render
        except ImportError as exc:
            print(f"renderer unavailable ({exc}); running checks only")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    n_hard = n_warn = 0
    for task_path in sorted(INPUT_DIR.glob("*.json")):
        task = json.loads(task_path.read_text())
        source = task["source"]
        pngs = []
        print(f"\n== {task_path.stem}  ({source['canvas']['width']}x{source['canvas']['height']}, "
              f"{len(source['elements'])} elements)")
        for name, (w, h) in sizes.items():
            target = {"width": w, "height": h}
            out = resize(source, target)
            hard, warn = check(source, target, out)
            n_hard += len(hard)
            n_warn += len([x for x in warn if not x.startswith("hidden")])
            tag = "FAIL" if hard else ("warn" if any(not x.startswith("hidden") for x in warn) else "ok")
            print(f"  {name:16s} {w:>4}x{h:<4} {tag}")
            for msg in hard:
                print(f"      HARD  {msg}")
            for msg in warn:
                print(f"      ...   {msg}")
            stem = f"{task_path.stem}__{name}"
            (OUT_DIR / f"{stem}.json").write_text(json.dumps(out, indent=2))
            if render:
                png = OUT_DIR / f"{stem}.png"
                render(out, ASSETS_DIR, str(png))
                pngs.append((f"{name} {w}x{h}", png))
        if pngs:
            contact_sheet(pngs, OUT_DIR / f"_sheet_{task_path.stem}.png")
    print(f"\nhard failures: {n_hard}   quality warnings: {n_warn}   (renders in {OUT_DIR}/)")
    return 1 if n_hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
