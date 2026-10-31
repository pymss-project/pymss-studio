from importlib import import_module, reload
import pickle
import sys

import pytest

from pymss.modules._core_shims import alias_module


def test_alias_module_allows_pymss_core_modules():
    local_name = "pymss.modules._core_shims_test_spectrogram"

    try:
        module = alias_module(local_name, "pymss_core.modules.spectrogram")

        assert sys.modules[local_name] is module
        assert module.__name__ == "pymss_core.modules.spectrogram"
        assert getattr(sys.modules["pymss.modules"], local_name.rsplit(".", 1)[1]) is module
    finally:
        sys.modules.pop(local_name, None)
        parent = sys.modules["pymss.modules"]
        if hasattr(parent, local_name.rsplit(".", 1)[1]):
            delattr(parent, local_name.rsplit(".", 1)[1])


def test_alias_module_rejects_non_core_targets():
    local_name = "pymss.modules._core_shims_test_os"

    with pytest.raises(ValueError, match="invalid core module alias"):
        alias_module(local_name, "os")

    assert local_name not in sys.modules


def test_alias_module_rejects_non_pymss_local_names():
    with pytest.raises(ValueError, match="invalid local module alias"):
        alias_module("other.modules.spectrogram", "pymss_core.modules.spectrogram")


@pytest.mark.parametrize(
    "suffix",
    [
        "spectrogram",
        "bs_roformer",
        "bs_roformer.common",
        "bandit.core.model.bsrnn",
        "scnet.scnet",
        "vocal_remover.uvr_lib_v5.vr_network.nets",
    ],
)
def test_legacy_imports_preserve_module_identity_and_parent_attributes(suffix):
    legacy_name = f"pymss.modules.{suffix}"
    core_name = f"pymss_core.modules.{suffix}"
    canonical = import_module(core_name)
    original_spec = canonical.__spec__
    legacy = import_module(legacy_name)

    assert legacy is canonical
    assert canonical.__name__ == core_name
    assert canonical.__spec__ is original_spec
    assert canonical.__spec__.name == core_name
    parent_name, _, child_name = legacy_name.rpartition(".")
    assert getattr(import_module(parent_name), child_name) is canonical


@pytest.fixture(params=[False, True], ids=["module", "package"])
def core_child_module(tmp_path, monkeypatch, request):
    """Supply a core submodule absent from the explicit compatibility list."""
    package_name = "pymss_core.modules.bs_roformer"
    package = import_module(package_name)
    child_name = "_shim_test_child"
    is_package = request.param
    if is_package:
        child_dir = tmp_path / child_name
        child_dir.mkdir()
        (child_dir / "__init__.py").write_text("from .nested import Marker\n", encoding="utf-8")
        (child_dir / "nested.py").write_text("class Marker:\n    pass\n", encoding="utf-8")
    else:
        (tmp_path / f"{child_name}.py").write_text("class Marker:\n    pass\n", encoding="utf-8")
    monkeypatch.setattr(package, "__path__", [*package.__path__, str(tmp_path)])
    core_name = f"{package_name}.{child_name}"
    legacy_name = f"pymss.modules.bs_roformer.{child_name}"
    try:
        yield core_name, legacy_name, is_package
    finally:
        for name in list(sys.modules):
            if name in (core_name, legacy_name) or name.startswith((core_name + ".", legacy_name + ".")):
                sys.modules.pop(name, None)
        if hasattr(package, child_name):
            delattr(package, child_name)


@pytest.mark.parametrize("legacy_first", [False, True])
def test_unlisted_child_preserves_identity_metadata_and_pickles(core_child_module, legacy_first):
    core_name, legacy_name, is_package = core_child_module
    first_name, second_name = (legacy_name, core_name) if legacy_first else (core_name, legacy_name)
    first = import_module(first_name)
    original_spec = first.__spec__
    second = import_module(second_name)

    assert first is second
    assert first.Marker is second.Marker
    assert first.__name__ == core_name
    assert first.__package__ == (core_name if is_package else core_name.rpartition(".")[0])
    assert first.__spec__ is original_spec
    assert first.__spec__.name == core_name
    assert first.__loader__ is first.__spec__.loader
    assert pickle.loads(pickle.dumps(first.Marker())).__class__ is second.Marker
    legacy_pickle = f"c{legacy_name}\nMarker\n.".encode("ascii")
    assert pickle.loads(legacy_pickle) is first.Marker
    assert reload(first) is second
    assert second.__spec__.name == core_name
    if is_package:
        assert import_module(legacy_name + ".nested") is import_module(core_name + ".nested")


def test_unlisted_child_preserves_dependency_error_and_can_retry(core_child_module, tmp_path):
    core_name, legacy_name, is_package = core_child_module
    child_name = core_name.rsplit(".", 1)[1]
    source = tmp_path / child_name / "__init__.py" if is_package else tmp_path / f"{child_name}.py"
    original_source = source.read_text(encoding="utf-8")
    source.write_text("import _missing_shim_dependency\n", encoding="utf-8")

    with pytest.raises(ModuleNotFoundError) as exc_info:
        import_module(legacy_name)

    assert exc_info.value.name == "_missing_shim_dependency"
    assert core_name not in sys.modules
    assert legacy_name not in sys.modules
    assert not hasattr(import_module(core_name.rpartition(".")[0]), child_name)
    source.write_text(original_source, encoding="utf-8")
    assert import_module(legacy_name) is import_module(core_name)


def test_missing_child_keeps_import_error():
    name = "pymss.modules.bs_roformer._missing_shim_child"
    with pytest.raises(ModuleNotFoundError, match="_missing_shim_child"):
        import_module(name)
    assert name not in sys.modules


def test_local_vr_modules_are_not_redirected():
    for suffix in ("vr_models", "uvr_lib_v5.spec_utils"):
        name = f"pymss.modules.vocal_remover.{suffix}"
        module = import_module(name)
        assert module.__name__ == name
        assert module.__spec__.name == name
