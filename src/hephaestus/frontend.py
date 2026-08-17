"""Safe, bounded local tensor loaders for model checkpoints and calibration data."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray


FloatArray = NDArray[np.float64]


@dataclass(frozen=True)
class LoadedTensor:
    """One materialized tensor together with reproducible source metadata."""

    values: FloatArray
    requested_path: Path
    descriptor_path: Path
    data_path: Path
    tensor_key: str | None
    original_shape: tuple[int, ...]
    selection: tuple[tuple[int, int], ...]

    @property
    def selected_shape(self) -> tuple[int, ...]:
        return tuple(int(value) for value in self.values.shape)

    @property
    def selection_slices(self) -> tuple[slice, ...]:
        return tuple(slice(start, stop) for start, stop in self.selection)


def _safe_open_type() -> Any:
    try:
        from safetensors import safe_open
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError(
            "safetensors support is optional; install hephaestus-compiler[hf]"
        ) from exc
    return safe_open


def _resolve_requested_path(requested: Path) -> Path:
    if not requested.is_dir():
        return requested

    index_candidates = sorted(requested.glob("*.safetensors.index.json"))
    if len(index_candidates) == 1:
        return index_candidates[0]
    if len(index_candidates) > 1:
        raise ValueError(
            "checkpoint directory contains multiple Safetensors index files; pass one explicitly"
        )

    tensor_candidates = sorted(requested.glob("*.safetensors"))
    if len(tensor_candidates) == 1:
        return tensor_candidates[0]
    if len(tensor_candidates) > 1:
        raise ValueError(
            "checkpoint directory contains multiple Safetensors shards but no unique index"
        )
    raise ValueError("checkpoint directory contains no Safetensors index or tensor file")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in {path}") from exc


def _read_weight_map(index_path: Path, value: Any | None = None) -> dict[str, str]:
    document = _read_json(index_path) if value is None else value
    if not isinstance(document, dict) or not isinstance(document.get("weight_map"), dict):
        raise ValueError(f"{index_path} is not a Safetensors index")

    raw_map = document["weight_map"]
    if not raw_map:
        raise ValueError("Safetensors index contains an empty weight_map")

    weight_map: dict[str, str] = {}
    for tensor_name, shard_name in raw_map.items():
        if not isinstance(tensor_name, str) or not tensor_name:
            raise ValueError("Safetensors index contains an invalid tensor name")
        if not isinstance(shard_name, str) or not shard_name:
            raise ValueError(f"Safetensors index has an invalid shard for {tensor_name!r}")
        weight_map[tensor_name] = shard_name
    return weight_map


def _select_tensor_key(keys: list[str], tensor_key: str | None, *, container: str) -> str:
    if tensor_key is not None:
        if tensor_key not in keys:
            raise ValueError(f"tensor {tensor_key!r} is not present in {container}")
        return tensor_key
    if len(keys) == 1:
        return keys[0]
    raise ValueError(f"{container} contains multiple tensors; pass --tensor-key")


def _resolve_shard(index_path: Path, shard_name: str) -> Path:
    shard_relative = Path(shard_name)
    if shard_relative.is_absolute():
        raise ValueError("Safetensors index may not reference an absolute shard path")

    root = index_path.parent.resolve()
    shard = (index_path.parent / shard_relative).resolve()
    try:
        shard.relative_to(root)
    except ValueError as exc:
        raise ValueError("Safetensors index shard escapes the checkpoint directory") from exc

    if shard.suffix.lower() != ".safetensors":
        raise ValueError("Safetensors index references a non-Safetensors shard")
    if not shard.is_file():
        raise ValueError(f"Safetensors shard does not exist: {shard}")
    return shard


def _normalize_selection(
    shape: tuple[int, ...],
    axis_slices: tuple[slice | None, ...] | None,
) -> tuple[tuple[slice, ...], tuple[tuple[int, int], ...]]:
    if axis_slices is None:
        axis_slices = ()
    if len(axis_slices) > len(shape):
        raise ValueError("more axis slices were supplied than the tensor has dimensions")

    normalized_slices: list[slice] = []
    normalized_ranges: list[tuple[int, int]] = []
    for axis, size in enumerate(shape):
        requested = axis_slices[axis] if axis < len(axis_slices) else None
        if requested is None:
            start, stop = 0, size
        else:
            if requested.step not in (None, 1):
                raise ValueError("tensor slices do not support a step other than 1")
            if requested.start is not None and requested.start < 0:
                raise ValueError("tensor slice starts must be non-negative")
            if requested.stop is not None and requested.stop < 0:
                raise ValueError("tensor slice stops must be non-negative")
            start = 0 if requested.start is None else requested.start
            stop = size if requested.stop is None else requested.stop

        if start > size or stop > size:
            raise ValueError(f"slice [{start}:{stop}] exceeds axis {axis} of size {size}")
        if stop <= start:
            raise ValueError(f"slice [{start}:{stop}] selects no values on axis {axis}")
        normalized_slices.append(slice(start, stop))
        normalized_ranges.append((start, stop))

    return tuple(normalized_slices), tuple(normalized_ranges)


def _validate_values(
    values: NDArray[np.generic] | Any,
    *,
    allowed_ndims: tuple[int, ...],
) -> FloatArray:
    try:
        array = np.asarray(values, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise ValueError("tensor source does not contain a numeric array") from exc
    if array.ndim not in allowed_ndims:
        expected = ", ".join(str(value) for value in allowed_ndims)
        raise ValueError(f"expected a tensor with ndim in ({expected}), got shape {array.shape}")
    if array.size == 0:
        raise ValueError("tensor must not be empty")
    if not np.all(np.isfinite(array)):
        raise ValueError("tensor contains NaN or infinite values")
    return array


def _load_safetensor(
    *,
    requested_path: Path,
    descriptor_path: Path,
    data_path: Path,
    tensor_key: str | None,
    axis_slices: tuple[slice | None, ...] | None,
    allowed_ndims: tuple[int, ...],
) -> LoadedTensor:
    safe_open = _safe_open_type()
    with safe_open(data_path, framework="np") as archive:
        keys = list(archive.keys())
        selected = _select_tensor_key(keys, tensor_key, container=str(data_path))
        tensor_slice = archive.get_slice(selected)
        original_shape = tuple(int(value) for value in tensor_slice.get_shape())
        if len(original_shape) not in allowed_ndims:
            expected = ", ".join(str(value) for value in allowed_ndims)
            raise ValueError(
                f"expected a tensor with ndim in ({expected}), got shape {original_shape}"
            )
        selection_slices, selection = _normalize_selection(original_shape, axis_slices)
        values = _validate_values(
            tensor_slice[selection_slices],
            allowed_ndims=allowed_ndims,
        )

    return LoadedTensor(
        values=values,
        requested_path=requested_path,
        descriptor_path=descriptor_path,
        data_path=data_path,
        tensor_key=selected,
        original_shape=original_shape,
        selection=selection,
    )


def load_tensor(
    path: str | Path,
    *,
    tensor_key: str | None = None,
    axis_slices: tuple[slice | None, ...] | None = None,
    allowed_ndims: tuple[int, ...] = (1, 2),
) -> LoadedTensor:
    """Load one local tensor without pickle, with optional bounded slicing.

    ``path`` may be a JSON/NumPy array, an NPZ, a Safetensors file, a Hugging Face
    ``*.safetensors.index.json`` file, or a directory containing one unambiguous
    Safetensors checkpoint. For sharded checkpoints only the selected shard and
    selected tensor slice are materialized.
    """

    if not allowed_ndims:
        raise ValueError("allowed_ndims must not be empty")

    requested = Path(path)
    descriptor = _resolve_requested_path(requested)
    if not descriptor.exists():
        raise ValueError(f"tensor source does not exist: {descriptor}")

    suffix = descriptor.suffix.lower()
    if suffix == ".json":
        document = _read_json(descriptor)
        if isinstance(document, dict) and "weight_map" in document:
            weight_map = _read_weight_map(descriptor, document)
            selected = _select_tensor_key(
                sorted(weight_map),
                tensor_key,
                container=str(descriptor),
            )
            shard = _resolve_shard(descriptor, weight_map[selected])
            return _load_safetensor(
                requested_path=requested,
                descriptor_path=descriptor,
                data_path=shard,
                tensor_key=selected,
                axis_slices=axis_slices,
                allowed_ndims=allowed_ndims,
            )

        raw = np.asarray(document)
        original_shape = tuple(int(value) for value in raw.shape)
        selection_slices, selection = _normalize_selection(original_shape, axis_slices)
        values = _validate_values(raw[selection_slices], allowed_ndims=allowed_ndims)
        return LoadedTensor(
            values=values,
            requested_path=requested,
            descriptor_path=descriptor,
            data_path=descriptor,
            tensor_key=None,
            original_shape=original_shape,
            selection=selection,
        )

    if suffix == ".npy":
        raw = np.load(descriptor, allow_pickle=False, mmap_mode="r")
        original_shape = tuple(int(value) for value in raw.shape)
        selection_slices, selection = _normalize_selection(original_shape, axis_slices)
        values = _validate_values(raw[selection_slices], allowed_ndims=allowed_ndims)
        return LoadedTensor(
            values=values,
            requested_path=requested,
            descriptor_path=descriptor,
            data_path=descriptor,
            tensor_key=None,
            original_shape=original_shape,
            selection=selection,
        )

    if suffix == ".npz":
        with np.load(descriptor, allow_pickle=False) as archive:
            keys = list(archive.keys())
            selected = _select_tensor_key(keys, tensor_key, container=str(descriptor))
            raw = archive[selected]
            original_shape = tuple(int(value) for value in raw.shape)
            selection_slices, selection = _normalize_selection(original_shape, axis_slices)
            values = _validate_values(raw[selection_slices], allowed_ndims=allowed_ndims)
        return LoadedTensor(
            values=values,
            requested_path=requested,
            descriptor_path=descriptor,
            data_path=descriptor,
            tensor_key=selected,
            original_shape=original_shape,
            selection=selection,
        )

    if suffix == ".safetensors":
        return _load_safetensor(
            requested_path=requested,
            descriptor_path=descriptor,
            data_path=descriptor,
            tensor_key=tensor_key,
            axis_slices=axis_slices,
            allowed_ndims=allowed_ndims,
        )

    raise ValueError(
        "supported inputs are directories, .json, .npy, .npz, .safetensors, "
        "and .safetensors.index.json"
    )


def load_matrix(
    path: str | Path,
    *,
    tensor_key: str | None = None,
    row_slice: slice | None = None,
    column_slice: slice | None = None,
) -> FloatArray:
    """Compatibility helper that materializes exactly one 2-D matrix."""

    return load_tensor(
        path,
        tensor_key=tensor_key,
        axis_slices=(row_slice, column_slice),
        allowed_ndims=(2,),
    ).values


def list_tensor_names(path: str | Path) -> tuple[str, ...]:
    """List tensor names without materializing tensor payloads."""

    requested = Path(path)
    descriptor = _resolve_requested_path(requested)
    if not descriptor.exists():
        raise ValueError(f"tensor source does not exist: {descriptor}")

    suffix = descriptor.suffix.lower()
    if suffix == ".json":
        document = _read_json(descriptor)
        if isinstance(document, dict) and "weight_map" in document:
            return tuple(sorted(_read_weight_map(descriptor, document)))
        return ("<array>",)
    if suffix == ".npz":
        with np.load(descriptor, allow_pickle=False) as archive:
            return tuple(sorted(archive.keys()))
    if suffix == ".npy":
        return ("<array>",)
    if suffix == ".safetensors":
        safe_open = _safe_open_type()
        with safe_open(descriptor, framework="np") as archive:
            return tuple(sorted(archive.keys()))
    raise ValueError("tensor listing supports NumPy, JSON, and Safetensors sources")
