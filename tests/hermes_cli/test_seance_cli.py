"""`hermes seance` CLI (hermes_cli/seance_cli.py): parser wiring + error mapping.

The consult core is covered in tests/agent/test_seance.py; here we pin the CLI
contract: the subcommand registers, positional question joins, SeanceError maps
to exit 1, transport failures to exit 2, and --json emits a stable shape.
"""

import argparse
import json

import pytest

from hermes_cli.seance_cli import _cmd_seance, add_parser


@pytest.fixture
def parser():
    root = argparse.ArgumentParser()
    sub = root.add_subparsers(dest="command")
    add_parser(sub)
    return root


def _args(parser, argv):
    return parser.parse_args(argv)


class TestParserWiring:
    def test_registers_seance_with_required_positionals(self, parser):
        args = _args(parser, ["seance", "sess-1", "what", "happened?"])
        assert args.command == "seance"
        assert args.session == "sess-1"
        assert args.question == ["what", "happened?"]
        assert args.max_chars == 120_000
        assert callable(args.func)


class TestCommand:
    def _patch_run(self, monkeypatch, **result):
        import agent.seance as seance_mod

        captured = {}

        def fake_run(session, question, **kwargs):
            captured.update(session=session, question=question, **kwargs)
            return seance_mod.SeanceResult(
                answer=result.get("answer", "cited [m2]"),
                session_id=session,
                transcript_chars=result.get("transcript_chars", 42),
                truncated=result.get("truncated", False),
            )

        monkeypatch.setattr(seance_mod, "run_seance", fake_run)
        return captured

    def test_prints_answer_and_joins_question(self, parser, monkeypatch, capsys):
        captured = self._patch_run(monkeypatch)
        args = _args(parser, ["seance", "sess-1", "what", "happened?"])
        assert _cmd_seance(args) == 0
        out = capsys.readouterr().out
        assert "cited [m2]" in out
        assert captured["question"] == "what happened?"
        assert captured["session"] == "sess-1"

    def test_json_emits_stable_shape(self, parser, monkeypatch, capsys):
        self._patch_run(monkeypatch, truncated=True)
        args = _args(parser, ["seance", "sess-1", "q", "--json"])
        assert _cmd_seance(args) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["session_id"] == "sess-1"
        assert payload["truncated"] is True
        assert payload["task"] == "seance"

    def test_seance_error_maps_to_exit_1(self, parser, monkeypatch, capsys):
        import agent.seance as seance_mod

        def fail(*a, **k):
            raise seance_mod.SeanceError("no-such-session: ghost")

        monkeypatch.setattr(seance_mod, "run_seance", fail)
        args = _args(parser, ["seance", "ghost", "q"])
        assert _cmd_seance(args) == 1
        assert "no-such-session" in capsys.readouterr().err

    def test_transport_error_maps_to_exit_2(self, parser, monkeypatch, capsys):
        import agent.seance as seance_mod

        def fail(*a, **k):
            raise RuntimeError("provider down")

        monkeypatch.setattr(seance_mod, "run_seance", fail)
        args = _args(parser, ["seance", "sess-1", "q"])
        assert _cmd_seance(args) == 2
        assert "consult failed" in capsys.readouterr().err

    def test_blank_question_refused(self, parser, monkeypatch, capsys):
        args = _args(parser, ["seance", "sess-1", "  "])
        assert _cmd_seance(args) == 1
        assert "question is required" in capsys.readouterr().err
