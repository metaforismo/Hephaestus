"""Digest-bound matched OpenROAD physical evidence for registered tiles."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .report import sha256_file, write_json

_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_MODULE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_METRIC_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SPEF_DATE_RE = re.compile(r'(?m)^\*DATE\s+"[^"\r\n]*"\r?$')
_STABLE_METRIC_WORDS = (
    "area",
    "cell",
    "clock",
    "congestion",
    "drc",
    "instance",
    "net",
    "power",
    "route",
    "slack",
    "tns",
    "utilization",
    "via",
    "violation",
    "wirelength",
    "wns",
)
_UNSTABLE_METRIC_WORDS = (
    "date",
    "elapsed",
    "host",
    "memory",
    "runtime",
    "timestamp",
    "version",
)


class OpenROADPhysicalError(RuntimeError):
    """Raised when matched physical evidence cannot be produced safely."""


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise OpenROADPhysicalError(f"cannot read JSON artifact {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise OpenROADPhysicalError(f"JSON artifact must be an object: {path}")
    return value


def _require_digest(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise OpenROADPhysicalError(f"{context} must be a lowercase SHA-256 digest")
    return value


def _require_positive_int(value: Any, *, context: str) -> int:
    if type(value) is not int or value <= 0:
        raise OpenROADPhysicalError(f"{context} must be a positive integer")
    return value


def _require_number(value: Any, *, context: str) -> float:
    if type(value) not in (int, float):
        raise OpenROADPhysicalError(f"{context} must be numeric")
    return float(value)


def _safe_module(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or _MODULE_RE.fullmatch(value) is None:
        raise OpenROADPhysicalError(f"{context} is not a safe module name: {value!r}")
    return value


def _resolve_artifact(
    root: Path,
    raw_path: Any,
    expected_digest: Any,
    *,
    context: str,
) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise OpenROADPhysicalError(f"{context}.path must be a non-empty string")
    digest = _require_digest(expected_digest, context=f"{context}.sha256")
    relative = Path(raw_path)
    if relative.is_absolute():
        raise OpenROADPhysicalError(f"{context}.path must be relative")
    resolved_root = root.resolve()
    resolved = (resolved_root / relative).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise OpenROADPhysicalError(f"{context}.path escapes its artifact root") from exc
    if resolved.is_symlink():
        raise OpenROADPhysicalError(f"{context} must not be a symlink")
    if not resolved.is_file():
        raise OpenROADPhysicalError(f"{context} does not exist: {resolved}")
    actual = sha256_file(resolved)
    if actual != digest:
        raise OpenROADPhysicalError(f"{context} digest mismatch: expected {digest}, got {actual}")
    return resolved


def _copy_tree_without_symlinks(source: Path, destination: Path) -> None:
    source_root = source.resolve()
    for path in source_root.rglob("*"):
        if path.is_symlink():
            raise OpenROADPhysicalError(f"registered bundle contains a symlink: {path}")
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(source_root, destination)


def _load_contract(path: Path) -> dict[str, Any]:
    contract = _load_json(path)
    if contract.get("schema") != "hephaestus.openroad-physical-contract.v1":
        raise OpenROADPhysicalError("unsupported OpenROAD physical contract schema")
    if contract.get("backends") != list(_BACKENDS):
        raise OpenROADPhysicalError("physical contract must name the three matched backends")
    if contract.get("platform") != "ihp-sg13g2":
        raise OpenROADPhysicalError("the first physical contract must target ihp-sg13g2")
    attempts = _require_positive_int(
        contract.get("attempts_per_backend"),
        context="attempts_per_backend",
    )
    if attempts != 2:
        raise OpenROADPhysicalError("the v1 physical contract requires exactly two attempts")

    clock = contract.get("clock")
    floorplan = contract.get("floorplan")
    io = contract.get("io")
    routing = contract.get("routing")
    if not all(isinstance(value, dict) for value in (clock, floorplan, io, routing)):
        raise OpenROADPhysicalError("physical contract sections are malformed")
    if clock.get("port") != "clk" or clock.get("name") != "core_clock":
        raise OpenROADPhysicalError("unexpected physical clock identity")
    clock_values = {
        field: _require_number(clock.get(field), context=f"clock.{field}")
        for field in (
            "period_ns",
            "input_delay_ns",
            "output_delay_ns",
            "uncertainty_ns",
        )
    }
    if clock_values["period_ns"] <= 0:
        raise OpenROADPhysicalError("clock period must be positive")
    for field in ("input_delay_ns", "output_delay_ns", "uncertainty_ns"):
        if clock_values[field] < 0:
            raise OpenROADPhysicalError(f"clock.{field} must be non-negative")
    die = floorplan.get("die_area_um")
    core = floorplan.get("core_area_um")
    if not isinstance(die, list) or not isinstance(core, list) or len(die) != 4 or len(core) != 4:
        raise OpenROADPhysicalError("die and core areas must each contain four coordinates")
    die_values = [_require_number(value, context="die_area_um") for value in die]
    core_values = [_require_number(value, context="core_area_um") for value in core]
    if not (die_values[0] < die_values[2] and die_values[1] < die_values[3]):
        raise OpenROADPhysicalError("die area is not positive")
    if not (core_values[0] < core_values[2] and core_values[1] < core_values[3]):
        raise OpenROADPhysicalError("core area is not positive")
    if not (
        die_values[0] <= core_values[0] < core_values[2] <= die_values[2]
        and die_values[1] <= core_values[1] < core_values[3] <= die_values[3]
    ):
        raise OpenROADPhysicalError("core area must be contained in the die area")
    density = _require_number(floorplan.get("place_density"), context="place_density")
    if not 0.0 < density < 1.0:
        raise OpenROADPhysicalError("place_density must be between zero and one")
    if io.get("input_driver_cell") != "sg13g2_buf_4":
        raise OpenROADPhysicalError("unexpected IHP input-driver cell")
    if _require_number(io.get("output_load_pf"), context="output_load_pf") <= 0:
        raise OpenROADPhysicalError("output load must be positive")
    if routing.get("minimum_layer") != "Metal2":
        raise OpenROADPhysicalError("unexpected minimum routing layer")
    if routing.get("maximum_layer") != "Metal5":
        raise OpenROADPhysicalError("unexpected maximum routing layer")
    if _require_positive_int(contract.get("openroad_threads"), context="openroad_threads") != 1:
        raise OpenROADPhysicalError("the v1 flow requires one OpenROAD thread")
    return contract


# ORFS names worst setup/hold slack as `finish__timing__...__ws` rather than
# using a literal `slack`/`wns` token. Runtime and environment noise is still
# rejected first by `_UNSTABLE_METRIC_WORDS`.
_STABLE_METRIC_WORDS = (*_STABLE_METRIC_WORDS, "timing")


def _load_probe_reference(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    reference = _load_json(path)
    if reference.get("schema") != "hephaestus.openroad-registered-smoke-reference.v1":
        raise OpenROADPhysicalError("unsupported OpenROAD smoke-reference schema")
    toolchain = reference.get("toolchain")
    physical = reference.get("physical_contract")
    source = reference.get("source")
    claims = reference.get("claims")
    if not all(isinstance(value, dict) for value in (toolchain, physical, source, claims)):
        raise OpenROADPhysicalError("OpenROAD smoke reference is malformed")
    image = toolchain.get("orfs_image_repo_digest")
    if (
        not isinstance(image, str)
        or not image.startswith("openroad/orfs@sha256:")
        or _SHA256_RE.fullmatch(image.split("sha256:", 1)[1]) is None
    ):
        raise OpenROADPhysicalError("smoke reference does not pin one ORFS image digest")
    required_true = (
        "registered_source_binding_verified",
        "single_backend_orfs_flow_completed",
        "placement_performed",
        "routing_performed",
        "gds_generated",
    )
    if any(claims.get(name) is not True for name in required_true):
        raise OpenROADPhysicalError("smoke reference lacks a required qualifying claim")
    required_false = (
        "matched_three_backend_physical_comparison_performed",
        "post_physical_equivalence_verified",
        "drc_clean",
        "lvs_clean",
        "power_estimated_with_activity",
        "post_layout_pex_verified",
        "foundry_signoff_complete",
        "silicon_verified",
    )
    if any(claims.get(name) is not False for name in required_false):
        raise OpenROADPhysicalError("smoke reference overstates its evidence boundary")

    clock = contract["clock"]
    floorplan = contract["floorplan"]
    io = contract["io"]
    routing = contract["routing"]
    expected = {
        "platform": contract["platform"],
        "backend": "shared_dag",
        "clock_name": clock["name"],
        "clock_port": clock["port"],
        "clock_period_ns": float(clock["period_ns"]),
        "input_delay_ns": float(clock["input_delay_ns"]),
        "output_delay_ns": float(clock["output_delay_ns"]),
        "clock_uncertainty_ns": float(clock["uncertainty_ns"]),
        "input_driver_cell": io["input_driver_cell"],
        "output_load_pf": float(io["output_load_pf"]),
        "die_area_um": [float(value) for value in floorplan["die_area_um"]],
        "core_area_um": [float(value) for value in floorplan["core_area_um"]],
        "place_density": float(floorplan["place_density"]),
        "min_routing_layer": routing["minimum_layer"],
        "max_routing_layer": routing["maximum_layer"],
    }
    for field, expected_value in expected.items():
        actual = physical.get(field)
        if isinstance(expected_value, float):
            if type(actual) not in (int, float) or float(actual) != expected_value:
                raise OpenROADPhysicalError(
                    f"smoke reference {field} differs from the permanent contract"
                )
        elif isinstance(expected_value, list):
            if not isinstance(actual, list) or [float(value) for value in actual] != expected_value:
                raise OpenROADPhysicalError(
                    f"smoke reference {field} differs from the permanent contract"
                )
        elif actual != expected_value:
            raise OpenROADPhysicalError(
                f"smoke reference {field} differs from the permanent contract"
            )
    _require_digest(
        source.get("registered_manifest_sha256"),
        context="smoke registered manifest",
    )
    _require_digest(source.get("core_sha256"), context="smoke source core")
    _require_digest(source.get("wrapper_sha256"), context="smoke source wrapper")
    if toolchain.get("num_cores") != contract["openroad_threads"]:
        raise OpenROADPhysicalError("smoke reference used a different ORFS core count")
    if toolchain.get("transactional_kepler_lec_enabled") is not False:
        raise OpenROADPhysicalError("smoke reference must disclose disabled transactional LEC")
    case = reference.get("case")
    if not isinstance(case, dict):
        raise OpenROADPhysicalError("OpenROAD smoke case is malformed")
    if (
        case.get("backend") != "shared_dag"
        or case.get("registered_latency_cycles") != 1
        or case.get("initiation_interval_cycles") != 1
        or case.get("runtime_coefficient_reads_per_matvec") != 0
    ):
        raise OpenROADPhysicalError("OpenROAD smoke case differs from the registered contract")
    return reference


def _load_registered_reference(path: Path) -> dict[str, Any]:
    reference = _load_json(path)
    if reference.get("schema") != "hephaestus.registered-matched-tiles-reference.v1":
        raise OpenROADPhysicalError("unsupported registered-tile reference schema")
    if reference.get("reference_id") != "registered-matched-tiles-tiny-v1":
        raise OpenROADPhysicalError("unexpected registered-tile reference identity")
    if not isinstance(reference.get("contract"), dict):
        raise OpenROADPhysicalError("registered-tile reference contract is malformed")
    return reference


def _validate_registered_bundle(
    bundle: Path,
    registered_reference: dict[str, Any],
    probe_reference: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    manifest_path = bundle / "registered_manifest.json"
    if not manifest_path.is_file():
        raise OpenROADPhysicalError("registered bundle is missing registered_manifest.json")
    manifest = _load_json(manifest_path)
    if manifest.get("schema") != "hephaestus.registered-matched-tiles.v1":
        raise OpenROADPhysicalError("unsupported registered-tile manifest schema")
    if manifest.get("contract") != registered_reference.get("contract"):
        raise OpenROADPhysicalError("registered bundle contract differs from its pinned reference")
    claims = manifest.get("claims")
    required_claims = (
        "source_matched_integer_contract_verified",
        "source_exhaustive_combinational_equivalence_verified",
        "source_formal_negative_control_counterexample_found",
        "registered_streaming_interface_generated",
        "registered_backends_match_oracle_on_executed_schedule",
        "one_cycle_latency_verified_on_executed_schedule",
        "initiation_interval_one_verified_on_executed_schedule",
        "reset_flush_verified_on_executed_schedule",
        "simulation_negative_control_detected",
    )
    if not isinstance(claims, dict) or any(
        claims.get(name) is not True for name in required_claims
    ):
        raise OpenROADPhysicalError("registered source evidence is not fully qualified")
    forbidden_claims = (
        "sequential_formal_equivalence_verified",
        "post_synthesis_ppa_measured",
        "placement_performed",
        "routing_performed",
        "power_estimated",
        "post_layout_pex_verified",
        "silicon_verified",
    )
    if any(claims.get(name) is not False for name in forbidden_claims):
        raise OpenROADPhysicalError("registered source evidence has an invalid claim boundary")

    backends = manifest.get("backends")
    if not isinstance(backends, dict) or set(backends) != set(_BACKENDS):
        raise OpenROADPhysicalError("registered manifest does not contain three matched backends")
    reference_cores = registered_reference.get("source_artifacts", {}).get("core_sha256")
    reference_wrappers = registered_reference.get("generated_artifacts", {}).get("wrapper_sha256")
    if not isinstance(reference_cores, dict) or not isinstance(reference_wrappers, dict):
        raise OpenROADPhysicalError("registered reference artifact digests are malformed")

    validated: dict[str, dict[str, Any]] = {}
    for backend_name in _BACKENDS:
        backend = backends[backend_name]
        if not isinstance(backend, dict):
            raise OpenROADPhysicalError(f"registered backend {backend_name!r} is malformed")
        core_module = _safe_module(
            backend.get("core_module"), context=f"{backend_name}.core_module"
        )
        wrapper_module = _safe_module(
            backend.get("wrapper_module"), context=f"{backend_name}.wrapper_module"
        )
        core_path = _resolve_artifact(
            bundle,
            backend.get("core_rtl"),
            backend.get("core_sha256"),
            context=f"{backend_name}.core_rtl",
        )
        wrapper_path = _resolve_artifact(
            bundle,
            backend.get("wrapper_rtl"),
            backend.get("wrapper_sha256"),
            context=f"{backend_name}.wrapper_rtl",
        )
        if backend.get("runtime_coefficient_reads_per_matvec") != 0:
            raise OpenROADPhysicalError(f"backend {backend_name} is not a zero-fetch core")
        if sha256_file(core_path) != reference_cores.get(backend_name):
            raise OpenROADPhysicalError(
                f"registered core {backend_name} differs from the pinned reference"
            )
        if sha256_file(wrapper_path) != reference_wrappers.get(backend_name):
            raise OpenROADPhysicalError(
                f"registered wrapper {backend_name} differs from the pinned reference"
            )
        validated[backend_name] = {
            "core_module": core_module,
            "wrapper_module": wrapper_module,
            "core_rtl": core_path.name,
            "core_sha256": sha256_file(core_path),
            "wrapper_rtl": wrapper_path.name,
            "wrapper_sha256": sha256_file(wrapper_path),
        }

    smoke_source = probe_reference["source"]
    # The full smoke manifest hash is qualifying-run provenance. It includes
    # proof/log digests that can change while the registered contract,
    # cores, wrappers, oracle, and claim boundary remain identical.
    # The pinned registered reference above is the stable authority;
    # each physical attempt still binds the exact current manifest hash.
    shared = validated["shared_dag"]
    if shared["core_sha256"] != smoke_source["core_sha256"]:
        raise OpenROADPhysicalError("shared-DAG core differs from the qualifying smoke run")
    if shared["wrapper_sha256"] != smoke_source["wrapper_sha256"]:
        raise OpenROADPhysicalError("shared-DAG wrapper differs from the qualifying smoke run")
    return manifest, validated


def emit_sdc(contract: dict[str, Any]) -> str:
    """Emit the common registered-tile SDC boundary."""

    clock = contract["clock"]
    io = contract["io"]
    return "\n".join(
        [
            (
                f"create_clock -name {clock['name']} -period "
                f"{float(clock['period_ns']):.9g} [get_ports {clock['port']}]"
            ),
            "",
            "set data_inputs [all_inputs -no_clocks]",
            (
                f"set_input_delay {float(clock['input_delay_ns']):.9g} "
                f"-clock {clock['name']} $data_inputs"
            ),
            (
                f"set_output_delay {float(clock['output_delay_ns']):.9g} "
                f"-clock {clock['name']} [all_outputs]"
            ),
            (
                f"set_clock_uncertainty {float(clock['uncertainty_ns']):.9g} "
                f"[get_clocks {clock['name']}]"
            ),
            "",
            f"set_driving_cell -lib_cell {io['input_driver_cell']} $data_inputs",
            f"set_load {float(io['output_load_pf']):.9g} [all_outputs]",
            "",
        ]
    )


def emit_config(
    contract: dict[str, Any],
    *,
    backend_name: str,
    backend: dict[str, Any],
) -> str:
    """Emit one ORFS config while keeping every physical variable common."""

    if backend_name not in _BACKENDS:
        raise ValueError(f"unsupported backend: {backend_name}")
    floorplan = contract["floorplan"]
    routing = contract["routing"]
    clock = contract["clock"]
    die = " ".join(f"{float(value):.9g}" for value in floorplan["die_area_um"])
    core = " ".join(f"{float(value):.9g}" for value in floorplan["core_area_um"])
    continuation = "\\"
    lines = [
        f"export PLATFORM = {contract['platform']}",
        f"export DESIGN_NAME = {backend['wrapper_module']}",
        f"export DESIGN_NICKNAME = hephaestus_registered_{backend_name}",
        "",
        (
            f"export VERILOG_FILES = "
            f"$(HEPHAESTUS_REGISTERED_DIR)/{backend['core_rtl']} {continuation}"
        ),
        (f"   $(HEPHAESTUS_REGISTERED_DIR)/{backend['wrapper_rtl']}"),
        "export SDC_FILE = $(dir $(DESIGN_CONFIG))/constraint.sdc",
        "export RULES_JSON = $(dir $(DESIGN_CONFIG))/rules.json",
        "export SYNTH_SCRIPT = $(dir $(DESIGN_CONFIG))/synth_compat.tcl",
        (
            "export YOSYS_DEPENDENCIES += $(SYNTH_SCRIPT) "
            "$(dir $(DESIGN_CONFIG))/sanitize_yosys_netlist.py"
        ),
        "",
        f"export CLOCK_PORT = {clock['port']}",
        f"export CLOCK_PERIOD = {float(clock['period_ns']):.9g}",
        f"export DIE_AREA = {die}",
        f"export CORE_AREA = {core}",
        f"export PLACE_DENSITY = {float(floorplan['place_density']):.9g}",
        f"export MIN_ROUTING_LAYER = {routing['minimum_layer']}",
        f"export MAX_ROUTING_LAYER = {routing['maximum_layer']}",
        f"export NUM_CORES = {int(contract['openroad_threads'])}",
        "export LEC_CHECK = 0",
        "",
    ]
    return "\n".join(lines)


def prepare_physical_evidence(
    registered_bundle: Path,
    registered_reference_path: Path,
    probe_reference_path: Path,
    contract_path: Path,
    output_dir: Path,
    *,
    helper_dir: Path | None = None,
) -> dict[str, Any]:
    """Validate and stage the exact registered sources for matched ORFS runs."""

    source_bundle = registered_bundle.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    contract = _load_contract(contract_path.resolve())
    registered_reference = _load_registered_reference(registered_reference_path.resolve())
    probe_reference = _load_probe_reference(probe_reference_path.resolve(), contract)
    registered_manifest, backends = _validate_registered_bundle(
        source_bundle,
        registered_reference,
        probe_reference,
    )

    helpers = (
        helper_dir.resolve()
        if helper_dir is not None
        else Path("flows/openroad/registered_shared_dag").resolve()
    )
    helper_paths: dict[str, Path] = {}
    for label, filename in (
        ("synth_compat", "synth_compat.tcl"),
        ("sanitizer", "sanitize_yosys_netlist.py"),
    ):
        path = (helpers / filename).resolve()
        try:
            path.relative_to(helpers)
        except ValueError as exc:
            raise OpenROADPhysicalError(
                f"physical helper {filename} escapes its helper root"
            ) from exc
        if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
            raise OpenROADPhysicalError(
                f"physical helper {filename} is missing, empty, or a symlink"
            )
        helper_paths[label] = path

    staged_registered = output / "registered"
    _copy_tree_without_symlinks(source_bundle, staged_registered)
    staged_contract = output / "physical_contract.json"
    staged_registered_reference = output / "registered_reference.json"
    staged_probe_reference = output / "openroad_smoke_reference.json"
    shutil.copyfile(contract_path.resolve(), staged_contract)
    shutil.copyfile(registered_reference_path.resolve(), staged_registered_reference)
    shutil.copyfile(probe_reference_path.resolve(), staged_probe_reference)

    designs_root = output / "designs"
    design_specs: dict[str, Any] = {}
    for backend_name in _BACKENDS:
        backend = backends[backend_name]
        design_dir = designs_root / backend_name
        design_dir.mkdir(parents=True, exist_ok=True)
        config_path = design_dir / "config.mk"
        sdc_path = design_dir / "constraint.sdc"
        rules_path = design_dir / "rules.json"
        synth_compat_path = design_dir / "synth_compat.tcl"
        sanitizer_path = design_dir / "sanitize_yosys_netlist.py"
        config_path.write_text(
            emit_config(contract, backend_name=backend_name, backend=backend),
            encoding="utf-8",
        )
        sdc_path.write_text(emit_sdc(contract), encoding="utf-8")
        write_json(rules_path, {})
        shutil.copyfile(helper_paths["synth_compat"], synth_compat_path)
        shutil.copyfile(helper_paths["sanitizer"], sanitizer_path)
        design_specs[backend_name] = {
            **backend,
            "design_dir": design_dir.relative_to(output).as_posix(),
            "config": config_path.relative_to(output).as_posix(),
            "config_sha256": sha256_file(config_path),
            "sdc": sdc_path.relative_to(output).as_posix(),
            "sdc_sha256": sha256_file(sdc_path),
            "rules": rules_path.relative_to(output).as_posix(),
            "rules_sha256": sha256_file(rules_path),
            "synth_compat": synth_compat_path.relative_to(output).as_posix(),
            "synth_compat_sha256": sha256_file(synth_compat_path),
            "sanitizer": sanitizer_path.relative_to(output).as_posix(),
            "sanitizer_sha256": sha256_file(sanitizer_path),
        }

    staged_manifest = staged_registered / "registered_manifest.json"
    if sha256_file(staged_manifest) != sha256_file(source_bundle / "registered_manifest.json"):
        raise OpenROADPhysicalError("registered manifest changed while staging")
    for backend_name, backend in design_specs.items():
        if sha256_file(staged_registered / backend["core_rtl"]) != backend["core_sha256"]:
            raise OpenROADPhysicalError(f"staged core digest drifted for {backend_name}")
        if sha256_file(staged_registered / backend["wrapper_rtl"]) != backend["wrapper_sha256"]:
            raise OpenROADPhysicalError(f"staged wrapper digest drifted for {backend_name}")

    manifest = {
        "schema": "hephaestus.openroad-physical-prepared.v1",
        "evidence_level": "matched_registered_sources_prepared_for_orfs",
        "source": {
            "registered_dir": staged_registered.relative_to(output).as_posix(),
            "registered_manifest": staged_manifest.relative_to(output).as_posix(),
            "registered_manifest_sha256": sha256_file(staged_manifest),
            "registered_reference": staged_registered_reference.name,
            "registered_reference_sha256": sha256_file(staged_registered_reference),
            "probe_reference": staged_probe_reference.name,
            "probe_reference_sha256": sha256_file(staged_probe_reference),
        },
        "contract": {
            "path": staged_contract.name,
            "sha256": sha256_file(staged_contract),
            "value": contract,
        },
        "toolchain": {
            "orfs_image_repo_digest": probe_reference["toolchain"]["orfs_image_repo_digest"],
            "orfs_image_id": probe_reference["toolchain"].get("orfs_image_id"),
            "tool_versions": probe_reference["toolchain"].get("tool_versions"),
            "num_cores": probe_reference["toolchain"].get("num_cores"),
            "transactional_kepler_lec_enabled": probe_reference["toolchain"].get(
                "transactional_kepler_lec_enabled"
            ),
            "qualifying_smoke_run": probe_reference.get("qualifying_run"),
        },
        "backends": design_specs,
        "source_registered_claims": registered_manifest["claims"],
        "claims": {
            "registered_source_binding_verified": True,
            "all_three_backends_prepared": True,
            "common_physical_contract_emitted": True,
            "orfs_image_digest_pinned": True,
            "placement_performed": False,
            "routing_performed": False,
            "post_physical_equivalence_verified": False,
            "comparative_ppa_claim_enabled": False,
            "drc_clean": False,
            "lvs_clean": False,
            "power_estimated_with_activity": False,
            "post_layout_pex_verified": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "prepared.json", manifest)
    return manifest


def _resolve_executable(requested: str) -> str:
    resolved = shutil.which(requested)
    if resolved is None:
        candidate = Path(requested)
        if candidate.is_file():
            resolved = str(candidate.resolve())
    if resolved is None:
        raise OpenROADPhysicalError(f"executable was not found: {requested!r}")
    return resolved


def _run_command(
    command: list[str],
    *,
    cwd: Path | None = None,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise OpenROADPhysicalError(
            f"command timed out after {timeout_seconds} seconds: {command!r}"
        ) from exc


def _exactly_one(root: Path, pattern: str, *, context: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise OpenROADPhysicalError(
            f"expected exactly one {context}, found {len(matches)}: {matches}"
        )
    path = matches[0]
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise OpenROADPhysicalError(f"{context} is missing, empty, or a symlink: {path}")
    return path


def gds_timestamp_normalized_sha256(path: Path) -> str:
    """Hash GDSII bytes while zeroing BGNLIB/BGNSTR date records."""

    content = path.read_bytes()
    digest = hashlib.sha256()
    offset = 0
    while offset < len(content):
        if offset + 4 > len(content):
            raise OpenROADPhysicalError(f"truncated GDSII record header in {path}")
        length = int.from_bytes(content[offset : offset + 2], "big")
        if length < 4 or length % 2 != 0 or offset + length > len(content):
            raise OpenROADPhysicalError(f"invalid GDSII record length in {path}: {length}")
        record = bytearray(content[offset : offset + length])
        record_type = record[2]
        if record_type in (0x01, 0x05):
            for index in range(4, len(record)):
                record[index] = 0
        digest.update(record)
        offset += length
    return digest.hexdigest()


def spef_date_normalized_sha256(path: Path) -> str:
    """Hash SPEF text while replacing exactly one non-semantic ``*DATE`` line."""

    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise OpenROADPhysicalError(f"final SPEF is not valid UTF-8: {path}") from exc
    matches = list(_SPEF_DATE_RE.finditer(text))
    if len(matches) != 1:
        raise OpenROADPhysicalError(f"final SPEF must contain exactly one *DATE record: {path}")
    normalized = _SPEF_DATE_RE.sub('*DATE "<normalized>"', text, count=1)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_def_metrics(path: Path) -> dict[str, Any]:
    """Extract a small deterministic geometry/structure contract from final DEF."""

    text = path.read_text(encoding="utf-8", errors="strict")
    units_match = re.search(r"(?m)^UNITS\s+DISTANCE\s+MICRONS\s+(\d+)\s*;", text)
    die_match = re.search(
        r"(?m)^DIEAREA\s*\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*"
        r"\(\s*(-?\d+)\s+(-?\d+)\s*\)\s*;",
        text,
    )
    if units_match is None or die_match is None:
        raise OpenROADPhysicalError(f"final DEF lacks units or DIEAREA: {path}")
    units = int(units_match.group(1))
    if units <= 0:
        raise OpenROADPhysicalError("DEF units must be positive")
    raw_die = [int(die_match.group(index)) for index in range(1, 5)]
    die_um = [value / units for value in raw_die]

    def count(section: str) -> int:
        match = re.search(rf"(?m)^{section}\s+(\d+)\s*;", text)
        if match is None:
            raise OpenROADPhysicalError(f"final DEF lacks {section} count")
        return int(match.group(1))

    return {
        "database_units_per_micron": units,
        "die_area_database_units": raw_die,
        "die_area_um": die_um,
        "die_width_um": die_um[2] - die_um[0],
        "die_height_um": die_um[3] - die_um[1],
        "component_count": count("COMPONENTS"),
        "net_count": count("NETS"),
        "special_net_count": count("SPECIALNETS"),
        "pin_count": count("PINS"),
        "row_count": len(re.findall(r"(?m)^ROW\s+", text)),
        "track_statement_count": len(re.findall(r"(?m)^TRACKS\s+", text)),
        "via_definition_count": count("VIAS"),
    }


def stable_metadata_metrics(value: dict[str, Any]) -> dict[str, int | float | bool]:
    """Select numeric ORFS QoR fields while excluding run-environment noise."""

    selected: dict[str, int | float | bool] = {}

    def visit(prefix: str, current: Any) -> None:
        if isinstance(current, dict):
            for key in sorted(current):
                next_prefix = f"{prefix}.{key}" if prefix else str(key)
                visit(next_prefix, current[key])
            return
        tokens = set(_METRIC_TOKEN_RE.findall(prefix.lower()))
        if tokens.intersection(_UNSTABLE_METRIC_WORDS):
            return
        if not tokens.intersection(_STABLE_METRIC_WORDS):
            return
        if type(current) in (int, float, bool):
            selected[prefix] = current

    visit("", value)
    if not selected:
        raise OpenROADPhysicalError("ORFS metadata contains no stable numeric physical metrics")
    return selected


def _load_prepared(path: Path) -> tuple[Path, dict[str, Any]]:
    prepared_path = path.resolve()
    prepared = _load_json(prepared_path)
    if prepared.get("schema") != "hephaestus.openroad-physical-prepared.v1":
        raise OpenROADPhysicalError("unsupported prepared OpenROAD evidence schema")
    claims = prepared.get("claims")
    if not isinstance(claims, dict):
        raise OpenROADPhysicalError("prepared evidence claims are malformed")
    required = (
        "registered_source_binding_verified",
        "all_three_backends_prepared",
        "common_physical_contract_emitted",
        "orfs_image_digest_pinned",
    )
    if any(claims.get(name) is not True for name in required):
        raise OpenROADPhysicalError("prepared evidence is not qualified")
    root = prepared_path.parent
    contract_spec = prepared.get("contract")
    if not isinstance(contract_spec, dict):
        raise OpenROADPhysicalError("prepared physical contract is malformed")
    contract_path = _resolve_artifact(
        root,
        contract_spec.get("path"),
        contract_spec.get("sha256"),
        context="prepared physical contract",
    )
    if _load_contract(contract_path) != contract_spec.get("value"):
        raise OpenROADPhysicalError("prepared physical contract value drifted")
    return root, prepared


def _relative_artifact(path: Path, root: Path) -> dict[str, Any]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def run_physical_attempt(
    prepared_path: Path,
    output_dir: Path,
    *,
    backend_name: str,
    attempt: int,
    docker: str = "docker",
    timeout_seconds: int = 7200,
) -> dict[str, Any]:
    """Run one registered backend through the pinned ORFS container."""

    if backend_name not in _BACKENDS:
        raise ValueError(f"unsupported backend: {backend_name}")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    prepared_root, prepared = _load_prepared(prepared_path)
    attempts = int(prepared["contract"]["value"]["attempts_per_backend"])
    if attempt < 1 or attempt > attempts:
        raise ValueError(f"attempt must be between 1 and {attempts}")

    backend = prepared.get("backends", {}).get(backend_name)
    if not isinstance(backend, dict):
        raise OpenROADPhysicalError(f"prepared backend is missing: {backend_name}")
    registered_dir = _resolve_artifact(
        prepared_root,
        prepared["source"]["registered_manifest"],
        prepared["source"]["registered_manifest_sha256"],
        context="prepared registered manifest",
    ).parent
    design_dir = (prepared_root / str(backend.get("design_dir", ""))).resolve()
    try:
        design_dir.relative_to(prepared_root)
    except ValueError as exc:
        raise OpenROADPhysicalError("prepared design directory escapes its root") from exc
    for label in ("config", "sdc", "rules", "synth_compat", "sanitizer"):
        _resolve_artifact(
            prepared_root,
            backend.get(label),
            backend.get(f"{label}_sha256"),
            context=f"{backend_name}.{label}",
        )
    for label in ("core", "wrapper"):
        _resolve_artifact(
            registered_dir,
            backend.get(f"{label}_rtl"),
            backend.get(f"{label}_sha256"),
            context=f"{backend_name}.{label}_rtl",
        )

    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    for directory in ("results", "reports", "logs", "objects", "provenance"):
        (output / directory).mkdir(exist_ok=True)

    docker_executable = _resolve_executable(docker)
    image = prepared.get("toolchain", {}).get("orfs_image_repo_digest")
    if not isinstance(image, str) or not image.startswith("openroad/orfs@sha256:"):
        raise OpenROADPhysicalError("prepared evidence does not pin an ORFS image")

    pull = _run_command(
        [docker_executable, "pull", image],
        timeout_seconds=min(timeout_seconds, 1800),
    )
    (output / "provenance/docker-pull.stdout.txt").write_text(pull.stdout, encoding="utf-8")
    (output / "provenance/docker-pull.stderr.txt").write_text(pull.stderr, encoding="utf-8")
    if pull.returncode != 0:
        raise OpenROADPhysicalError("cannot pull the pinned ORFS image")

    inspect = _run_command(
        [docker_executable, "image", "inspect", image],
        timeout_seconds=120,
    )
    inspect_path = output / "provenance/orfs-image-inspect.json"
    inspect_path.write_text(inspect.stdout, encoding="utf-8")
    if inspect.returncode != 0:
        raise OpenROADPhysicalError("cannot inspect the pinned ORFS image")
    try:
        inspect_value = json.loads(inspect.stdout)
    except json.JSONDecodeError as exc:
        raise OpenROADPhysicalError("Docker image inspection is not valid JSON") from exc
    if not isinstance(inspect_value, list) or len(inspect_value) != 1:
        raise OpenROADPhysicalError("Docker image inspection is ambiguous")
    repo_digests = inspect_value[0].get("RepoDigests")
    if not isinstance(repo_digests, list) or image not in repo_digests:
        raise OpenROADPhysicalError("pulled ORFS image does not expose the pinned RepoDigest")
    expected_image_id = prepared.get("toolchain", {}).get("orfs_image_id")
    if expected_image_id is not None and inspect_value[0].get("Id") != expected_image_id:
        raise OpenROADPhysicalError("pulled ORFS image ID differs from the smoke reference")

    tool_versions = _run_command(
        [
            docker_executable,
            "run",
            "--rm",
            image,
            "bash",
            "-lc",
            (
                "set -e; source /OpenROAD-flow-scripts/env.sh; "
                "printf 'orfs_commit='; "
                "git -C /OpenROAD-flow-scripts rev-parse HEAD 2>/dev/null || "
                "printf 'unavailable\\n'; "
                "openroad -version; yosys -V; "
                "(klayout -v || true)"
            ),
        ],
        timeout_seconds=300,
    )
    tool_versions_path = output / "provenance/tool-versions.txt"
    tool_versions_path.write_text(
        tool_versions.stdout + tool_versions.stderr,
        encoding="utf-8",
    )
    if tool_versions.returncode != 0:
        raise OpenROADPhysicalError("cannot capture the ORFS tool versions")
    expected_versions = prepared.get("toolchain", {}).get("tool_versions")
    if isinstance(expected_versions, dict):
        combined_versions = tool_versions.stdout + tool_versions.stderr
        for label in ("openroad", "yosys", "klayout"):
            expected = expected_versions.get(label)
            if isinstance(expected, str) and expected not in combined_versions:
                raise OpenROADPhysicalError(f"ORFS tool banner differs for {label}: {expected!r}")

    variant = f"attempt-{attempt:02d}"
    command = [
        docker_executable,
        "run",
        "--rm",
        "-e",
        "HEPHAESTUS_REGISTERED_DIR=/work/registered",
        "-v",
        f"{registered_dir}:/work/registered:ro",
        "-v",
        f"{design_dir}:/work/design:ro",
        "-v",
        f"{output / 'results'}:/OpenROAD-flow-scripts/flow/results",
        "-v",
        f"{output / 'reports'}:/OpenROAD-flow-scripts/flow/reports",
        "-v",
        f"{output / 'logs'}:/OpenROAD-flow-scripts/flow/logs",
        "-v",
        f"{output / 'objects'}:/OpenROAD-flow-scripts/flow/objects",
        image,
        "bash",
        "-lc",
        (
            "set -euo pipefail; "
            "source /OpenROAD-flow-scripts/env.sh; "
            "cd /OpenROAD-flow-scripts/flow; "
            "make DESIGN_CONFIG=/work/design/config.mk "
            f"FLOW_VARIANT={variant}; "
            "make DESIGN_CONFIG=/work/design/config.mk "
            f"FLOW_VARIANT={variant} metadata-generate"
        ),
    ]
    completed = _run_command(command, timeout_seconds=timeout_seconds)
    stdout_path = output / "orfs.stdout.txt"
    stderr_path = output / "orfs.stderr.txt"
    returncode_path = output / "orfs.returncode.txt"
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    returncode_path.write_text(f"{completed.returncode}\n", encoding="utf-8")
    if completed.returncode != 0:
        raise OpenROADPhysicalError(
            f"ORFS failed for {backend_name} attempt {attempt}; inspect preserved logs"
        )

    artifacts = {
        "final_gds": _exactly_one(output, f"results/**/{variant}/6_final.gds", context="final GDS"),
        "final_def": _exactly_one(output, f"results/**/{variant}/6_final.def", context="final DEF"),
        "final_verilog": _exactly_one(
            output, f"results/**/{variant}/6_final.v", context="final Verilog"
        ),
        "final_odb": _exactly_one(
            output, f"results/**/{variant}/6_final.odb", context="final OpenDB"
        ),
        "final_spef": _exactly_one(
            output, f"results/**/{variant}/6_final.spef", context="final SPEF"
        ),
        "metadata": _exactly_one(
            output, f"reports/**/{variant}/metadata.json", context="ORFS metadata"
        ),
    }
    metadata = _load_json(artifacts["metadata"])
    def_metrics = parse_def_metrics(artifacts["final_def"])
    stable_metrics = stable_metadata_metrics(metadata)
    gds_normalized = gds_timestamp_normalized_sha256(artifacts["final_gds"])
    spef_normalized = spef_date_normalized_sha256(artifacts["final_spef"])

    prepared_digest = sha256_file(prepared_path.resolve())
    run_manifest = {
        "schema": "hephaestus.openroad-physical-run.v1",
        "evidence_level": "single_registered_backend_orfs_rtl_to_gds",
        "identity": {
            "backend": backend_name,
            "attempt": attempt,
            "variant": variant,
        },
        "source": {
            "prepared_manifest_sha256": prepared_digest,
            "registered_manifest_sha256": prepared["source"]["registered_manifest_sha256"],
            "core_sha256": backend["core_sha256"],
            "wrapper_sha256": backend["wrapper_sha256"],
            "config_sha256": backend["config_sha256"],
            "sdc_sha256": backend["sdc_sha256"],
            "rules_sha256": backend["rules_sha256"],
            "synth_compat_sha256": backend["synth_compat_sha256"],
            "sanitizer_sha256": backend["sanitizer_sha256"],
            "contract_sha256": prepared["contract"]["sha256"],
        },
        "toolchain": {
            "orfs_image_repo_digest": image,
            "image_inspect": _relative_artifact(inspect_path, output),
            "tool_versions": _relative_artifact(tool_versions_path, output),
        },
        "artifacts": {
            label: _relative_artifact(path, output) for label, path in sorted(artifacts.items())
        },
        "normalized": {
            "gds_timestamp_normalized_sha256": gds_normalized,
            "spef_date_normalized_sha256": spef_normalized,
            "def_metrics": def_metrics,
            "stable_metadata_metrics": stable_metrics,
        },
        "execution": {
            "returncode": completed.returncode,
            "stdout": _relative_artifact(stdout_path, output),
            "stderr": _relative_artifact(stderr_path, output),
            "returncode_file": _relative_artifact(returncode_path, output),
        },
        "claims": {
            "registered_source_binding_verified": True,
            "pinned_orfs_image_used": True,
            "placement_performed": True,
            "routing_performed": True,
            "gds_generated": True,
            "spef_generated": True,
            "metadata_generated": True,
            "post_physical_equivalence_verified": False,
            "drc_clean": False,
            "lvs_clean": False,
            "power_estimated_with_activity": False,
            "post_layout_pex_verified": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "openroad_run.json", run_manifest)
    return run_manifest


def _validate_run_manifest(
    path: Path,
    *,
    prepared: dict[str, Any],
    prepared_digest: str,
) -> dict[str, Any]:
    run_path = path.resolve()
    run_root = run_path.parent
    run = _load_json(run_path)
    if run.get("schema") != "hephaestus.openroad-physical-run.v1":
        raise OpenROADPhysicalError(f"unsupported physical-run schema: {run_path}")
    identity = run.get("identity")
    source = run.get("source")
    toolchain = run.get("toolchain")
    artifacts = run.get("artifacts")
    normalized = run.get("normalized")
    execution = run.get("execution")
    claims = run.get("claims")
    if not all(
        isinstance(value, dict)
        for value in (
            identity,
            source,
            toolchain,
            artifacts,
            normalized,
            execution,
            claims,
        )
    ):
        raise OpenROADPhysicalError(f"physical run is malformed: {run_path}")

    backend_name = identity.get("backend")
    attempt = identity.get("attempt")
    if backend_name not in _BACKENDS:
        raise OpenROADPhysicalError(f"physical run has an unexpected backend: {backend_name!r}")
    attempts = int(prepared["contract"]["value"]["attempts_per_backend"])
    if type(attempt) is not int or attempt < 1 or attempt > attempts:
        raise OpenROADPhysicalError(f"physical run has an invalid attempt: {attempt!r}")
    expected_variant = f"attempt-{attempt:02d}"
    if identity.get("variant") != expected_variant:
        raise OpenROADPhysicalError("physical-run variant does not match its attempt")

    backend = prepared["backends"][backend_name]
    expected_source = {
        "prepared_manifest_sha256": prepared_digest,
        "registered_manifest_sha256": prepared["source"]["registered_manifest_sha256"],
        "core_sha256": backend["core_sha256"],
        "wrapper_sha256": backend["wrapper_sha256"],
        "config_sha256": backend["config_sha256"],
        "sdc_sha256": backend["sdc_sha256"],
        "rules_sha256": backend["rules_sha256"],
        "synth_compat_sha256": backend["synth_compat_sha256"],
        "sanitizer_sha256": backend["sanitizer_sha256"],
        "contract_sha256": prepared["contract"]["sha256"],
    }
    if source != expected_source:
        raise OpenROADPhysicalError(
            f"physical run source binding differs for {backend_name} attempt {attempt}"
        )
    if toolchain.get("orfs_image_repo_digest") != prepared["toolchain"]["orfs_image_repo_digest"]:
        raise OpenROADPhysicalError("physical run used a different ORFS image")
    if execution.get("returncode") != 0:
        raise OpenROADPhysicalError("physical run has a nonzero ORFS return code")

    required_claims = (
        "registered_source_binding_verified",
        "pinned_orfs_image_used",
        "placement_performed",
        "routing_performed",
        "gds_generated",
        "spef_generated",
        "metadata_generated",
    )
    if any(claims.get(name) is not True for name in required_claims):
        raise OpenROADPhysicalError("physical run lacks a required positive claim")
    forbidden_claims = (
        "post_physical_equivalence_verified",
        "drc_clean",
        "lvs_clean",
        "power_estimated_with_activity",
        "post_layout_pex_verified",
        "foundry_signoff_complete",
        "silicon_verified",
    )
    if any(claims.get(name) is not False for name in forbidden_claims):
        raise OpenROADPhysicalError("physical run overstates its claim boundary")

    required_artifacts = (
        "final_gds",
        "final_def",
        "final_verilog",
        "final_odb",
        "final_spef",
        "metadata",
    )
    resolved: dict[str, Path] = {}
    for label in required_artifacts:
        specification = artifacts.get(label)
        if not isinstance(specification, dict):
            raise OpenROADPhysicalError(f"physical run lacks artifact {label!r}")
        resolved[label] = _resolve_artifact(
            run_root,
            specification.get("path"),
            specification.get("sha256"),
            context=f"run artifact {label}",
        )
        if specification.get("size_bytes") != resolved[label].stat().st_size:
            raise OpenROADPhysicalError(f"physical artifact size drifted for {label}")

    for label in ("image_inspect", "tool_versions"):
        specification = toolchain.get(label)
        if not isinstance(specification, dict):
            raise OpenROADPhysicalError(f"physical run lacks tool artifact {label}")
        _resolve_artifact(
            run_root,
            specification.get("path"),
            specification.get("sha256"),
            context=f"run tool artifact {label}",
        )
    for label in ("stdout", "stderr", "returncode_file"):
        specification = execution.get(label)
        if not isinstance(specification, dict):
            raise OpenROADPhysicalError(f"physical run lacks execution artifact {label}")
        _resolve_artifact(
            run_root,
            specification.get("path"),
            specification.get("sha256"),
            context=f"run execution artifact {label}",
        )

    recomputed_gds = gds_timestamp_normalized_sha256(resolved["final_gds"])
    if normalized.get("gds_timestamp_normalized_sha256") != recomputed_gds:
        raise OpenROADPhysicalError("normalized GDS digest does not match the routed GDS")
    recomputed_spef = spef_date_normalized_sha256(resolved["final_spef"])
    if normalized.get("spef_date_normalized_sha256") != recomputed_spef:
        raise OpenROADPhysicalError("normalized SPEF digest does not match the final SPEF")
    recomputed_def = parse_def_metrics(resolved["final_def"])
    if normalized.get("def_metrics") != recomputed_def:
        raise OpenROADPhysicalError("normalized DEF metrics do not match the final DEF")
    recomputed_metadata = stable_metadata_metrics(_load_json(resolved["metadata"]))
    if normalized.get("stable_metadata_metrics") != recomputed_metadata:
        raise OpenROADPhysicalError("normalized ORFS metrics do not match metadata.json")
    run["_manifest_path"] = run_path
    run["_manifest_sha256"] = sha256_file(run_path)
    return run


def _repeatability_contract(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    first_artifacts = first["artifacts"]
    second_artifacts = second["artifacts"]
    byte_labels = ("final_def", "final_verilog")
    byte_identical = {
        label: first_artifacts[label]["sha256"] == second_artifacts[label]["sha256"]
        for label in byte_labels
    }
    gds_geometry_identical = (
        first["normalized"]["gds_timestamp_normalized_sha256"]
        == second["normalized"]["gds_timestamp_normalized_sha256"]
    )
    spef_parasitics_identical = (
        first["normalized"]["spef_date_normalized_sha256"]
        == second["normalized"]["spef_date_normalized_sha256"]
    )
    def_metrics_identical = (
        first["normalized"]["def_metrics"] == second["normalized"]["def_metrics"]
    )
    metadata_metrics_identical = (
        first["normalized"]["stable_metadata_metrics"]
        == second["normalized"]["stable_metadata_metrics"]
    )
    passed = (
        all(byte_identical.values())
        and gds_geometry_identical
        and spef_parasitics_identical
        and def_metrics_identical
        and metadata_metrics_identical
    )
    return {
        "passed": passed,
        "byte_identical": byte_identical,
        "gds_timestamp_normalized_identical": gds_geometry_identical,
        "spef_date_normalized_identical": spef_parasitics_identical,
        "def_metrics_identical": def_metrics_identical,
        "stable_metadata_metrics_identical": metadata_metrics_identical,
        "raw_gds_byte_identical": (
            first_artifacts["final_gds"]["sha256"] == second_artifacts["final_gds"]["sha256"]
        ),
        "raw_spef_byte_identical": (
            first_artifacts["final_spef"]["sha256"] == second_artifacts["final_spef"]["sha256"]
        ),
        "raw_odb_byte_identical": (
            first_artifacts["final_odb"]["sha256"] == second_artifacts["final_odb"]["sha256"]
        ),
    }


def bind_physical_evidence(
    prepared_path: Path,
    runs_root: Path,
    output_dir: Path,
) -> dict[str, Any]:
    """Bind six exact ORFS runs into one matched physical-evidence manifest."""

    prepared_root, prepared = _load_prepared(prepared_path)
    del prepared_root
    prepared_digest = sha256_file(prepared_path.resolve())
    root = runs_root.resolve()
    output = output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)

    manifest_paths = sorted(root.rglob("openroad_run.json"))
    expected_count = len(_BACKENDS) * int(prepared["contract"]["value"]["attempts_per_backend"])
    if len(manifest_paths) != expected_count:
        raise OpenROADPhysicalError(
            f"expected {expected_count} physical run manifests, found {len(manifest_paths)}"
        )

    runs: dict[tuple[str, int], dict[str, Any]] = {}
    for path in manifest_paths:
        run = _validate_run_manifest(
            path,
            prepared=prepared,
            prepared_digest=prepared_digest,
        )
        key = (run["identity"]["backend"], run["identity"]["attempt"])
        if key in runs:
            raise OpenROADPhysicalError(f"duplicate physical run identity: {key}")
        runs[key] = run

    attempts = int(prepared["contract"]["value"]["attempts_per_backend"])
    expected_keys = {
        (backend_name, attempt) for backend_name in _BACKENDS for attempt in range(1, attempts + 1)
    }
    if set(runs) != expected_keys:
        raise OpenROADPhysicalError("physical run set is incomplete or unexpected")

    preserved_prepared = output / "source_prepared.json"
    shutil.copyfile(prepared_path.resolve(), preserved_prepared)
    preserved_runs = output / "run_manifests"
    preserved_runs.mkdir(exist_ok=True)

    backend_evidence: dict[str, Any] = {}
    for backend_name in _BACKENDS:
        first = runs[(backend_name, 1)]
        second = runs[(backend_name, 2)]
        repeatability = _repeatability_contract(first, second)
        if not repeatability["passed"]:
            raise OpenROADPhysicalError(f"physical repeatability failed for backend {backend_name}")
        run_records: list[dict[str, Any]] = []
        for attempt, run in ((1, first), (2, second)):
            destination = preserved_runs / f"{backend_name}-attempt-{attempt:02d}.json"
            shutil.copyfile(run["_manifest_path"], destination)
            run_records.append(
                {
                    "attempt": attempt,
                    "manifest": destination.relative_to(output).as_posix(),
                    "manifest_sha256": sha256_file(destination),
                    "artifacts": run["artifacts"],
                    "normalized": run["normalized"],
                }
            )
        backend_evidence[backend_name] = {
            "core_sha256": prepared["backends"][backend_name]["core_sha256"],
            "wrapper_sha256": prepared["backends"][backend_name]["wrapper_sha256"],
            "repeatability": repeatability,
            "runs": run_records,
            "observed_physical_metrics": first["normalized"]["stable_metadata_metrics"],
            "observed_def_metrics": first["normalized"]["def_metrics"],
            "gds_timestamp_normalized_sha256": first["normalized"][
                "gds_timestamp_normalized_sha256"
            ],
        }

    die_areas = {
        tuple(evidence["observed_def_metrics"]["die_area_um"])
        for evidence in backend_evidence.values()
    }
    if len(die_areas) != 1:
        raise OpenROADPhysicalError("backends did not use one common physical die area")

    manifest = {
        "schema": "hephaestus.openroad-physical-evidence.v1",
        "evidence_level": "matched_registered_orfs_rtl_to_gds_repeatability",
        "source": {
            "prepared_manifest": preserved_prepared.name,
            "prepared_manifest_sha256": sha256_file(preserved_prepared),
            "registered_manifest_sha256": prepared["source"]["registered_manifest_sha256"],
            "registered_reference_sha256": prepared["source"]["registered_reference_sha256"],
            "probe_reference_sha256": prepared["source"]["probe_reference_sha256"],
        },
        "toolchain": prepared["toolchain"],
        "contract": prepared["contract"],
        "backends": backend_evidence,
        "claims": {
            "registered_source_binding_verified": True,
            "pinned_orfs_image_used": True,
            "all_three_backends_placed": True,
            "all_three_backends_routed": True,
            "all_three_backends_emitted_gds": True,
            "all_three_backends_emitted_spef": True,
            "two_attempts_per_backend_completed": True,
            "physical_repeatability_verified": True,
            "physical_metrics_recorded": True,
            "common_physical_boundary_verified": True,
            "post_physical_equivalence_verified": False,
            "comparative_ppa_claim_enabled": False,
            "drc_clean": False,
            "lvs_clean": False,
            "power_estimated_with_activity": False,
            "post_layout_pex_verified": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }
    write_json(output / "openroad_physical_evidence.json", manifest)
    _write_summary(output / "SUMMARY.md", manifest)
    return manifest


def _write_summary(path: Path, manifest: dict[str, Any]) -> None:
    lines = [
        "# Matched registered OpenROAD physical evidence",
        "",
        "All three registered zero-fetch backends completed two IHP SG13G2 ORFS runs under one",
        "pinned image and one common physical boundary. The table records preserved observations;",
        "it is not yet a comparative PPA claim because post-physical equivalence is a later gate.",
        "",
        "| Backend | Components | Nets | GDS geometry digest | Repeatable |",
        "|---|---:|---:|---|:---:|",
    ]
    for backend_name in _BACKENDS:
        evidence = manifest["backends"][backend_name]
        metrics = evidence["observed_def_metrics"]
        lines.append(
            "| "
            f"`{backend_name}` | {metrics['component_count']} | {metrics['net_count']} | "
            f"`{evidence['gds_timestamp_normalized_sha256']}` | "
            f"{'yes' if evidence['repeatability']['passed'] else 'no'} |"
        )
    lines.extend(
        [
            "",
            "## Claim boundary",
            "",
            "This bundle proves source binding, execution through placement and routing, physical",
            "artifact generation, one common boundary, and the declared repeatability checks.",
            "It does not prove post-physical equivalence, DRC, LVS, or activity-based power.",
            "It also does not prove validated PEX, foundry sign-off, or fabricated silicon.",
            "Comparative PPA stays disabled until routed-netlist equivalence is proved.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build digest-bound matched OpenROAD physical evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare")
    prepare.add_argument("registered_bundle", type=Path)
    prepare.add_argument("--registered-reference", type=Path, required=True)
    prepare.add_argument("--probe-reference", type=Path, required=True)
    prepare.add_argument("--contract", type=Path, required=True)
    prepare.add_argument(
        "--flow-helpers",
        type=Path,
        default=Path("flows/openroad/registered_shared_dag"),
    )
    prepare.add_argument("--out", type=Path, required=True)

    run = subparsers.add_parser("run")
    run.add_argument("prepared", type=Path)
    run.add_argument("--backend", choices=_BACKENDS, required=True)
    run.add_argument("--attempt", type=int, required=True)
    run.add_argument("--out", type=Path, required=True)
    run.add_argument("--docker", default="docker")
    run.add_argument("--timeout", type=int, default=7200)

    bind = subparsers.add_parser("bind")
    bind.add_argument("prepared", type=Path)
    bind.add_argument("--runs", type=Path, required=True)
    bind.add_argument("--out", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        if arguments.command == "prepare":
            manifest = prepare_physical_evidence(
                arguments.registered_bundle,
                arguments.registered_reference,
                arguments.probe_reference,
                arguments.contract,
                arguments.out,
                helper_dir=arguments.flow_helpers,
            )
            print(
                "prepared matched OpenROAD sources: "
                f"backends={len(manifest['backends'])} "
                f"image={manifest['toolchain']['orfs_image_repo_digest']}"
            )
        elif arguments.command == "run":
            manifest = run_physical_attempt(
                arguments.prepared,
                arguments.out,
                backend_name=arguments.backend,
                attempt=arguments.attempt,
                docker=arguments.docker,
                timeout_seconds=arguments.timeout,
            )
            print(
                "completed ORFS physical run: "
                f"backend={manifest['identity']['backend']} "
                f"attempt={manifest['identity']['attempt']}"
            )
        else:
            manifest = bind_physical_evidence(
                arguments.prepared,
                arguments.runs,
                arguments.out,
            )
            print(
                "bound matched OpenROAD evidence: "
                f"backends={len(manifest['backends'])} "
                f"repeatable={manifest['claims']['physical_repeatability_verified']}"
            )
    except (
        OpenROADPhysicalError,
        OSError,
        ValueError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
