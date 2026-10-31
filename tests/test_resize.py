"""Offline regression suite: python -m unittest discover -s tests -v

Adapted from the `aditi` branch's suite to this engine's contract. Each
sample is run as-is and as 8 harder variants (long copy, extra badges, many
texts, a missing asset, one image, no CTA, no groups) across 8 common
formats, plus robustness checks (determinism, input immutability, injected
failures, missing fonts, working directory, no network, long tokens).
"""

from copy import deepcopy
import json
from math import cos, isfinite, radians, sin
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import engine.resize as engine

ROOT = Path(__file__).resolve().parents[1]
FORMATS = [(1080, 1920), (1280, 720), (728, 90), (820, 312),
           (1000, 1500), (300, 600), (1600, 400), (1080, 1080)]
TASKS = [json.loads(path.read_text()) for path in sorted((ROOT / "input").glob("*.json"))]

# fields the engine must never touch (font_size and geometry may change)
PROTECTED = ("id", "type", "content", "font_family", "font_weight", "font_style",
             "asset_id", "group_id", "z_index", "rotation", "shape_type",
             "text_align", "line_height", "letter_spacing", "stroke_color")


def variants(source):
    yield "original", deepcopy(source)
    for name in ("long_headline", "long_subhead", "extra_badges", "many_texts",
                 "missing_asset", "one_image", "no_cta", "no_groups"):
        result = deepcopy(source)
        texts = sorted((e for e in result["elements"] if e["type"] == "text"),
                       key=lambda e: -e["font_size"])
        if name in ("long_headline", "long_subhead"):
            text = texts[min(1 if name == "long_subhead" else 0, len(texts) - 1)]
            text["content"] = (text["content"] + " ") * 6
        elif name == "extra_badges":
            for i in range(4):
                group = f"extra_badge_{i}"
                x, y = 20 + i * 150, source["canvas"]["height"] * 0.3
                result["elements"].extend([
                    dict(id=group, type="shape", shape_type="rectangle", fill_color="rgba(90,40,20,0.5)",
                         x=x, y=y, width=140, height=40, group_id=group, z_index=3),
                    dict(id=group + "_text", type="text", content=f"BADGE {i}", font_family="Solstice Sans",
                         font_size=14, color="rgba(255,255,255,1)", x=x + 5, y=y + 5,
                         width=130, height=30, group_id=group, z_index=4),
                ])
        elif name == "many_texts":
            for i in range(12):
                text = deepcopy(texts[-1])
                text.update(id=f"small_text_{i}", content=f"Additional detail {i}", font_size=12,
                            x=20 + (i % 4) * 200, y=150 + (i // 4) * 80, width=180, height=45)
                text.pop("group_id", None)
                result["elements"].append(text)
        elif name == "missing_asset":
            next(e for e in result["elements"] if e["type"] == "image")["asset_id"] = "missing-test.png"
        elif name == "one_image":
            images = [e for e in result["elements"] if e["type"] == "image"]
            keep = images[-1]["id"]
            result["elements"] = [e for e in result["elements"] if e["type"] != "image" or e["id"] == keep]
        elif name == "no_cta":
            result["elements"] = [e for e in result["elements"] if not e["id"].startswith("cta")]
        elif name == "no_groups":
            for element in result["elements"]:
                element.pop("group_id", None)
        yield name, result


def _hidden(element):
    return (element.get("opacity", 1) or 0) <= 0


def _box(e):
    return e["x"], e["y"], e["x"] + e["width"], e["y"] + e["height"]


def _overlap(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0


class ResizeTests(unittest.TestCase):
    def assert_contract(self, source, target, output):
        self.assertEqual(output["canvas"], target)
        self.assertEqual(set(output), {"canvas", "elements"})
        self.assertEqual([e["id"] for e in output["elements"]], [e["id"] for e in source["elements"]])
        sw, sh = source["canvas"]["width"], source["canvas"]["height"]
        width, height = target["width"], target["height"]
        for original, result in zip(source["elements"], output["elements"]):
            eid = original["id"]
            for key in PROTECTED:
                self.assertEqual(original.get(key), result.get(key), (eid, key))
            if not _hidden(result):  # hidden text also gets a transparent colour
                for key in ("color", "fill_color", "style_runs"):
                    self.assertEqual(original.get(key), result.get(key), (eid, key))
            for key in ("x", "y", "width", "height"):
                self.assertTrue(isfinite(result[key]) and result[key] >= -1e-6, (eid, key))
            self.assertLessEqual(result["x"] + result["width"], width + 1e-6, eid)
            self.assertLessEqual(result["y"] + result["height"], height + 1e-6, eid)
            angle = radians(result.get("rotation", 0) or 0)
            rw = abs(cos(angle)) * result["width"] + abs(sin(angle)) * result["height"]
            rh = abs(sin(angle)) * result["width"] + abs(cos(angle)) * result["height"]
            cx, cy = result["x"] + result["width"] / 2, result["y"] + result["height"] / 2
            self.assertGreaterEqual(cx - rw / 2, -0.5, eid)
            self.assertGreaterEqual(cy - rh / 2, -0.5, eid)
            self.assertLessEqual(cx + rw / 2, width + 0.5, eid)
            self.assertLessEqual(cy + rh / 2, height + 0.5, eid)
            if original["type"] == "image":
                covered = (max(0, min(sw, original["x"] + original["width"]) - max(0, original["x"])) *
                           max(0, min(sh, original["y"] + original["height"]) - max(0, original["y"])))
                background = covered >= 0.85 * sw * sh
                if not background and original["width"] and original["height"]:
                    self.assertAlmostEqual(result["width"] / result["height"],
                                           original["width"] / original["height"], delta=0.05 * original["width"] / original["height"])
                if not background:
                    self.assertEqual(original.get("fit"), result.get("fit"), eid)
            if original["type"] == "text":
                self.assertTrue(isfinite(result["font_size"]) and result["font_size"] >= 1, eid)

    def assert_labels_on_shapes(self, source, output):
        """Grouped labels stay inside their (possibly widened) button shape."""
        boxes = {e["id"]: e for e in output["elements"]}
        groups = {}
        for e in source["elements"]:
            if e.get("group_id"):
                groups.setdefault(e["group_id"], []).append(e)
        for members in groups.values():
            shapes = [m for m in members if m["type"] == "shape"]
            if not shapes or any(m.get("rotation") for m in members):
                continue
            base = max(shapes, key=lambda m: m["width"] * m["height"])
            b = boxes[base["id"]]
            for m in members:
                if m["type"] != "text" or _hidden(boxes[m["id"]]):
                    continue
                t = boxes[m["id"]]
                self.assertGreaterEqual(t["x"], b["x"] - 1, m["id"])
                self.assertGreaterEqual(t["y"], b["y"] - 1, m["id"])
                self.assertLessEqual(t["x"] + t["width"], b["x"] + b["width"] + 1, m["id"])
                self.assertLessEqual(t["y"] + t["height"], b["y"] + b["height"] + 1, m["id"])

    def test_common_formats_and_variants(self):
        cases = 0
        for task in TASKS:
            for name, source in variants(task["source"]):
                before = deepcopy(source)
                for width, height in FORMATS:
                    with self.subTest(task=task["task_id"], variant=name, size=(width, height)):
                        target = {"width": width, "height": height}
                        output = engine.resize(source, target)
                        self.assert_contract(source, target, output)
                        self.assert_labels_on_shapes(source, output)
                        self.assertEqual(source, before)  # input never mutated
                        self.assertEqual(output, engine.resize(source, target))  # deterministic
                        cases += 1
        print(f"Validated {cases} sample/variant/format combinations.", flush=True)

    def test_official_samples_are_readable_and_collision_free(self):
        for task in TASKS:
            source, target = task["source"], task["target_canvas"]
            output = engine.resize(source, target)
            visible = [e for e in output["elements"] if not _hidden(e)]
            for e in visible:
                if e["type"] == "text":
                    self.assertGreaterEqual(e["font_size"], engine.MIN_FONT, (task["task_id"], e["id"]))
            texts = [e for e in visible if e["type"] == "text"]
            content = [e for e in visible if e["type"] == "image"
                       and _overlap(_box(e), (0, 0, target["width"], target["height"]))
                       < 0.85 * target["width"] * target["height"]]
            for t in texts:  # text never sits on a picture
                for img in content:
                    self.assertEqual(_overlap(_box(t), _box(img)), 0, (task["task_id"], t["id"], img["id"]))

    def test_banner_reads_left_to_right(self):
        task = next(t for t in TASKS if t["task_id"] == "core_01")
        boxes = {e["id"]: e for e in engine.resize(task["source"], task["target_canvas"])["elements"]}
        self.assertLess(boxes["logo"]["x"], boxes["product"]["x"])
        self.assertLess(boxes["product"]["x"], boxes["headline"]["x"])
        self.assertLess(boxes["headline"]["x"], boxes["cta_bg"]["x"])

    def test_same_shape_keeps_original_composition(self):
        task = next(t for t in TASKS if t["task_id"] == "warmup_01")
        source = task["source"]
        output = engine.resize(source, {"width": 2160, "height": 2160})
        for old, new in zip(source["elements"], output["elements"]):
            self.assertAlmostEqual(new["x"], old["x"] * 2, delta=1, msg=old["id"])
            self.assertAlmostEqual(new["y"], old["y"] * 2, delta=1, msg=old["id"])

    def test_missing_asset_is_hidden_not_placeholdered(self):
        task = next(t for t in TASKS if t["task_id"] == "advanced_01")
        for width, height in FORMATS:
            out = engine.resize(task["source"], {"width": width, "height": height})
            mark = next(e for e in out["elements"] if e["id"] == "holiday_watermark")
            self.assertEqual(mark["opacity"], 0)

    def test_failures_use_safe_fallback(self):
        for task in TASKS:
            for target in (task["target_canvas"], {"width": 300, "height": 600}):
                with self.subTest(task=task["task_id"], target=target):
                    with patch.object(engine.Engine, "run", side_effect=RuntimeError("injected failure")):
                        output = engine.resize(task["source"], target)
                    self.assert_contract(task["source"], target, output)

    def test_missing_fonts_and_independent_working_directory(self):
        source = deepcopy(TASKS[0]["source"])
        for element in source["elements"]:
            if element["type"] == "text":
                element["font_family"] = "Unavailable Test Font"
        target = TASKS[0]["target_canvas"]
        self.assert_contract(source, target, engine.resize(source, target))
        previous = os.getcwd()
        try:
            with TemporaryDirectory() as directory:
                os.chdir(directory)
                original = TASKS[2]["source"]
                out = engine.resize(original, TASKS[2]["target_canvas"])
                self.assert_contract(original, TASKS[2]["target_canvas"], out)
                # assets still resolve from the repo, so the missing one is still hidden
                mark = next(e for e in out["elements"] if e["id"] == "holiday_watermark")
                self.assertEqual(mark["opacity"], 0)
        finally:
            os.chdir(previous)

    def test_offline_execution(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network access")):
            for task in TASKS:
                self.assert_contract(task["source"], task["target_canvas"],
                                     engine.resize(task["source"], task["target_canvas"]))

    def test_long_tokens_newlines_and_tight_leading(self):
        source = deepcopy(TASKS[0]["source"])
        headline = next(e for e in source["elements"] if e["id"] == "headline")
        headline.update(content="Supercalifragilisticexpialidocious\nA second line", letter_spacing=1, line_height=1.05)
        target = {"width": 300, "height": 600}
        output = engine.resize(source, target)
        self.assert_contract(source, target, output)
        out = next(e for e in output["elements"] if e["id"] == "headline")
        longest = engine._longest_word_width(out, int(out["font_size"]))
        self.assertLessEqual(longest, out["width"] + 1e-6)


if __name__ == "__main__":
    unittest.main()
