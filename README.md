# Chameleon — Adobe University Hackathon 2026

Resize engine for the Adobe **Chameleon Challenge**: take a design + a target canvas size, return the same design re-laid-out for the new size.

The only file we implement is **`engine/resize.py`**. Everything else (input loading, rendering, Docker) is starter plumbing.

## Setup (first time after cloning)

Needs Python 3.10+ and git.

```bash
./setup.sh
source .venv/bin/activate
```

(`setup.sh` makes a `.venv` and runs `pip install -r requirements.txt`, which also pulls in `cr-renderer` from GitHub.)

## Run

```bash
python -m engine.main
```

Reads every task JSON in `input/`, writes `output/<task>_output.json` and `output/<task>_output.png`.

## Run in Docker (how scoring actually runs it)

```bash
docker build --platform linux/amd64 -t chameleon-engine .
docker run --rm --network none \
  -v "$(pwd)/input:/app/input" -v "$(pwd)/output:/app/output" \
  chameleon-engine
```

## Approach (`engine/resize.py`)

The engine **re-lays-out** the design rather than scaling it:

1. **Analyse.** Each element gets a role inferred from geometry alone (no semantic labels are given):
   - **background**: covers more than 85% of the canvas; becomes full-bleed `cover`
   - **decoration**: faint or off-canvas free shapes; stay pinned to their corner
   - **logo** and **hero image**
   - **headline / body / legal** text, by font size and position
   - **button / badge / ribbon**, from `group_id` or a shape that contains a single label
   - **rows** of similar badges or gallery images
   - **captioned images**, paired automatically
   - **panels**: solid shapes behind several elements
   - **missing assets**
2. **Lay out.** It tries three layout families:
   - **vertical stack**: logo pinned top, legal pinned bottom, the rest centred
   - **two columns**: hero on one side, text stack on the other
   - **horizontal banner row**: logo | hero | text column | CTA

   Each family binary-searches the largest type scale that fits. Text is measured with the real brand fonts, using the same greedy wrap and skia metrics as the renderer, so line counts match the PNG. Button labels are re-fitted so they stay on one line and stay vertically centred on their shape.
3. **Choose.** Every candidate is scored on type scale kept, hero size kept, dropped content (weighted by priority) and an aspect-ratio prior. The best one wins.
4. **Degrade gracefully.** When a canvas is too small for everything (e.g. a 17-element poster going to 728×90), the lowest-priority units are hidden: legal, then badges/ribbon, then body text. Headline and CTA are never hidden. A hidden element keeps its id and an in-canvas, in-proportion box but becomes transparent (`opacity: 0`; text colour alpha is also 0, because cr-renderer ignores `opacity` on text). Missing assets are hidden the same way instead of rendering a grey placeholder.
5. **Finish.** Panels are wrapped around their new contents. Everything is then clamped inside the canvas, including the rotated bounds of rotated elements.

Hard rules kept: same ids and count, `content` / `font_family` / `asset_id` never change, images keep their aspect ratio (only full-bleed backgrounds crop), nothing goes off-canvas, no pixels are generated.

## Testing

```bash
python -m engine.main             # the 3 official samples -> output/
python -m tools.stress_test       # every sample x 18 real-world sizes -> output/stress/
python -m tools.stress_test --sizes story,leaderboard,1200x300   # pick sizes
```

The stress test checks the hard rules (ids, unchanged fields, on-canvas, aspect ratio). It also flags text collisions and fonts under 9px, lists hidden elements, and writes one contact sheet per design (`output/stress/_sheet_*.png`) so you can review every render at once. It exits with code 1 if any hard rule fails.

## Docs

- `STARTER_README.md` — original starter-kit README (full details)
- `RULES.md` — what is checked, graded, and what disqualifies
- `FAQ.md` — input/output format questions
- `chameleon_design_schema_v0.9.json` — field-by-field schema
