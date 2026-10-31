# Chameleon: resize a design without wrecking it

Adobe University Hackathon 2026, Chameleon Challenge, stage one submission.

Give this engine a finished design and a new canvas size. It hands back the same design, laid out again for the new shape. Nothing is stretched, nothing is cropped away. The logo, product art, colours, fonts and copy are all untouched. What changes is where things sit, how big they are, and how the text wraps.

![One square design adapted to 18 canvas sizes](docs/results/level2_all_sizes.png)

*The Level 2 sample, a 1080×1080 square, adapted to 18 real formats: Instagram, stories, YouTube thumbnails, web banners, posters, all the way down to a 728×90 leaderboard.*

---

## Running it

You need Python 3.10 or newer and git. There is no model to download and the engine never touches the network.

```bash
git clone https://github.com/PrishaTHE-PRO/AdobeHackathon.git
cd AdobeHackathon
./setup.sh                    # makes a .venv and installs requirements.txt
source .venv/bin/activate
python -m engine.main
```

That last command picks up every task file in `input/` and, for each one, writes the resized design to `output/<task>_output.json` and a real render to `output/<task>_output.png`. The render uses the actual brand fonts and assets through cr-renderer, so what you see is what the judges' renderer will draw.

To try your own design, drop a task JSON into `input/` in the same shape as the three samples and run the command again.

On Linux, cr-renderer needs two system libraries that macOS already has: `sudo apt-get install libegl1 libgl1`. The Docker image includes them.

### Docker

Stage one is scored inside a container with networking turned off. This is the exact setup:

```bash
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none \
  -v "$(pwd)/input:/app/input" -v "$(pwd)/output:/app/output" \
  chameleon-engine
```

### Tests

```bash
python -m unittest discover -s tests -v    # regression suite, about 15 seconds
python -m tools.stress_test                # every sample at 18 sizes, contact sheets in output/stress/
python -m tools.stress_test --sizes story,1200x300
```

---

## What's in the box

The whole engine is one file, `engine/resize.py`. It's plain Python with no machine learning. It uses skia (installed with cr-renderer) or Pillow to measure text in the real brand fonts, and falls back to a character-width estimate if neither is around. A resize takes about 10 to 30 milliseconds on a laptop, and the same input always produces the same output.

Everything else is either the starter kit, unchanged, or our test tooling:

```
engine/resize.py        the engine
engine/main.py          starter orchestrator, unchanged
tools/stress_test.py    runs every design at 18 sizes and builds contact sheets
tests/test_resize.py    regression suite
docs/results/           the renders shown in this README
renderer/  brand_kit/   starter renderer and brand assets, unchanged
input/  output/         task files go in, results come out
```

---

## How it thinks

The brief says a bad resizer stretches the photograph and a good one moves the furniture. That's what we built. The engine runs in four steps.

**1. Work out what each element is.** The input has no labels. There is nothing that says "this is the headline" or "this is the logo". So the engine infers roles from geometry, stacking order, grouping and font size. An image covering most of the canvas is the background. A small, wide image near the top is the logo. The largest remaining image is the hero. The biggest text is the headline, mid-size text is body copy, and tiny text near the bottom is legal. A shape that contains exactly one text label is a button. A rotated group is a ribbon. Faint or off-canvas shapes are decoration.

**2. Build several candidate layouts.** Depending on the target shape, the engine tries a few compositions. A vertical stack for portrait and square canvases, with the logo pinned to the top and legal text to the bottom. Two columns for landscape, with the hero on one side and the text on the other. A single banner row for very wide canvases: logo, hero, text, button. If the target has the same shape as the source, it also tries simply keeping the designer's original composition. For each candidate it searches for the largest type size that still fits. In the stacked layouts, the engine also looks at the background image to find its focal point, such as the glow in the Solstice gradient, and centres the design's focal element on it: the hero image when there is one, otherwise the whole text block of headline, body and button. The logo stays anchored at the top and legal text at the bottom, so the block is balanced against them as well. This only reads pixels, never changes them, and it switches itself off when the background has no clear focal point or when a wide canvas crops the glow to the top edge.

**3. Score them and pick one.** A layout earns points for keeping text and the hero image large, and loses points for hiding elements or producing cramped columns of text. The best one wins.

**4. Tidy up.** The background goes full bleed. Panels re-wrap around whatever ended up inside them. Decorations get placed last, in whatever free space is left, so they don't cover text or buttons. Every element is clamped inside the canvas, including the rotated bounds of rotated elements.

A few principles sit behind all of that:

- **The brand never changes.** Colours, fonts, copy, logo and product art pass through exactly as they arrived. Only font size adapts, and all text scales by one shared factor so the headline stays the biggest thing.
- **Things that belong together move together.** A button and its label, a badge, a ribbon, an image and its caption. Rows of badges stay rows.
- **Readable beats complete.** Body text never drops below 10px and headlines never below 12px. On big posters those floors rise to 15 and 18. If a canvas is genuinely too small for everything, the engine hides the least important things first: legal text, then badges and ribbons, then the subhead. The headline and the call to action are never hidden.
- **Hidden means invisible, not deleted.** Hidden elements keep their id and a valid in-canvas box, with opacity set to zero. The output always has exactly the same elements as the input.

---

## How well it works

**The three official samples.**

Level 1 (1080×1080 to 1080×1350) becomes a vertical stack with the logo on top and the headline and button centred. Level 2 (square to 1600×400) becomes a banner row with everything kept, including the two-colour headline and the "NEW FLAVOR" badge. Level 3 (1080×1350 to 728×90) is the hard one: 17 elements into a strip 90 pixels tall. The engine keeps the logo, bottle, headline and button, and hides the subhead, badges, ribbon and legal text because none of them could be legible at that height. Renders are in `docs/results/`.

![Level 3 at 728×90](docs/results/official_level3_advanced.png)

**Three designs at 18 real sizes.** The stress test runs every sample through the formats a marketing team actually uses, from Instagram posts to A4 posters to a 320×50 mobile banner. All 54 outputs pass every hard rule. One produces a quality warning: Level 3 at 320×50, where fitting a 150-character headline into 50 pixels forces 7px type. We'd rather report that honestly than overlap text. Contact sheets for [Level 1](docs/results/level1_all_sizes.png), [Level 2](docs/results/level2_all_sizes.png) and [Level 3](docs/results/level3_all_sizes.png).

**The regression suite.** Each sample also runs as eight harder variants: a much longer headline, a longer subhead, four extra badges, twelve extra text boxes, a missing image, only one image, no button, and no group ids. Across eight formats that's 216 combinations, and every output must keep the same ids in the same order, leave protected fields alone, stay on canvas, keep image proportions, keep button labels inside their buttons, and be identical on every run. Separate tests inject crashes into the engine, remove the fonts, run from a different folder and block the network. We also checked the suite actually catches broken output, not just that it passes.

**Designs it was never tuned on.** We fuzzed it with left-aligned layouts, card panels, captioned galleries, text-only designs, a lone image, and random designs with rotated or grouped elements and missing assets. No crashes, no rule failures.

---

## The rules, and where we stand

Every source element id appears exactly once in the output. No asset pixels are generated or edited; they are only moved and resized. Non-background images keep their aspect ratio, and only full-bleed backgrounds are allowed to crop. Text content and font family are unchanged. Nothing sits off canvas. The intentionally missing watermark asset in the Level 3 sample is hidden cleanly rather than drawn as a grey box. The engine runs with `--network none`.

---

## What we'd do next

The provided logo PNG is about 60 percent transparent padding, so the logo can look smaller than the space it was given. Measuring the visible pixels instead of the file bounds would fix that. At extreme sizes like 320×50, something has to be hidden, and a finale add-on could let the user choose what survives or pick between a few layouts. Role detection is heuristic, so a design with several equally large headlines could be ranked in a surprising order; a small vision model could help there. Rewriting copy or swapping fonts is out of scope for stage one by the rules, but it's the obvious next lever.

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

We submitted this one and merged the best idea from each of the others. From Saanvi's branch came free-space placement for decorations, which cut the number of sizes where a decoration covers text from 16 to 1. From Aditi's came the regression suite and the idea of a minimum font size that grows with the canvas. Her harder test variants also found a real bug in our fallback path, now fixed. Both full engines are still on their branches.
