from __future__ import annotations

import pytest

from hephaestus import pvt_corner


def _report(*, slack: str = "0.25", tns: str = "0.0") -> str:
    status = "VIOLATED" if float(slack) < 0 else "MET"
    return "\n".join(
        [
            "HEPHAESTUS_PVT_CORNER=slow",
            f"{slack} slack ({status})",
            f"worst slack max {slack}",
            f"tns max {tns}",
            "HEPHAESTUS_PVT_DONE=1",
            "",
        ]
    )


def test_parser_accepts_the_pinned_opensta_summary_format() -> None:
    value = pvt_corner.parse_opensta_output(
        _report(),
        "",
        expected_label="slow",
    )

    assert value == {
        "worst_setup_slack_ns": 0.25,
        "slack_status": "met",
        "total_negative_slack_ns": 0.0,
    }


def test_parser_accepts_a_consistent_pinned_violation() -> None:
    value = pvt_corner.parse_opensta_output(
        _report(slack="-0.125", tns="-0.5"),
        "",
        expected_label="slow",
    )

    assert value["slack_status"] == "violated"
    assert value["worst_setup_slack_ns"] == -0.125
    assert value["total_negative_slack_ns"] == -0.5


def test_parser_rejects_duplicate_total_negative_slack_summaries() -> None:
    stdout = _report().replace(
        "HEPHAESTUS_PVT_DONE=1",
        "tns max 0.0\nHEPHAESTUS_PVT_DONE=1",
    )

    with pytest.raises(
        pvt_corner.PVTCornerError,
        match="exactly one total-negative-slack",
    ):
        pvt_corner.parse_opensta_output(
            stdout,
            "",
            expected_label="slow",
        )


def test_parser_rejects_inconsistent_worst_slack_and_tns() -> None:
    with pytest.raises(
        pvt_corner.PVTCornerError,
        match="no negative total slack",
    ):
        pvt_corner.parse_opensta_output(
            _report(slack="-0.125", tns="0.0"),
            "",
            expected_label="slow",
        )
