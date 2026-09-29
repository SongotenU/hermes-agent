"""Custom models the catalog has never heard of can still get a projector (vision).

The catalog carries only published models, so a self-staged finetune used to autoload without
`mmproj` and silently lost vision. `model_overrides.json` is read at preset-build time — which is
why it survives a regeneration — and must never break a boot when it is missing or malformed.
"""
from types import SimpleNamespace

from hermes_cli.local_runtime import presets
from hermes_cli.local_runtime.estimator import HardwareBudget, ModelProfile


def _stage(tmp_path, monkeypatch, stem: str):
    """One staged model whose header/profile are stubbed, with a fixed 4 GiB window budget."""
    mdir = tmp_path / "models"
    mdir.mkdir(exist_ok=True)
    gguf = mdir / f"{stem}.gguf"
    gguf.touch()
    monkeypatch.setattr(presets, "read_gguf_header",
                        lambda p: SimpleNamespace(path=p, sampling_defaults={}))
    monkeypatch.setattr(presets, "profile_from_gguf", lambda h: ModelProfile(
        name=h.path.stem, weights_bytes=4 << 30, embd_table_bytes=0, n_ctx_train=65536, layers=[]))
    return gguf


def _budget() -> HardwareBudget:
    return HardwareBudget(24 << 30, 24 << 30, 32 << 30)


def test_override_projector_reaches_a_non_catalog_model(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    projector = home / "models" / "assets" / "mmproj-Custom-BF16.gguf"
    projector.parent.mkdir(parents=True)
    projector.write_bytes(b"x" * 1024)

    path = presets.model_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"Custom-Heretic-27B": {"mmproj": "%s"}}' % projector, encoding="utf-8")

    gguf = _stage(tmp_path, monkeypatch, "Custom-Heretic-27B")
    result = presets.preset_for_model(gguf, _budget(), set())
    assert result is not None and result.keys is not None
    assert result.keys["mmproj"] == str(projector)


def test_no_override_leaves_a_non_catalog_model_without_a_projector(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    gguf = _stage(tmp_path, monkeypatch, "Custom-Heretic-27B")
    result = presets.preset_for_model(gguf, _budget(), set())
    assert result is not None and result.keys is not None
    assert "mmproj" not in result.keys


def test_overrides_read_as_empty_when_missing_or_corrupt(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    assert presets.load_model_overrides() == {}
    assert presets._override_mmproj_path("anything") is None

    path = presets.model_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert presets.load_model_overrides() == {}

    # A declared path that is not on disk is ignored rather than handed to llama.cpp.
    path.write_text('{"m": {"mmproj": "/nope/missing.gguf"}}', encoding="utf-8")
    assert presets._override_mmproj_path("m") is None


def test_catalog_projector_still_wins(tmp_path, monkeypatch):
    """An override must not shadow a catalog model's own projector."""
    from hermes_cli.local_runtime import bootstrap, catalog

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    entry = next(e for e in catalog.CATALOG if e.mmproj)
    assert entry.mmproj is not None
    catalog_projector = bootstrap.assets_dir() / entry.mmproj.local_name
    catalog_projector.parent.mkdir(parents=True, exist_ok=True)
    catalog_projector.touch()

    decoy = home / "decoy.gguf"
    decoy.touch()
    path = presets.model_overrides_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"%s": {"mmproj": "%s"}}' % (entry.variants[0].model_id, decoy),
                    encoding="utf-8")

    gguf = _stage(tmp_path, monkeypatch, entry.variants[0].model_id)
    result = presets.preset_for_model(gguf, _budget(), set())
    assert result is not None and result.keys is not None
    assert result.keys["mmproj"] == str(catalog_projector)
