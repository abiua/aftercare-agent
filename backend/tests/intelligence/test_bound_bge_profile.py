import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from aftercare.intelligence import retrieval_evidence as evidence


@pytest.mark.parametrize("extra_module", [False, True])
def test_pinned_bge_module_profile_allows_fileless_normalize_only(
    tmp_path, monkeypatch, extra_module
):
    for relative in evidence.BGE_FILES:
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"isolated fixture")
    modules = [
        {"path": "", "type": "sentence_transformers.models.Transformer"},
        {"path": "1_Pooling", "type": "sentence_transformers.models.Pooling"},
        {"path": "2_Normalize", "type": "sentence_transformers.models.Normalize"},
    ]
    if extra_module:
        modules.append({"path": "3_Custom", "type": "untrusted.module"})
    (tmp_path / "modules.json").write_text(json.dumps(modules))
    loaded = []

    def model(root, **kwargs):
        assert kwargs["local_files_only"] is True and kwargs["trust_remote_code"] is False
        assert not (Path(root) / "2_Normalize").exists()
        assert len([path for path in Path(root).rglob("*") if path.is_file()]) == 10
        loaded.append(True)
        return SimpleNamespace(
            encode=lambda *_args, **_kwargs: SimpleNamespace(tolist=lambda: [[1.0] * 512]),
        )

    monkeypatch.setitem(
        sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=model)
    )
    try:
        encode = evidence.bound_encoder(tmp_path, evidence.encoder_manifest(tmp_path))
        if extra_module:
            with pytest.raises(ValueError, match="BGE_BOUND_LOAD_FAILED") as error:
                encode(["test"])
            assert str(error.value.__context__) == "BGE_MODULE_PROFILE_UNSUPPORTED"
            assert not loaded
        else:
            assert len(encode(["test"])[0]) == 512 and loaded == [True]
    finally:
        evidence._bound_model.cache_clear()
