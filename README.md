# Chameleon: resize a design without wrecking it

Adobe University Hackathon 2026 · Chameleon Challenge · Stage one

Give the engine a finished design and a new canvas size. It returns the **same design laid out again for the new shape**. Nothing is stretched or cropped away. Logo, product art, colours, fonts and copy are untouched. Only positions, sizes and text wrapping change.

![One square design adapted to 18 canvas sizes](docs/results/level2_all_sizes.png)

*The Level 2 sample adapted to 18 real formats. Instagram posts and stories down to a 728×90 leaderboard.*

---

## Running it

You need **Python 3.10+** and git. No model to download. No network at run time.

```bash
git clone https://github.com/PrishaTHE-PRO/AdobeHackathon.git && cd AdobeHackathon
./setup.sh && source .venv/bin/activate
python -m engine.main
```

Every task in `input/` becomes `output/<task>_output.json` and a real render at `output/<task>_output.png`. The render uses cr-renderer with the actual brand fonts and assets so **what you see is what the judges' renderer draws**. Drop your own task JSON into `input/` to try another design.

Stage one is scored inside a container with networking off:

```bash
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none -v "$(pwd)/input:/app/input" -v "$(pwd)/output:/app/output" chameleon-engine
```

Tests: `python -m unittest discover -s tests -v` runs the regression suite in about fifteen seconds. `python -m tools.stress_test` renders every sample at 18 sizes and writes contact sheets to `output/stress/`. On Linux install `libegl1` and `libgl1` first. The Docker image already has them.

The whole engine is **one file: `engine/resize.py`**. Plain Python. No machine learning. It measures text in the real brand fonts through skia or Pillow and falls back to a width estimate if neither is present. A resize takes **10 to 30 milliseconds** and the same input always gives the same output. Everything else in the repo is either the untouched starter kit (`engine/main.py`, `renderer/`, `brand_kit/`) or our tooling: `tools/stress_test.py` for the multi-format run and `tests/test_resize.py` for the regression suite.

---

## How it thinks

**A bad resizer stretches the photograph. A good one moves the furniture.** Here's how the engine moves it.

**It figures out what everything is.** The input has no labels, so roles come from geometry.
- Image covering most of the canvas: **background**. Small wide image near the top: **logo**. Largest image left: **hero**.
- Biggest text: **headline**. Mid-size: body. Tiny text near the bottom: legal.
- Shape holding exactly one label: **button**. Rotated group: ribbon. Faint shapes off the edge: decoration.
- Designer groups stay together. A row of badges stays a row.

**It tries several layouts and keeps the best.**
- Portrait and square: **vertical stack**, logo pinned top, legal pinned bottom.
- Landscape: **two columns**, hero on one side, text on the other.
- Wide banners: **one row** of logo, hero, text, button.
- Same shape as the source: the designer's composition as-is.
- Each candidate finds the largest type size that fits, then is scored on text and hero size kept minus penalties for anything hidden or cramped.

**It reads the background to place the text.** The Solstice background has a glow above centre. Text centred in leftover space drifts away from it.
- In stacked layouts the engine **finds the brightest smooth region of the background** and **centres the focal element on it**: the hero if there is one, otherwise the text block.
- The logo stays anchored at the top so the text is balanced against it too.
- Reads pixels only. Never changes them.
- **Switches itself off** on wide banners where the glow is cropped away, and on flat backgrounds with no focal point.

**Buttons get real padding.** Scaled-up buttons balloon because source designs leave too much air around the label.
- Padding is **capped at about 1.75 em on the sides and 0.65 em above and below**, close to Bootstrap and Material.
- The label keeps its size. Only the pill tightens.

**Readable beats complete.**
- Body text never below **10 px**, headlines never below **12 px**. On big posters the floors rise to 15 and 18.
- All text scales by one factor so the headline stays dominant.
- When a canvas is too small, the engine hides legal first, then badges and ribbons, then the subhead. **The headline and call to action are never hidden.**
- Hidden elements keep their id and an on-canvas box at opacity zero, so the output always has exactly the input's elements.

**Finishing pass.** Background goes full bleed. Panels re-wrap around their contents. Decorations land in free space so they never sit on text or buttons. Everything is clamped inside the canvas, ribbons included. A missing asset is hidden instead of drawn as a grey box. If every layout fails, a letterbox fallback still returns a valid undistorted output.

---

## How well it works

**Level 1** (1080×1080 to 1080×1350) becomes a vertical stack with the text centred on the glow. **Level 2** (square to 1600×400) becomes one banner row with everything kept, two-colour headline and "NEW FLAVOR" badge included. **Level 3** (1080×1350 to 728×90) is the hard one: 17 elements into a 90 px strip. The engine keeps logo, bottle, headline and button and hides what couldn't be legible at that height.

![Level 3 at 728×90](docs/results/official_level3_advanced.png)

The stress test pushes all three designs through 18 real formats, Instagram post to A4 poster to 320×50 mobile banner. **All 54 outputs pass every hard rule.** One quality warning: Level 3 at 320×50, where a 150-character headline has to drop to 7 px. I'd rather report that than overlap text. Contact sheets: [Level 1](docs/results/level1_all_sizes.png) · [Level 2](docs/results/level2_all_sizes.png) · [Level 3](docs/results/level3_all_sizes.png).

The regression suite runs each sample as eight harder variants (longer headline, longer subhead, extra badges, twelve extra text boxes, missing image, single image, no button, no group ids) across eight formats. **216 combinations.** Every output must keep the same ids in order, leave protected fields alone, stay on canvas, keep image proportions, keep labels inside their buttons and come out identical every run. Other tests inject crashes, remove fonts, run from another folder and block the network. I also fuzzed it with card panels, captioned galleries, text-only layouts and random rotated or grouped designs. No crashes. No rule failures.

---

## Rules

Every source id appears exactly once. No asset pixels are generated or edited. Non-background images keep their aspect ratio and only full-bleed backgrounds crop. Content and font family are unchanged. Nothing sits off canvas. The intentionally missing watermark in Level 3 is hidden rather than drawn. The engine runs with `--network none`.

---

## What I'd do next

The logo PNG is about 60 percent transparent padding so it can look smaller than its box. Measuring visible pixels would fix that. At sizes like 320×50 something has to be hidden and a finale add-on could let the user choose what survives. Role detection is heuristic, so several equally large headlines could rank in a surprising order. Rewriting copy or swapping fonts is out of scope for stage one but is the obvious next lever.

---

## How this came together

Three of us built an engine each on our own branch and ran all three through the same stress test.

| | this engine | `saanvi` branch | `aditi` branch |
|---|---|---|---|
| Hard-rule failures | 0 | 0 | 0 |
| Text overlapping other things | 0 | 0 | 10 |
| Text below 9px | 2 | 60 | 81 |
| Missing asset | hidden | grey box | grey box |
| Time per resize | about 15 ms | about 1.5 s | about 40 ms |

This one was submitted with the best idea from each merged in. **Saanvi's** free-space placement for decorations cut the sizes where a decoration covers text from 16 to 1. **Aditi's** regression suite and canvas-scaled font floor came over too, and her harder variants found a real bug in the fallback path that's now fixed. Both full engines are still on their branches.
