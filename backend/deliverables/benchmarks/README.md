# Benchmark Deck Library

Frozen reference decks for the three deliverable types, generated from the
**Payment Modernization** workspace after Phase 5 production polish. Each type has:

- `<type>.pptx` — the rendered benchmark deck
- `<type>_deckspec.json` — the exact deck spec that produced it
- `<type>_scorecard.json` — dimension scores + objective metrics
- `index.json` — summary + the regression gate + generation metadata

## Current benchmark scores

| Type | Average | Content slides | Insight titles | Dividers (label) | Max layout run | Ending = action | Leaks |
|------|---------|----------------|----------------|------------------|----------------|-----------------|-------|
| Executive Summary | 9.17 | 6 | 83% | 3 (0) | 1 | ✓ | 0 |
| Client 101 | 8.88 | 12 | 92% | 2 (0) | 2 | ✓ | 0 |
| Client 201 | 8.87 | 15 | 73% | 5 (0) | 2 | ✓ | 0 |

## Regression gate

Any future change must answer: **"Is the new deck better than this benchmark?"**

A change ships only if, for each type, the regenerated deck:

1. Matches or beats the dimension scores in the scorecard, AND
2. Preserves the hard invariants: `leaks == 0`, one sources slide, `label_dividers == 0`,
   `max_consecutive_layout <= 2`, and an action/decision closing slide.

If a change regresses any invariant or drops a dimension, it is not an improvement.
Regenerate with the same workspace and compare against these files before merging.
