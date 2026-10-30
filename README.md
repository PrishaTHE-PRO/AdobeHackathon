# Chameleon — Adobe University Hackathon 2026

An offline resize engine that adapts a finished design to a new canvas. It preserves every element and its copy, fonts, colors, assets, rotation, opacity, and stacking order. It changes position, dimensions, and text font size.

The engine implementation is in `engine/resize.py`. `engine/main.py` loads tasks, runs structural checks, and renders outputs with the supplied cr-renderer integration.

## Setup

Requires Python 3.10+ and Git. From the repository root:

```bash
./setup.sh
source .venv/bin/activate
```

Alternatively:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

Dependencies are Pillow and cr-renderer (including its transitive renderer dependencies). Installation fetches cr-renderer from GitHub and requires network access. **There are no ML/LLM models, model weights, API keys, or inference services.** Fonts and image assets come from `brand_kit/`; layout and rendering run offline after installation. The engine itself uses only Pillow and the Python standard library.

## Run and inspect

Run from the repository root:

```bash
python -m engine.main
```

Each JSON file in `input/` is a task with `source` and `target_canvas` fields. The source is a design containing `canvas: {width, height}` and `elements`. The engine returns that same design structure with the target canvas and updated elements. Geometry fields are `x`, `y`, `width`, and `height`.

The runner writes `output/<task>_output.json` and `output/<task>_output.png`. On macOS:

```bash
open output/*_output.png
```

To try a different format, edit a task's `target_canvas`, for example:

```json
"target_canvas": { "width": 1280, "height": 720 }
```

Then rerun the command. An `OK` message means the runner's structural self-check passed. Inspect the PNG too: the self-check does not measure readability, cropping, visual hierarchy, or layout quality. The engine never renders images itself.

## Approach

1. **Parse and infer roles.** Read the schema fields and infer backgrounds, logos, hero artwork, headlines, secondary text, buttons, badges, and decorations from geometry, type, and typography.
2. **Build rigid groups.** Preserve explicit `group_id` units and infer labels/logos contained in bands. Decorative shapes that overlap hero images travel with that artwork. Preserve original element order and all styling fields.
3. **Choose aspect-ratio strategies.** Portrait/square canvases use a vertical stack, wide canvases use art and text columns, and banners use a main row plus secondary content. Thresholds use width/height ratios, not platform names or sample IDs.
4. **Generate candidates.** Try 24 margin/gap/proportion combinations per applicable strategy, adjacent strategies near boundaries, and the proportional anchor baseline. Empty roles consume no slots. Every input element receives an output box.
5. **Fit text.** Allocate the available box width before shrinking fonts. Measure bundled Solstice fonts with Pillow, account for explicit newlines, word wrapping, letter spacing, style runs, line height, and line caps, then binary-search font size. Keep grouped labels inside their original relative geometry. Missing fonts use a conservative width estimate.
6. **Score and select.** Penalize structural violations, new overlaps, text overflow, and unreadably small text before weaker placement, spacing, hierarchy, and alignment preferences. Original overlaps are distinguished from new collisions. Nearby formats favor preserving placement; wide formats favor using the horizontal space.
7. **Validate and fall back.** Check IDs/count, protected fields, finite on-canvas bounds including rotation, image proportions, rigid explicit groups, and font size. If a candidate is invalid, try the next. Unexpected pipeline failures use a proportional scene fallback that keeps groups intact.

Existing background images with `fit: "cover"` use a canvas-sized crop viewport; the renderer preserves the asset's proportions while cropping. Other images preserve their box aspect ratios. A background without an existing crop mode is contained because full coverage, an unchanged aspect ratio, and on-canvas geometry cannot all be guaranteed at a different ratio.

## Tests

```bash
python -m unittest discover -s tests -v
python -m engine.main
```

The regression suite checks all three samples across these formats:

| Format | Canvas |
| --- | --- |
| Story | 1080 × 1920 |
| Landscape / thumbnail | 1280 × 720 |
| Leaderboard | 728 × 90 |
| Wide banner | 820 × 312 |
| Portrait | 1000 × 1500 |
| Narrow portrait | 300 × 600 |
| Wide banner | 1600 × 400 |
| Square | 1080 × 1080 |

It also tests longer headlines/subheads, extra badges, many text boxes, missing assets, a single image, no CTA, and absent group IDs: **216 sample/variant/format combinations**. Further checks cover determinism, source immutability, rigid grouping, layered artwork, font fallback, different working directories, long tokens, validator rejection, and deliberately injected pipeline failures. These tests are local regression checks, not the official scorer.

## Docker and offline evaluation

```bash
docker build --platform linux/amd64 -t my-chameleon-engine .
docker run --rm --network none \
  -v "$(pwd)/input:/app/input" \
  -v "$(pwd)/output:/app/output" \
  my-chameleon-engine
```

Build with network access so dependencies are installed into the image. The evaluation run uses `--network none`. The Dockerfile's entrypoint is `python -m engine.main`; input/output folders are mounted at runtime. Docker is needed to verify the container locally; Python tests alone do not verify the image build.

## Limitations

- Role detection is heuristic. A small edge image may be a logo or a badge, and some button-like shapes have other meanings. Artwork pixels are not analyzed for saliency or contrast.
- Extreme banners cannot always hold all source copy legibly. Headline and CTA get the main row; secondary content remains present at smaller sizes. The canvas-scaled readability floor is a strong preference, with emergency sizes down to the schema minimum of 1 px when fitting all copy would otherwise be impossible.
- A single unbreakable token can still exceed its box at 1 px. The engine preserves the text, penalizes overflow, and returns the best structurally valid candidate. A structurally valid output does not promise legibility for arbitrarily long copy.
- Font measurement uses Pillow with conservative padding. The renderer uses Skia, so small metric differences and unsupported glyphs remain possible. `max_lines` is considered during fitting even though the supplied renderer does not itself enforce it.
- The Level 3 missing watermark asset is intentional. The engine lays out its box without opening the file; the supplied renderer decides how its placeholder appears.
- The safe fallback prioritizes preserving all elements and groups. It can be visually sparse or too small after an extreme ratio change.
- The renderer requirement currently follows its upstream Git repository. Reproducibility across fresh builds depends on that upstream revision and dependency availability.

## Repository access and feedback

Keep the GitHub repository **private**, as required by the challenge guidance. A repository owner should verify visibility in GitHub repository settings; local Git branches and these scripts do not configure repository visibility. For technical feedback, contact the Hackathon team using **Feedback** in the email subject.

## Reference documents

- `STARTER_README.md` — starter-kit instructions
- `RULES.md` — submission and evaluation rules
- `FAQ.md` — input/output format questions
- `chameleon_design_schema_v0.9.json` — exact field definitions
