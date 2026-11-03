# SPDX-License-Identifier: MIT
"""Plain JSON data: read-only copies, encoding and JSON Schema checks."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from datetime import date
from typing import Any

import pytest

from ai_robot.core.config import platform_dir
from ai_robot.core.data import Schema, ecma_end, encode, frozen, plain

# The protocols whose schemas the robot reads (platform/protocols/).
PROTOCOLS = ("cloud", "setup")
# Classes that Python's regular expressions read otherwise than ECMA-262's, on text. Measured
# against Node 24 on 2026-11-03: Python's \d and \w take in every script's digits and letters,
# and its \s takes U+001C to U+001F and U+0085 where ECMA-262's takes U+FEFF.
SHORTHANDS = re.compile(r"\\[dDwWsSbB]")


def test_frozen_is_a_deep_read_only_copy() -> None:
    days = [18, {"high": 19}]
    data = frozen("data", {"days": days})
    days.append(20)
    assert data["days"] == (18, {"high": 19})
    with pytest.raises(TypeError):
        data["days"][1]["high"] = 0  # type: ignore[index]


@pytest.mark.parametrize("data", [{"t": math.nan}, {(1, 2): 3}, {"day": date(2026, 10, 17)}, []])
def test_frozen_refuses_what_is_not_json(data: object) -> None:
    with pytest.raises(ValueError, match="result must"):
        frozen("result", data)  # type: ignore[arg-type]


def test_encode_and_plain_undo_frozen() -> None:
    data = frozen("data", {"a": [1, {"b": None}], "c": "é"})
    assert json.loads(encode(data)) == plain(data) == {"a": [1, {"b": None}], "c": "é"}
    with pytest.raises(ValueError):
        encode({"t": math.inf})


def test_schema_accepts_frozen_data_and_says_what_is_wrong() -> None:
    schema = Schema(
        "test",
        {
            "type": "object",
            "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
            "required": ["tags"],
        },
    )
    assert schema.problem(frozen("data", {"tags": ["a"]})) is None
    assert schema.problem({"tags": ["a", 2]}) == "tags.1: must be of type string"
    assert schema.problem({}) == "'tags' is a required property"


SECRET = "made-up-secret"


@pytest.mark.parametrize(
    ("rule", "value", "says"),
    [
        ({"type": "integer"}, SECRET, "must be of type integer"),
        ({"maxLength": 4}, SECRET, "breaks maxLength 4"),
        ({"enum": ["a", "b"]}, SECRET, 'breaks enum ["a", "b"]'),
        ({"const": "a"}, SECRET, 'breaks const "a"'),
        ({"pattern": "^[0-9]+$"}, SECRET, 'breaks pattern "^[0-9]+$"'),
        ({"not": {"type": "string"}}, SECRET, 'breaks not {"type": "string"}'),
    ],
)
def test_a_problem_names_the_rule_never_the_value(rule: object, value: str, says: str) -> None:
    schema = Schema("test", {"type": "object", "properties": {"field": rule}})
    problem = schema.problem({"field": value})
    assert problem == f"field: {says}"
    assert SECRET not in problem


def test_a_false_schema_s_problem_names_no_value() -> None:
    # jsonschema gives the error of a subschema that is false no path.
    schema = Schema("test", {"type": "object", "properties": {"field": False}})
    assert schema.problem({"field": SECRET}) == "is not allowed"


def test_a_refused_wi_fi_password_stays_out_of_the_problem() -> None:
    """The set-up session logs the problem of a message it refuses."""
    schema = json.loads((platform_dir() / "setup" / "schema.json").read_text(encoding="utf-8"))
    from_app = Schema("setup", {**schema, "$ref": "#/$defs/from_app"})
    join = {"type": "join", "seq": 4, "ssid": "Home", "password": SECRET + "x" * 200}
    problem = from_app.problem(join)
    assert problem is not None and problem.startswith("password: breaks maxLength")
    assert SECRET not in problem


def test_invalid_schemas_are_refused() -> None:
    with pytest.raises(ValueError, match="test is not a valid JSON Schema"):
        Schema("test", {"type": "thing"})


@pytest.mark.parametrize(
    ("pattern", "python"),
    [
        ("^[a-z]+$", r"^[a-z]+\Z"),
        ("^a", "^a"),
        (r"^cost\$", r"^cost\$"),  # an escaped dollar is a dollar
        (r"^a\\$", r"^a\\\Z"),  # an escaped backslash, then the end
    ],
)
def test_a_pattern_ends_where_ecma_262_ends_it(pattern: str, python: str) -> None:
    assert ecma_end(pattern) == python


def test_a_last_newline_does_not_pass_a_pattern() -> None:
    schema = Schema("test", {"type": "string", "pattern": "^[a-z]+$"})
    assert schema.problem("vi") is None
    assert schema.problem("vi\n") == 'breaks pattern "^[a-z]+$"'
    assert Schema("test", {"pattern": r"^cost\$"}).problem("cost$") is None
    # A pattern is for text only.
    assert Schema("test", {"pattern": "^[a-z]+$"}).problem(1) is None


def _patterns(schema: Any) -> Iterator[str]:
    if isinstance(schema, dict):
        for key, value in schema.items():
            if key == "pattern" and isinstance(value, str):
                yield value
            else:
                yield from _patterns(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _patterns(value)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_protocols_patterns_read_alike_in_python_and_ecma_262(protocol: str) -> None:
    """What ``ecma_end`` counts on: each pattern's only ``$`` ends it, and none uses a class
    whose meaning differs between the two."""
    schema = json.loads((platform_dir() / protocol / "schema.json").read_text(encoding="utf-8"))
    patterns = list(_patterns(schema))
    assert patterns
    for pattern in patterns:
        assert "$" not in pattern[:-1], pattern
        assert SHORTHANDS.search(pattern) is None, pattern


@pytest.mark.parametrize("pattern", [r"^\d+$", r"^\w$", r"a\sb", r"\bx"])
def test_a_shorthand_would_be_noticed(pattern: str) -> None:
    assert SHORTHANDS.search(pattern) is not None
