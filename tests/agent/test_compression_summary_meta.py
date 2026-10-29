"""Compaction summary metadata (DeepSeek Harness parity): record which
provider/model wrote the latest accepted summary, with its usage, so a degraded
summary can be traced to its author. Persisted per session (JSON) and readable
without a live compressor."""

from types import SimpleNamespace

from agent.context_compressor_telemetry import summary_usage_dict


class TestSummaryUsageDict:
    def test_extracts_openai_usage_fields(self):
        response = SimpleNamespace(
            usage=SimpleNamespace(prompt_tokens=1234, completion_tokens=567, total_tokens=1801)
        )
        assert summary_usage_dict(response) == {
            "prompt_tokens": 1234,
            "completion_tokens": 567,
            "total_tokens": 1801,
        }

    def test_dict_usage_works(self):
        response = {"usage": {"prompt_tokens": 10, "completion_tokens": 2}}
        assert summary_usage_dict(response) == {"prompt_tokens": 10, "completion_tokens": 2}

    def test_missing_usage_is_empty(self):
        assert summary_usage_dict(SimpleNamespace()) == {}


class TestSessionDBRoundtrip:
    def _db(self, tmp_path):
        from hermes_state import SessionDB
        return SessionDB(tmp_path / "state.db")

    def test_set_and_get_summary_meta(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.create_session("s1", "cli", model="m")
        meta = {"provider": "opencode-go", "model": "glm-5.3-flash", "max_tokens": 8000,
                "usage": {"prompt_tokens": 100}, "at": 123}
        db.set_compression_summary_meta(sid, meta)
        assert db.get_compression_summary_meta(sid) == meta

    def test_defaults_to_empty_dict(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.create_session("s2", "cli", model="m")
        assert db.get_compression_summary_meta(sid) == {}
        assert db.get_compression_summary_meta("") == {}

    def test_malformed_json_reads_as_empty(self, tmp_path):
        db = self._db(tmp_path)
        sid = db.create_session("s3", "cli", model="m")
        db.set_compression_summary_meta(sid, {"x": 1})
        db._write_session_column("compression_summary_meta", sid, "{not json")
        assert db.get_compression_summary_meta(sid) == {}
