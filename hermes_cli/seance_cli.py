"""``hermes seance <session_id> <question...>`` — consult a saved session read-only.

oh-my-pi ``/seance`` parity (design port): a dedicated auxiliary model reads the
ENTIRE saved transcript and answers with ``[m<id>]`` citations while the source DB
is opened ``mode=ro`` (see :mod:`agent.seance`). Explicit ``--model``/``--provider``
pin the consult route; otherwise ``auxiliary.seance`` config / the auto aux route
applies. Exit codes: 0 success, 1 seance error, 2 LLM/transport error.
"""

from __future__ import annotations

import json
import logging
import sys

logger = logging.getLogger(__name__)


def _cmd_seance(args) -> int:
    from agent.seance import SeanceError, run_seance

    question = " ".join(args.question).strip()
    if not question:
        print("seance: a question is required", file=sys.stderr)
        return 1
    try:
        result = run_seance(
            args.session,
            question,
            provider=args.provider,
            model=args.model,
            max_chars=args.max_chars,
        )
    except SeanceError as exc:
        print(f"seance: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # provider/transport failure boundary: structured exit, logged traceback
        logger.exception("seance consult failed for session %s", args.session)
        print(f"seance: consult failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "answer": result.answer,
            "session_id": result.session_id,
            "transcript_chars": result.transcript_chars,
            "truncated": result.truncated,
            "task": result.task,
        }, ensure_ascii=False))
    else:
        print(result.answer)
        if result.truncated:
            print(
                f"— transcript truncated to {result.transcript_chars} chars "
                "(increase with --max-chars)",
                file=sys.stderr,
            )
    return 0


def add_parser(subparsers) -> None:
    """Register `hermes seance` on the given argparse subparsers object."""
    seance_parser = subparsers.add_parser(
        "seance",
        help="Consult a saved session read-only (cited answer, source never modified)",
        description=(
            "Ask a question about a saved session. A dedicated auxiliary model reads "
            "the full transcript and answers with [m<id>] citations; the session is "
            "opened strictly read-only and is never modified. Pin the consult model "
            "with --model/--provider (config: auxiliary.seance), or set a custom "
            "transcript budget with --max-chars."
        ),
    )
    seance_parser.add_argument("session", help="Session id to consult (see `hermes sessions`)")
    seance_parser.add_argument("question", nargs="+", help="Question about that session")
    seance_parser.add_argument("--model", default=None, help="Consult model override")
    seance_parser.add_argument("--provider", default=None, help="Consult provider override")
    seance_parser.add_argument(
        "--max-chars", dest="max_chars", type=int, default=120_000,
        help="Transcript character budget (default 120000)",
    )
    seance_parser.add_argument("--json", action="store_true", help="Machine-readable result")
    seance_parser.set_defaults(func=_cmd_seance)
