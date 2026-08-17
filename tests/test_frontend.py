import json
from pathlib import Path

import numpy as np
import pytest
from safetensors.numpy import save_file

from hephaestus.cli import main
from hephaestus.frontend import list_tensor_names, load_matrix, load_tensor


def _write_sharded_checkpoint(root: Path) -> Path:
    save_file(
        {"model.layers.0.mlp.up_proj.weight": np.arange(20, dtype=np.float32).reshape(4, 5)},
        root / "model-00001-of-00002.safetensors",
    )
    save_file(
        {"model.layers.0.mlp.down_proj.weight": np.arange(15, dtype=np.float32).reshape(3, 5)},
        root / "model-00002-of-00002.safetensors",
    )
    index = root / "model.safetensors.index.json"
    index.write_text(
        json.dumps(
            {
                "metadata": {"total_size": 140},
                "weight_map": {
                    "model.layers.0.mlp.up_proj.weight": "model-00001-of-00002.safetensors",
                    "model.layers.0.mlp.down_proj.weight": "model-00002-of-00002.safetensors",
                },
            }
        ),
        encoding="utf-8",
    )
    return index


def test_sharded_safetensors_loads_only_selected_tile(tmp_path: Path) -> None:
    index = _write_sharded_checkpoint(tmp_path)
    loaded = load_tensor(
        index,
        tensor_key="model.layers.0.mlp.up_proj.weight",
        axis_slices=(slice(1, 3), slice(2, 5)),
        allowed_ndims=(2,),
    )

    assert loaded.original_shape == (4, 5)
    assert loaded.selected_shape == (2, 3)
    assert loaded.selection == ((1, 3), (2, 5))
    assert loaded.data_path.name == "model-00001-of-00002.safetensors"
    assert loaded.values.tolist() == [[7.0, 8.0, 9.0], [12.0, 13.0, 14.0]]


def test_checkpoint_directory_and_tensor_listing(tmp_path: Path) -> None:
    _write_sharded_checkpoint(tmp_path)

    names = list_tensor_names(tmp_path)
    matrix = load_matrix(
        tmp_path,
        tensor_key="model.layers.0.mlp.down_proj.weight",
        row_slice=slice(0, 2),
        column_slice=slice(1, 4),
    )

    assert names == (
        "model.layers.0.mlp.down_proj.weight",
        "model.layers.0.mlp.up_proj.weight",
    )
    assert matrix.shape == (2, 3)


def test_safetensors_index_rejects_path_escape(tmp_path: Path) -> None:
    index = tmp_path / "model.safetensors.index.json"
    index.write_text(
        json.dumps({"weight_map": {"layer.weight": "../escape.safetensors"}}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="escapes the checkpoint directory"):
        load_tensor(index, tensor_key="layer.weight", allowed_ndims=(2,))


def test_cli_compiles_checkpoint_slice_and_aligns_full_importance(tmp_path: Path) -> None:
    index = _write_sharded_checkpoint(tmp_path)
    importance = tmp_path / "importance.json"
    importance.write_text("[1, 2, 3, 4, 5]\n", encoding="utf-8")
    output = tmp_path / "out"

    exit_code = main(
        [
            "compile",
            str(index),
            "--tensor-key",
            "model.layers.0.mlp.up_proj.weight",
            "--rows",
            "1:3",
            "--columns",
            "2:5",
            "--importance",
            str(importance),
            "--out",
            str(output),
            "--verify-samples",
            "4",
        ]
    )

    assert exit_code == 0
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source"]["original_shape"] == [4, 5]
    assert manifest["source"]["shape"] == [2, 3]
    assert manifest["source"]["selection"] == [
        {"start": 1, "stop": 3},
        {"start": 2, "stop": 5},
    ]
    assert manifest["source"]["descriptor_sha256"]


def test_tensor_listing_cli_is_machine_readable(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    index = _write_sharded_checkpoint(tmp_path)

    assert main(["tensors", str(index), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert "model.layers.0.mlp.up_proj.weight" in payload["tensors"]
