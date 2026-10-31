# Chameleon: resize a design without wrecking it

Adobe University Hackathon 2026, Chameleon Challenge, stage one submission.

Give this engine a finished design and a new canvas size, and it hands back the same design laid out again for the new shape. Nothing is stretched and nothing is cropped away. The logo, product art, colours, fonts and copy are untouched. What changes is where things sit, how big they are, and how the text wraps.

![One square design adapted to 18 canvas sizes](docs/results/level2_all_sizes.png)

*The Level 2 sample, a 1080×1080 square, adapted to 18 real formats, from Instagram posts and stories down to a 728×90 leaderboard.*

---

## Running it

You need Python 3.10 or newer and git. There's no model to download and the engine never touches the network.

```bash
git clone https://github.com/PrishaTHE-PRO/AdobeHackathon.git
cd AdobeHackathon
./setup.sh
source .venv/bin/activate
python -m engine.main
```

That last command picks up every task in `input/` and writes the resized design to `output/<task>_output.json` plus a real render to `output/<task>_output.png`. The render goes through cr-renderer with the actual brand fonts and assets, so what you see is what the judges' renderer will draw. To try your own design, drop a task JSON into `input/` in the same shape as the samples and run it again.

Stage one is scored inside a container with networking off. This is the exact setup:

```bash
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none \
  -v "$(pwd)/input:/app/input" -v "$(pwd)/output:/app/output" \
  chameleon-engine
```

On Linux, cr-renderer needs two system libraries that macOS already has: `sudo apt-get install libegl1 libgl1`. The Docker image includes them.

To run the tests, `python -m unittest discover -s tests -v` runs the regression suite in about fifteen seconds, and `python -m tools.stress_test` renders every sample at 18 sizes and drops contact sheets into `output/stress/`.

---

## What's in the box

The whole engine is one file, `engine/resize.py`. It's plain Python with no machine learning. It uses skia or Pillow to measure text in the real brand fonts and falls back to a character-width estimate if neither is around. A resize takes about 10 to 30 milliseconds, and the same input always gives the same output.

```
engine/resize.py        the engine
engine/main.py          starter orchestrator, unchanged
tools/stress_test.py    runs every design at 18 sizes and builds contact sheets
tests/test_resize.py    regression suite
docs/results/           the renders in this README
renderer/  brand_kit/   starter renderer and brand assets, unchanged
input/  output/         task files go in, results come out
```

---

## How it thinks

The brief puts it well: a bad resizer stretches the photograph, a good one moves the furniture. So that's what I built. Here's the path a design takes through the engine.

**First, it works out what everything is.** The input has no labels. Nothing tells you which element is the headline or the logo, just type, position and size. So the engine infers roles from geometry. An image covering most of the canvas is the background. A small, wide image near the top is the logo. The largest image left over is the hero. The biggest text is the headline, mid-size text is body copy, and tiny text near the bottom is legal. A shape with exactly one text label inside it is a button. A rotated group is a ribbon. Faint shapes hanging off the edge are decoration. Groups the designer made stay together, and if the design has a row of badges, that row stays a row.

**Then it tries a few layouts and picks the best.** For portrait and square targets it builds a vertical stack, logo pinned to the top, legal text to the bottom, everything else in between. For landscape it tries two columns, hero on one side and text on the other. For very wide banners it lays everything out in one row: logo, hero, text, button. If the target is the same shape as the source, it also tries keeping the designer's composition as-is. Each candidate searches for the largest type size that still fits, then gets scored on how much text size and hero size it kept, minus penalties for anything hidden or cramped. The winner is what you get.

**It reads the background to decide where the eye should land.** The Solstice background has a warm glow a little above centre. If text just gets centred in whatever space is free, it drifts away from that glow and the design stops feeling composed. So in stacked layouts the engine looks at the background image, finds its brightest smooth region, works out where that lands after the cover crop, and centres the focal element on it: the hero image if there is one, otherwise the whole text block. The logo keeps its place at the top so the text is balanced against it too. This only reads pixels, it never changes them. It also knows when to leave things alone. On a wide banner the glow is cropped up to the top edge or off the canvas entirely, so there's nothing to aim at and the rule switches off. The same happens if a background is flat or has no clear focal point.

**Buttons get proper padding.** When a button is scaled up with the rest of the design, the pill around the label tends to balloon, because many source designs leave far more air around a label than a real button needs. The engine caps the padding at about 1.75 em on the sides and 0.65 em above and below the text, which is roughly what Bootstrap and Material use. The label itself keeps its size, only the pill tightens. Label boxes also get a little headroom over the measured glyph width, which fixed a subtle bug where a one-line label was measured as two.

**Readable beats complete.** Body text never drops below 10 px and headlines never below 12 px, and on big posters those floors rise to 15 and 18. All text scales by one shared factor so the headline stays the most dominant thing. If a canvas is genuinely too small for everything, like 17 elements into a 728×90 strip, the engine hides the least important things first: legal text, then badges and ribbons, then the subhead. The headline and the call to action are never hidden. Hidden elements keep their id and a valid box on the canvas with opacity set to zero, so the output always has exactly the elements the input had.

**Finishing touches.** The background goes full bleed. Panels re-wrap around whatever ended up inside them. Decorations are placed last, in whatever free space is left, so they don't sit on top of text or buttons. Every element is clamped inside the canvas, including the rotated bounds of ribbons. An asset that's missing from the brand kit is hidden cleanly instead of showing up as a grey box. And if every layout somehow fails, which never happened in testing, a letterbox fallback guarantees a valid, undistorted output.

---

## How well it works

On the three official samples: Level 1 (1080×1080 to 1080×1350) becomes a vertical stack with the text block centred on the glow. Level 2 (square to 1600×400) becomes a single banner row with everything kept, including the two-colour headline and the "NEW FLAVOR" badge. Level 3 (1080×1350 to 728×90) is the hard one, 17 elements into a strip 90 pixels tall. The engine keeps the logo, bottle, headline and button, and hides the rest because none of it could be legible at that height.

![Level 3 at 728×90](docs/results/official_level3_advanced.png)

The stress test runs all three designs through 18 real formats, from Instagram posts to A4 posters to a 320×50 mobile banner. All 54 outputs pass every hard rule. One produces a quality warning, Level 3 at 320×50, where a 150-character headline has to drop to 7 px type. I'd rather report that honestly than overlap text. Contact sheets for [Level 1](docs/results/level1_all_sizes.png), [Level 2](docs/results/level2_all_sizes.png) and [Level 3](docs/results/level3_all_sizes.png).

The regression suite goes further. Each sample also runs as eight harder variants (a much longer headline, a longer subhead, extra badges, twelve extra text boxes, a missing image, a single image, no button, no group ids) across eight formats, 216 combinations in all. Every output has to keep the same ids in the same order, leave protected fields alone, stay on canvas, keep image proportions, keep button labels inside their buttons, and come out identical on every run. Other tests inject crashes into the engine, remove the fonts, run from a different folder and block the network. I also fuzzed it with designs it was never tuned on, including card panels, captioned galleries, text-only layouts and random designs with rotated or grouped elements. No crashes, no rule failures.

---

## Where it stands on the rules

Every source element id appears exactly once in the output. No asset pixels are generated or edited, they're only moved and resized. Non-background images keep their aspect ratio, and only full-bleed backgrounds are allowed to crop. Text content and font family are unchanged. Nothing sits off canvas. The intentionally missing watermark in the Level 3 sample is hidden rather than drawn as a placeholder. The engine runs with `--network none`.

---

## What I'd do next

The provided logo PNG is about 60 percent transparent padding, so the logo can look smaller than the space it was given. Measuring the visible pixels rather than the file bounds would fix that. At extreme sizes like 320×50 something has to be hidden, and a finale add-on could let the user choose what survives or pick between a few layouts. Role detection is heuristic, so a design with several equally large headlines could be ranked in a surprising order, and a small vision model could help there. Rewriting copy or swapping fonts is out of scope for stage one by the rules, but it's the obvious next lever.

---

## How this came together

Three of us each built an engine on our own branch, then ran all three through the same stress test.

| | this engine | `saanvi` branch | `aditi` branch |
|---|---|---|---|
| Hard-rule failures | 0 | 0 | 0 |
| Text overlapping other things | 0 | 0 | 10 |
| Text below 9px | 2 | 60 | 81 |
| Missing asset | hidden | grey box | grey box |
| Time per resize | about 15 ms | about 1.5 s | about 40 ms |

This one was submitted, with the best idea from each of the others merged in. Saanvi's branch contributed free-space placement for decorations, which cut the number of sizes where a decoration covers text from 16 to 1. Aditi's contributed the regression suite and a minimum font size that grows with the canvas, and her harder test variants found a real bug in the fallback path that's now fixed. Both full engines are still on their branches.
