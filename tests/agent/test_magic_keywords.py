"""Magic keywords (oh-my-pi parity): one hidden instruction per matched keyword,
appended to the CURRENT turn's user-attributed message — never the system prompt,
never a synthetic mid-loop row (prompt-cache + alternation invariants)."""

from agent.magic_keywords import (
    MAGIC_KEYWORDS,
    apply_magic_keywords,
    matched_magic_keywords,
)


class TestMatching:
    def test_matches_whole_word_case_insensitive(self):
        assert matched_magic_keywords("please ultrathink about this") == ["ultrathink"]
        assert matched_magic_keywords("UltraThink about this") == ["ultrathink"]

    def test_no_match_inside_word(self):
        assert matched_magic_keywords("ultrathinking is not a word") == []

    def test_no_match_inside_fenced_code(self):
        text = "look:\n```\nultrathink\n```\nthat's it"
        assert matched_magic_keywords(text) == []

    def test_no_match_inside_tilde_fence(self):
        text = "~~~\nultrathink\n~~~"
        assert matched_magic_keywords(text) == []

    def test_matches_after_fence_closes(self):
        text = "```\nquoted\n```\nnow ultrathink"
        assert matched_magic_keywords(text) == ["ultrathink"]


class TestApply:
    def test_appends_notice_and_keeps_word(self):
        out = apply_magic_keywords("ultrathink this problem")
        assert out.startswith("ultrathink this problem")
        assert "[magic-keyword:ultrathink]" in out
        assert MAGIC_KEYWORDS["ultrathink"] in out

    def test_idempotent_on_resend(self):
        once = apply_magic_keywords("ultrathink this")
        twice = apply_magic_keywords(once)
        assert twice == once

    def test_passthrough_without_keyword(self):
        assert apply_magic_keywords("hello world") == "hello world"

    def test_passthrough_empty_and_non_string(self):
        assert apply_magic_keywords("") == ""
        assert apply_magic_keywords(None) is None

    def test_fenced_keyword_gets_no_notice(self):
        text = "```\nultrathink\n```"
        assert apply_magic_keywords(text) == text


class TestWiringIntoTurnContext:
    """The turn prologue applies the transform before persistence, so the
    stored user row equals the wire row (alternation/cache consistency)."""

    def test_turn_context_module_imports_the_transform(self):
        # Wiring contract: the prologue resolves the helper from this module, so
        # patching it there intercepts production (root: patch where production reads).
        import agent.turn_context as tc

        assert tc.apply_magic_keywords is apply_magic_keywords
