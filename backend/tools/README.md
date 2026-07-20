# Validation tooling

## `visual_qa.py` — geometric + text-fit visual QA

Analyzes a rendered PPTX's object model for visual defects, separating EXACT
checks from ESTIMATES:
- **EXACT** (object geometry): off-slide shapes, content-shape collisions.
- **ESTIMATE** (font metrics, no rasterizer): text likely overflowing its own frame.

```bash
venv/bin/python tools/visual_qa.py --dir deliverables/benchmarks
venv/bin/python tools/visual_qa.py --pptx <file>.pptx --render /tmp/qa   # rasterize if LibreOffice present
```

Pixel-accurate rendering QA requires a rasterizer (LibreOffice `soffice`). Where one
is on PATH, `--render` produces PDFs/images for human inspection; otherwise the
geometry+fit analysis is the automated proxy. Current benchmark decks: 0 errors, 0 warnings.

---


## `validate_workspace.py` — real-corpus benchmark validation harness

Generates the three deliverables for a target workspace (or scores already-saved
deck specs) and checks them against the frozen benchmark library using **objective
metrics only** — no synthetic quality scoring.

### When to use
Run this against a **real 25–50 document workspace** to validate the platform before
broad rollout — the outstanding validation gate. It answers "does a real large corpus
still meet the release invariants and hold up against the 3-doc benchmarks?"

### Usage
```bash
# Generate on a real workspace and compare (real LLM calls):
venv/bin/python tools/validate_workspace.py --workspace <id>

# Score already-generated deck specs (no LLM calls):
venv/bin/python tools/validate_workspace.py --specs deliverables/output/phase3
```

### Hard invariants (a deck FAILS if any is violated)
- `leaks == 0` (no template/placeholder text)
- `empty_placeholders == 0` (no 'Click to add text' prompts / dashed borders)
- `sources == 1` (exactly one sources slide)
- `label_dividers == 0`
- `max_consecutive_layout <= 2`
- content-slide count within the type band (Exec 4–10, 101 8–18, 201 12–22)
- the deck ends on an action/decision slide

### Benchmark comparison
Objective metrics (insight-title %, theme coverage, distinct layouts, dividers) are
compared to `deliverables/benchmarks/<type>_scorecard.json` → better / same / worse.
Subjective consulting scores are intentionally NOT computed here — those require human
review of the generated decks.
