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

## Docs

- `STARTER_README.md` — original starter-kit README (full details)
- `RULES.md` — what is checked, graded, and what disqualifies
- `FAQ.md` — input/output format questions
- `chameleon_design_schema_v0.9.json` — field-by-field schema
