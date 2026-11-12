"""The committed capability catalog must match a fresh render (drift guard).

Same contract as the gateway command parity doc: the generator is the source of
truth, the committed markdown is a build artifact — a new command/tool that lands
without regenerating fails here with the fix in the message.
"""

from pathlib import Path

import scripts.gen_capability_catalog as gen


def test_committed_catalog_matches_fresh_render():
    committed = Path(gen.OUT).read_text(encoding="utf-8")
    fresh = gen.render()
    assert committed == fresh, (
        "capability-catalog.md is stale — regenerate with "
        "`scripts/run-in-hermes-env python3 scripts/gen_capability_catalog.py`"
    )
