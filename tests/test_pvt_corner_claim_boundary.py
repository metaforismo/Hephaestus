from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus import pvt_corner


def _valid_contract() -> dict[str, object]:
    return {
        "schema": "hephaestus.ihp-pvt-corner-contract.v2",
        "contract_id": "ihp-sg13g2-routed-pvt-corner-v2",
        "backends": [
            "shared_dag",
            "naive_shift_add",
            "constant_multipliers",
        ],
        "corner_order": ["slow", "typ", "fast"],
        "physical_attempts": [1, 2],
        "analysis_replays": [1, 2],
        "timeout_seconds": 30,
        "negative_control_clock_period_ns": 0.05,
        "ihp_open_pdk": {
            "repository": "https://example.invalid/pdk.git",
            "commit": "1" * 40,
            "liberty": {
                "slow": {
                    "path": "libs/slow.lib",
                    "sha256": "1" * 64,
                    "git_blob_sha": "2" * 40,
                    "nominal_voltage_v": 1.08,
                    "nominal_temperature_c": 125.0,
                },
                "typ": {
                    "path": "libs/typ.lib",
                    "sha256": "2" * 64,
                    "git_blob_sha": "3" * 40,
                    "nominal_voltage_v": 1.2,
                    "nominal_temperature_c": 25.0,
                },
                "fast": {
                    "path": "libs/fast.lib",
                    "sha256": "3" * 64,
                    "git_blob_sha": "4" * 40,
                    "nominal_voltage_v": 1.32,
                    "nominal_temperature_c": -40.0,
                },
            },
        },
        "opensta": {
            "repository": "parallaxsw/OpenSTA",
            "commit": "5" * 40,
        },
        "claim_boundary": {
            "ocv_analyzed": False,
            "aocv_analyzed": False,
            "pocv_analyzed": False,
            "statistical_variation_analyzed": False,
            "crosstalk_delay_analyzed": False,
            "ir_drop_analyzed": False,
            "electromigration_analyzed": False,
            "thermal_analyzed": False,
            "foundry_signoff_sta_performed": False,
            "foundry_signoff_complete": False,
            "silicon_verified": False,
        },
    }


def test_contract_rejects_claim_boundary_key_injection(tmp_path: Path) -> None:
    contract = _valid_contract()
    contract["claim_boundary"]["comparative_pvt_claim_enabled"] = True
    path = tmp_path / "contract.json"
    path.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        pvt_corner.PVTCornerError,
        match="exactly the supported false claims",
    ):
        pvt_corner.validate_contract(path)
