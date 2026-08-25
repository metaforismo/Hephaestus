"""Strict SPEF parsing and order-independent RC-network canonicalization."""

from __future__ import annotations

import json
import re
import shlex
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from ._common import SPEFSemanticError, _sha256_bytes

_CAPACITANCE_RELATIVE_TOLERANCE = Decimal("1e-5")
_CAPACITANCE_ABSOLUTE_TOLERANCE_PF = Decimal("1e-12")
_NAME_REFERENCE_RE = re.compile(r"\*(?P<index>[0-9]+)(?P<suffix>.*)")
_NAME_MAP_KEY_RE = re.compile(r"\*[0-9]+")
_ESCAPED_CHARACTER_RE = re.compile(r"\\(.)")
_HEADER_DIRECTIVES = {
    "*SPEF",
    "*DESIGN",
    "*DATE",
    "*VENDOR",
    "*PROGRAM",
    "*VERSION",
    "*DESIGN_FLOW",
    "*DIVIDER",
    "*DELIMITER",
    "*BUS_DELIMITER",
    "*T_UNIT",
    "*C_UNIT",
    "*R_UNIT",
    "*L_UNIT",
}
_REQUIRED_HEADERS = _HEADER_DIRECTIVES
_DIRECTION_VALUES = {"I", "O", "B"}
_TIME_TO_NS = {
    "S": Decimal("1e9"),
    "MS": Decimal("1e6"),
    "US": Decimal("1e3"),
    "NS": Decimal("1"),
    "PS": Decimal("1e-3"),
    "FS": Decimal("1e-6"),
}
_CAPACITANCE_TO_PF = {
    "F": Decimal("1e12"),
    "MF": Decimal("1e9"),
    "UF": Decimal("1e6"),
    "NF": Decimal("1e3"),
    "PF": Decimal("1"),
    "FF": Decimal("1e-3"),
}
_RESISTANCE_TO_OHM = {
    "OHM": Decimal("1"),
    "KOHM": Decimal("1e3"),
}
_INDUCTANCE_TO_HENRY = {
    "HENRY": Decimal("1"),
    "MH": Decimal("1e-3"),
    "UH": Decimal("1e-6"),
    "NH": Decimal("1e-9"),
    "PH": Decimal("1e-12"),
}


def _canonical_decimal(value: Decimal) -> str:
    if value == 0:
        return "0"
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _parse_decimal(token: str, *, context: str, nonnegative: bool = True) -> Decimal:
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise SPEFSemanticError(f"{context} is not numeric: {token!r}") from exc
    if not value.is_finite():
        raise SPEFSemanticError(f"{context} must be finite")
    if nonnegative and value < 0:
        raise SPEFSemanticError(f"{context} must be non-negative")
    return value


def _unescape_name(value: str) -> str:
    return _ESCAPED_CHARACTER_RE.sub(r"\1", value)


def _resolve_name(token: str, name_map: dict[str, str], *, context: str) -> str:
    match = _NAME_REFERENCE_RE.fullmatch(token)
    if match is None:
        return _unescape_name(token)
    key = f"*{match.group('index')}"
    mapped = name_map.get(key)
    if mapped is None:
        raise SPEFSemanticError(f"{context} uses undefined name-map entry {key}")
    return _unescape_name(mapped + match.group("suffix"))


def _header_tokens(line: str, *, line_number: int) -> list[str]:
    try:
        return shlex.split(line, posix=True)
    except ValueError as exc:
        raise SPEFSemanticError(f"line {line_number}: malformed quoted SPEF header: {exc}") from exc


def _parse_unit(
    tokens: list[str],
    *,
    directive: str,
    factors: dict[str, Decimal],
    canonical_name: str,
    line_number: int,
) -> tuple[str, Decimal]:
    if len(tokens) != 3:
        raise SPEFSemanticError(f"line {line_number}: {directive} must contain multiplier and unit")
    multiplier = _parse_decimal(
        tokens[1],
        context=f"line {line_number} {directive} multiplier",
        nonnegative=False,
    )
    if multiplier <= 0:
        raise SPEFSemanticError(f"line {line_number}: {directive} multiplier must be positive")
    unit = tokens[2].upper()
    factor = factors.get(unit)
    if factor is None:
        raise SPEFSemanticError(f"line {line_number}: unsupported {directive} unit {unit!r}")
    return canonical_name, multiplier * factor


def _normalized_lines(text: str) -> list[tuple[int, str]]:
    records: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text.splitlines(), start=1):
        value = raw.strip()
        if not value or value.startswith("//"):
            continue
        records.append((line_number, value))
    if not records:
        raise SPEFSemanticError("SPEF document is empty")
    return records


def _parse_header(
    records: list[tuple[int, str]],
) -> tuple[int, dict[str, Any], dict[str, str], dict[str, Decimal]]:
    headers: dict[str, Any] = {}
    units: dict[str, Decimal] = {}
    index = 0
    while index < len(records):
        line_number, line = records[index]
        if line == "*NAME_MAP":
            break
        tokens = _header_tokens(line, line_number=line_number)
        directive = tokens[0] if tokens else ""
        if directive not in _HEADER_DIRECTIVES:
            raise SPEFSemanticError(
                f"line {line_number}: unsupported SPEF header directive {directive!r}"
            )
        if directive in headers:
            raise SPEFSemanticError(
                f"line {line_number}: duplicate SPEF header directive {directive}"
            )
        if directive in {"*T_UNIT", "*C_UNIT", "*R_UNIT", "*L_UNIT"}:
            if directive == "*T_UNIT":
                name, factor = _parse_unit(
                    tokens,
                    directive=directive,
                    factors=_TIME_TO_NS,
                    canonical_name="time_ns_per_unit",
                    line_number=line_number,
                )
            elif directive == "*C_UNIT":
                name, factor = _parse_unit(
                    tokens,
                    directive=directive,
                    factors=_CAPACITANCE_TO_PF,
                    canonical_name="capacitance_pf_per_unit",
                    line_number=line_number,
                )
            elif directive == "*R_UNIT":
                name, factor = _parse_unit(
                    tokens,
                    directive=directive,
                    factors=_RESISTANCE_TO_OHM,
                    canonical_name="resistance_ohm_per_unit",
                    line_number=line_number,
                )
            else:
                name, factor = _parse_unit(
                    tokens,
                    directive=directive,
                    factors=_INDUCTANCE_TO_HENRY,
                    canonical_name="inductance_henry_per_unit",
                    line_number=line_number,
                )
            units[name] = factor
            headers[directive] = tokens[1:]
        elif directive == "*DESIGN_FLOW":
            if len(tokens) < 2:
                raise SPEFSemanticError(f"line {line_number}: *DESIGN_FLOW must not be empty")
            headers[directive] = tokens[1:]
        else:
            if len(tokens) != 2:
                raise SPEFSemanticError(
                    f"line {line_number}: {directive} must contain exactly one value"
                )
            headers[directive] = tokens[1]
        index += 1

    if index >= len(records) or records[index][1] != "*NAME_MAP":
        raise SPEFSemanticError("SPEF document is missing *NAME_MAP")
    missing = sorted(_REQUIRED_HEADERS - set(headers))
    if missing:
        raise SPEFSemanticError(f"SPEF document is missing headers: {missing}")
    if headers["*SPEF"] != "ieee 1481-1999":
        raise SPEFSemanticError("unsupported SPEF standard")
    if not headers["*DESIGN"]:
        raise SPEFSemanticError("SPEF design name is empty")
    delimiters = {
        "divider": str(headers["*DIVIDER"]),
        "delimiter": str(headers["*DELIMITER"]),
        "bus_delimiter": str(headers["*BUS_DELIMITER"]),
    }
    if any(len(value) == 0 for value in delimiters.values()):
        raise SPEFSemanticError("SPEF delimiter contract is empty")
    return index + 1, headers, delimiters, units


def _parse_name_map(
    records: list[tuple[int, str]],
    index: int,
) -> tuple[int, dict[str, str]]:
    name_map: dict[str, str] = {}
    while index < len(records):
        line_number, line = records[index]
        if line == "*PORTS":
            break
        parts = line.split(maxsplit=1)
        if len(parts) != 2 or _NAME_MAP_KEY_RE.fullmatch(parts[0]) is None:
            raise SPEFSemanticError(f"line {line_number}: malformed *NAME_MAP entry")
        key, value = parts
        if key in name_map:
            raise SPEFSemanticError(f"line {line_number}: duplicate *NAME_MAP key {key}")
        if not value:
            raise SPEFSemanticError(f"line {line_number}: empty *NAME_MAP value for {key}")
        name_map[key] = value
        index += 1
    if index >= len(records) or records[index][1] != "*PORTS":
        raise SPEFSemanticError("SPEF document is missing *PORTS")
    if not name_map:
        raise SPEFSemanticError("SPEF *NAME_MAP is empty")
    return index + 1, name_map


def _parse_ports(
    records: list[tuple[int, str]],
    index: int,
    name_map: dict[str, str],
) -> tuple[int, list[list[str]]]:
    ports: list[list[str]] = []
    seen: set[str] = set()
    while index < len(records):
        line_number, line = records[index]
        if line.startswith("*D_NET "):
            break
        tokens = line.split()
        if len(tokens) != 2 or tokens[1] not in _DIRECTION_VALUES:
            raise SPEFSemanticError(f"line {line_number}: malformed *PORTS entry")
        name = _resolve_name(tokens[0], name_map, context=f"line {line_number} port")
        if name in seen:
            raise SPEFSemanticError(f"line {line_number}: duplicate port {name!r}")
        seen.add(name)
        ports.append([name, tokens[1]])
        index += 1
    if not ports:
        raise SPEFSemanticError("SPEF *PORTS section is empty")
    if index >= len(records):
        raise SPEFSemanticError("SPEF document contains no *D_NET sections")
    ports.sort()
    return index, ports


def _parse_connection(
    line: str,
    *,
    line_number: int,
    name_map: dict[str, str],
) -> list[str]:
    tokens = line.split()
    if len(tokens) < 3 or tokens[0] not in {"*P", "*I"}:
        raise SPEFSemanticError(f"line {line_number}: malformed *CONN entry")
    if tokens[2] not in _DIRECTION_VALUES:
        raise SPEFSemanticError(f"line {line_number}: invalid connection direction")
    node = _resolve_name(tokens[1], name_map, context=f"line {line_number} connection")
    if tokens[0] == "*P":
        if len(tokens) != 3:
            raise SPEFSemanticError(f"line {line_number}: unsupported port-connection attributes")
        return ["port", node, tokens[2]]
    if len(tokens) != 5 or tokens[3] != "*D":
        raise SPEFSemanticError(f"line {line_number}: instance connection must bind one cell type")
    cell = _resolve_name(tokens[4], name_map, context=f"line {line_number} cell type")
    return ["instance", node, tokens[2], cell]


def _parse_net(
    records: list[tuple[int, str]],
    index: int,
    name_map: dict[str, str],
    units: dict[str, Decimal],
) -> tuple[int, dict[str, Any], dict[str, Any]]:
    line_number, line = records[index]
    tokens = line.split()
    if len(tokens) != 3 or tokens[0] != "*D_NET":
        raise SPEFSemanticError(f"line {line_number}: expected *D_NET")
    name = _resolve_name(tokens[1], name_map, context=f"line {line_number} net")
    cap_factor = units["capacitance_pf_per_unit"]
    resistance_factor = units["resistance_ohm_per_unit"]
    declared = (
        _parse_decimal(
            tokens[2],
            context=f"line {line_number} declared capacitance",
        )
        * cap_factor
    )
    index += 1

    if index >= len(records) or records[index][1] != "*CONN":
        raise SPEFSemanticError(f"net {name!r} is missing *CONN")
    index += 1
    connections: list[list[str]] = []
    connection_nodes: set[str] = set()
    while index < len(records) and records[index][1] != "*CAP":
        conn_line_number, conn_line = records[index]
        if conn_line.startswith("*") and not conn_line.startswith(("*P ", "*I ")):
            raise SPEFSemanticError(f"line {conn_line_number}: unexpected directive before *CAP")
        connection = _parse_connection(
            conn_line,
            line_number=conn_line_number,
            name_map=name_map,
        )
        connections.append(connection)
        connection_nodes.add(connection[1])
        index += 1
    if not connections:
        raise SPEFSemanticError(f"net {name!r} has no connections")
    if index >= len(records) or records[index][1] != "*CAP":
        raise SPEFSemanticError(f"net {name!r} is missing *CAP")
    index += 1

    ground_caps: list[list[str]] = []
    coupling_caps: list[list[str]] = []
    cap_nodes: set[str] = set()
    cap_ids: set[int] = set()
    ground_sum = Decimal(0)
    coupling_sum = Decimal(0)
    while index < len(records) and records[index][1] != "*RES":
        cap_line_number, cap_line = records[index]
        cap_tokens = cap_line.split()
        if len(cap_tokens) not in {3, 4}:
            raise SPEFSemanticError(f"line {cap_line_number}: malformed *CAP entry")
        try:
            cap_id = int(cap_tokens[0])
        except ValueError as exc:
            raise SPEFSemanticError(
                f"line {cap_line_number}: capacitance index is not an integer"
            ) from exc
        if cap_id <= 0 or cap_id in cap_ids:
            raise SPEFSemanticError(
                f"line {cap_line_number}: invalid or duplicate capacitance index"
            )
        cap_ids.add(cap_id)
        if len(cap_tokens) == 3:
            node = _resolve_name(
                cap_tokens[1],
                name_map,
                context=f"line {cap_line_number} ground capacitance node",
            )
            value = (
                _parse_decimal(
                    cap_tokens[2],
                    context=f"line {cap_line_number} ground capacitance",
                )
                * cap_factor
            )
            cap_nodes.add(node)
            ground_sum += value
            ground_caps.append([node, _canonical_decimal(value)])
        else:
            lhs = _resolve_name(
                cap_tokens[1],
                name_map,
                context=f"line {cap_line_number} coupling node",
            )
            rhs = _resolve_name(
                cap_tokens[2],
                name_map,
                context=f"line {cap_line_number} coupling node",
            )
            if lhs == rhs:
                raise SPEFSemanticError(
                    f"line {cap_line_number}: coupling capacitance is self-connected"
                )
            value = (
                _parse_decimal(
                    cap_tokens[3],
                    context=f"line {cap_line_number} coupling capacitance",
                )
                * cap_factor
            )
            cap_nodes.update((lhs, rhs))
            coupling_sum += value
            first, second = sorted((lhs, rhs))
            coupling_caps.append([first, second, _canonical_decimal(value)])
        index += 1
    if not cap_ids:
        raise SPEFSemanticError(f"net {name!r} has no capacitances")
    if index >= len(records) or records[index][1] != "*RES":
        raise SPEFSemanticError(f"net {name!r} is missing *RES")
    index += 1

    resistances: list[list[str]] = []
    resistance_ids: set[int] = set()
    resistance_sum = Decimal(0)
    resistance_nodes: set[str] = set()
    while index < len(records) and records[index][1] != "*END":
        res_line_number, res_line = records[index]
        res_tokens = res_line.split()
        if len(res_tokens) != 4:
            raise SPEFSemanticError(f"line {res_line_number}: malformed *RES entry")
        try:
            resistance_id = int(res_tokens[0])
        except ValueError as exc:
            raise SPEFSemanticError(
                f"line {res_line_number}: resistance index is not an integer"
            ) from exc
        if resistance_id <= 0 or resistance_id in resistance_ids:
            raise SPEFSemanticError(
                f"line {res_line_number}: invalid or duplicate resistance index"
            )
        resistance_ids.add(resistance_id)
        lhs = _resolve_name(
            res_tokens[1],
            name_map,
            context=f"line {res_line_number} resistance node",
        )
        rhs = _resolve_name(
            res_tokens[2],
            name_map,
            context=f"line {res_line_number} resistance node",
        )
        if lhs == rhs:
            raise SPEFSemanticError(f"line {res_line_number}: resistance is self-connected")
        value = (
            _parse_decimal(
                res_tokens[3],
                context=f"line {res_line_number} resistance",
            )
            * resistance_factor
        )
        resistance_sum += value
        resistance_nodes.update((lhs, rhs))
        first, second = sorted((lhs, rhs))
        resistances.append([first, second, _canonical_decimal(value)])
        index += 1
    if not resistances:
        raise SPEFSemanticError(f"net {name!r} has no resistances")
    if index >= len(records) or records[index][1] != "*END":
        raise SPEFSemanticError(f"net {name!r} is missing *END")
    index += 1

    known_nodes = connection_nodes | cap_nodes
    missing_resistance_nodes = sorted(resistance_nodes - known_nodes)
    if missing_resistance_nodes:
        raise SPEFSemanticError(
            f"net {name!r} resistances reference unknown nodes: {missing_resistance_nodes[:5]}"
        )

    observed = ground_sum + coupling_sum
    error = abs(declared - observed)
    tolerance = max(
        _CAPACITANCE_ABSOLUTE_TOLERANCE_PF,
        abs(declared) * _CAPACITANCE_RELATIVE_TOLERANCE,
    )
    if error > tolerance:
        raise SPEFSemanticError(
            f"net {name!r} declared capacitance differs from *CAP sum: "
            f"declared={_canonical_decimal(declared)} pF, "
            f"observed={_canonical_decimal(observed)} pF, "
            f"error={_canonical_decimal(error)} pF, "
            f"tolerance={_canonical_decimal(tolerance)} pF"
        )

    connections.sort()
    ground_caps.sort()
    coupling_caps.sort()
    resistances.sort()
    net = {
        "name": name,
        "declared_capacitance_pf": _canonical_decimal(declared),
        "connections": connections,
        "ground_capacitances_pf": ground_caps,
        "coupling_capacitances_pf": coupling_caps,
        "resistances_ohm": resistances,
    }
    metrics = {
        "connection_count": len(connections),
        "ground_capacitance_count": len(ground_caps),
        "coupling_capacitance_count": len(coupling_caps),
        "resistance_count": len(resistances),
        "declared_capacitance_pf": declared,
        "ground_capacitance_pf": ground_sum,
        "coupling_capacitance_pf": coupling_sum,
        "resistance_ohm": resistance_sum,
        "declared_capacitance_error_pf": error,
        "nodes": connection_nodes | cap_nodes | resistance_nodes,
    }
    return index, net, metrics


def _parse_document(text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    records = _normalized_lines(text)
    index, headers, delimiters, units = _parse_header(records)
    index, name_map = _parse_name_map(records, index)
    index, ports = _parse_ports(records, index, name_map)

    nets: list[dict[str, Any]] = []
    net_names: set[str] = set()
    all_nodes: set[str] = set()
    totals = {
        "connection_count": 0,
        "ground_capacitance_count": 0,
        "coupling_capacitance_count": 0,
        "resistance_count": 0,
        "declared_capacitance_pf": Decimal(0),
        "ground_capacitance_pf": Decimal(0),
        "coupling_capacitance_pf": Decimal(0),
        "resistance_ohm": Decimal(0),
        "declared_capacitance_error_pf": Decimal(0),
    }
    while index < len(records):
        index, net, metrics = _parse_net(records, index, name_map, units)
        if net["name"] in net_names:
            raise SPEFSemanticError(f"duplicate *D_NET name {net['name']!r}")
        net_names.add(net["name"])
        nets.append(net)
        all_nodes.update(metrics["nodes"])
        for key in (
            "connection_count",
            "ground_capacitance_count",
            "coupling_capacitance_count",
            "resistance_count",
        ):
            totals[key] += metrics[key]
        for key in (
            "declared_capacitance_pf",
            "ground_capacitance_pf",
            "coupling_capacitance_pf",
            "resistance_ohm",
        ):
            totals[key] += metrics[key]
        totals["declared_capacitance_error_pf"] = max(
            totals["declared_capacitance_error_pf"],
            metrics["declared_capacitance_error_pf"],
        )
    if not nets:
        raise SPEFSemanticError("SPEF document contains no routed nets")
    nets.sort(key=lambda value: value["name"])

    unit_contract = {name: _canonical_decimal(value) for name, value in sorted(units.items())}
    canonical = {
        "spef_standard": headers["*SPEF"],
        "design": headers["*DESIGN"],
        "design_flow": headers["*DESIGN_FLOW"],
        "delimiters": delimiters,
        "unit_contract": unit_contract,
        "ports": ports,
        "nets": nets,
    }
    canonical_bytes = json.dumps(
        canonical,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    summary = {
        "canonical_sha256": _sha256_bytes(canonical_bytes),
        "design": headers["*DESIGN"],
        "spef_standard": headers["*SPEF"],
        "design_flow": headers["*DESIGN_FLOW"],
        "producer": {
            "vendor": headers["*VENDOR"],
            "program": headers["*PROGRAM"],
            "version": headers["*VERSION"],
            "date": headers["*DATE"],
        },
        "delimiters": delimiters,
        "unit_contract": unit_contract,
        "metrics": {
            "name_map_entry_count": len(name_map),
            "port_count": len(ports),
            "net_count": len(nets),
            "node_count": len(all_nodes),
            "connection_count": totals["connection_count"],
            "ground_capacitance_count": totals["ground_capacitance_count"],
            "coupling_capacitance_count": totals["coupling_capacitance_count"],
            "resistance_count": totals["resistance_count"],
            "total_declared_capacitance_pf": _canonical_decimal(totals["declared_capacitance_pf"]),
            "total_ground_capacitance_pf": _canonical_decimal(totals["ground_capacitance_pf"]),
            "total_coupling_capacitance_pf": _canonical_decimal(totals["coupling_capacitance_pf"]),
            "total_resistance_ohm": _canonical_decimal(totals["resistance_ohm"]),
            "max_declared_capacitance_error_pf": _canonical_decimal(
                totals["declared_capacitance_error_pf"]
            ),
        },
    }
    return canonical, summary


def parse_spef_text(text: str) -> dict[str, Any]:
    """Parse one SPEF string and return its canonical semantic summary."""

    _, summary = _parse_document(text)
    return summary


def parse_spef(path: str | Path) -> dict[str, Any]:
    """Parse one UTF-8 SPEF file and return its canonical semantic summary."""

    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SPEFSemanticError(f"cannot read SPEF file {source}: {exc}") from exc
    return parse_spef_text(text)


def parser_contract() -> dict[str, Any]:
    """Return the stable parser and consistency-check contract."""

    return {
        "spef_standard": "ieee 1481-1999",
        "canonicalization": (
            "resolved-name-map, sorted ports/nets/connections/capacitances/resistances, "
            "base-unit normalized numeric values, semantic design flow retained, "
            "producer metadata excluded"
        ),
        "declared_capacitance_relative_tolerance": _canonical_decimal(
            _CAPACITANCE_RELATIVE_TOLERANCE
        ),
        "declared_capacitance_absolute_tolerance_pf": _canonical_decimal(
            _CAPACITANCE_ABSOLUTE_TOLERANCE_PF
        ),
        "connection_forms": ["*P node direction", "*I node direction *D cell"],
        "capacitance_forms": ["ground", "coupling"],
        "resistance_form": "undirected edge",
    }
