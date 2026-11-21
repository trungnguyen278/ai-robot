# SPDX-License-Identifier: MIT
"""Plain JSON data: read-only copies, encoding and JSON Schema checks."""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator
from datetime import date
from types import MappingProxyType
from typing import Any

import pytest

from ai_robot.core.config import platform_dir
from ai_robot.core.data import (
    JSON_DEPTH,
    Schema,
    ecma_end,
    encode,
    frozen,
    plain,
    read_json,
    write_json,
)

# The protocols whose schemas the robot reads (platform/protocols/).
PROTOCOLS = ("cloud", "setup")
# Classes that Python's regular expressions read otherwise than ECMA-262's, on text. Measured
# against Node 24 on 2026-11-03: Python's \d and \w take in every script's digits and letters,
# and its \s takes U+001C to U+001F and U+0085 where ECMA-262's takes U+FEFF.
SHORTHANDS = re.compile(r"\\[dDwWsSbB]")
# How long reading a text of 1 MiB may take, in seconds: about a tenth on this computer,
# where reading it in time that grew with the square of its length took hours on the Pi.
QUICK = 2.0
MEBIBYTE = 1 << 20
QUOTE, BACKSLASH = '"', chr(92)


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


def nested(levels: int, value: Any = 0) -> Any:
    """``value`` in ``levels`` arrays and objects, by turns."""
    for level in range(levels):
        value = [value] if level % 2 else {"x": value}
    return value


@pytest.mark.parametrize(
    "value",
    [
        {"a": ["[" * 100 + '"' + "\\" + "{" * 100, "]"]},  # brackets in strings
        chr(0x1F600),  # a pair of surrogates, once escaped
        {"": [1, -2.5, 1e308, None, True, "x"]},
        [10**308, -(10**308)],  # integers within a double's range
        nested(JSON_DEPTH),
        [[0]] * (JSON_DEPTH * 2),  # wide, not deep
    ],
)
def test_json_every_side_can_hold_is_read(value: Any) -> None:
    for ascii_only in (True, False):
        text = json.dumps(value, ensure_ascii=ascii_only)
        assert read_json(text) == value and read_json(text.encode("utf-8")) == value


@pytest.mark.parametrize(
    ("text", "rule"),
    [
        (bytes([0xFF]), "not UTF-8"),
        ("[1,]", "not JSON"),
        ("NaN", "NaN is not JSON"),
        ("[-Infinity]", "Infinity is not JSON"),
        ("[1e999]", "past a double"),
        ("1" + "0" * 400, "past a double"),  # an integer too, which int() would take
        ("[-" + "9" * 5000 + "]", "past a double"),  # more digits than int() takes
        ('"secret\\u0000"', "U[+]0000"),
        ('{"a": "secret\\udc00"}', "lone surrogate"),
        ('{"secret\\ud800": 1}', "lone surrogate"),  # in a name
        ('["\\ud800\\ud800"]', "lone surrogate"),  # two highs are no pair
        (json.dumps(nested(JSON_DEPTH + 1)), "deeper than 64"),
        ("[" * (JSON_DEPTH + 1), "deeper than 64"),  # however far a parser would get
        # A repeated name, whose first value json would drop unseen: a U+0000, a lone
        # surrogate, or one in a name or an array of the value dropped.
        ('{"secret": "\\u0000", "secret": "ok"}', "repeats a name"),
        ('[{"a": "\\ud800", "a": 1}]', "repeats a name"),
        ('{"a": {"secret\\u0000": 1}, "a": 1}', "repeats a name"),
        ('{"a": {"b": ["\\u0000"], "b": 1}}', "repeats a name"),
        ('{"a": 1, "b": 2, "a": 1}', "repeats a name"),  # the same value, apart
    ],
)
def test_json_that_not_every_side_can_hold_is_refused(text: str | bytes, rule: str) -> None:
    with pytest.raises(ValueError, match=rule) as refused:
        read_json(text)
    assert "secret" not in str(refused.value)


def test_names_that_differ_are_not_repeated() -> None:
    assert read_json('{"a": {"a": 1}, "A": [{"a": 2}, {"a": 3}]}') == {
        "a": {"a": 1},
        "A": [{"a": 2}, {"a": 3}],
    }


def test_what_is_written_every_side_can_hold() -> None:
    assert write_json(MappingProxyType({"a": (1, "x")})) == '{"a": [1, "x"]}'
    for value, rule in [
        ({"secret": "\x00"}, "U[+]0000"),
        (["secret\ud800"], "lone surrogate"),
        (nested(JSON_DEPTH + 1), "deeper than 64"),
        ([math.nan], "Out of range"),
        ({"secret": 10**400}, "past a double"),
    ]:
        with pytest.raises(ValueError, match=rule) as refused:
            write_json(value)
        assert "secret" not in str(refused.value)
    assert read_json(write_json(nested(JSON_DEPTH))) == nested(JSON_DEPTH)


# Escaped quotes in a string: each may look like its end to a reader that tries again.
ESCAPED = QUOTE + (BACKSLASH + QUOTE) * (MEBIBYTE // 2)


@pytest.mark.parametrize(
    ("text", "read"),
    [
        (ESCAPED, False),  # no closing quote
        (ESCAPED + BACKSLASH, False),  # an escape cut off
        (ESCAPED + BACKSLASH + "\n" + QUOTE, False),  # an escape JSON has not
        ("[" + ESCAPED + QUOTE + "]", True),
    ],
    ids=["unterminated", "a last backslash", "a backslash and a newline", "valid"],
)
def test_a_mebibyte_of_text_is_read_at_once(text: str, read: bool) -> None:
    """A text the size of a message is read in time that grows with its length."""
    started = time.perf_counter()
    if read:
        assert read_json(text) == [ESCAPED[1:].replace(BACKSLASH, "")]
    else:
        with pytest.raises(ValueError, match="not JSON"):
            read_json(text)
    assert time.perf_counter() - started < QUICK


UNIQUE = Schema(
    "test", {"type": "array", "uniqueItems": True, "maxItems": 3, "items": {"type": "object"}}
)


def test_unique_items_are_checked_on_an_array_the_other_rules_allow() -> None:
    assert UNIQUE.problem([{"a": 1}, {"a": 2}]) is None
    assert UNIQUE.problem([{"a": 1}, {"a": 1}]) == "breaks uniqueItems true"
    # Without maxItems and items, every array is checked.
    assert Schema("test", {"uniqueItems": True}).problem([{"a": 1}, {"a": 1}]) is not None
    assert Schema("test", {"uniqueItems": True}).problem(5) is None  # arrays only


@pytest.mark.parametrize(
    ("array", "rule"),
    [
        ([{"a": item} for item in range(4000)], "maxItems"),  # each compared with each
        ([1, 1, {"a": 1}], "type"),  # no uniqueItems before the items pass
    ],
    ids=["too many", "items that fail"],
)
def test_unique_items_wait_for_the_array_s_other_rules(array: list[Any], rule: str) -> None:
    started = time.perf_counter()
    problem = UNIQUE.problem(array)
    assert problem is not None and rule in problem
    assert time.perf_counter() - started < QUICK


def test_a_problem_is_the_first_rule_broken_found_at_once() -> None:
    """One error, the first in the schema's order. Weighing every error of a 1 MiB array
    took 4 s on a desktop PC and 2.7 s on the Pi (2026-11-21): under the vectors' 5 s, which
    do not catch it."""
    strings = Schema("test", {"type": "array", "items": {"type": "string"}, "maxItems": 3})
    objects = [{"a": item} for item in range(75_000)]
    started = time.perf_counter()
    assert strings.problem(objects) == "0: must be of type string"
    assert time.perf_counter() - started < QUICK / 4
    assert strings.problem(["a", 1, 2, 3]) == "1: must be of type string"
    # best_match still says which branch of a oneOf failed, where one alone is weighed.
    either = Schema("test", {"oneOf": [{"type": "string"}, {"type": "object", "required": ["a"]}]})
    assert either.problem({}) == "'a' is a required property"


def _unique_arrays(schema: Any) -> Iterator[dict[str, Any]]:
    if isinstance(schema, dict):
        if "uniqueItems" in schema:
            yield schema
        for value in schema.values():
            yield from _unique_arrays(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _unique_arrays(value)


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_the_protocols_bound_every_array_of_unique_items(protocol: str) -> None:
    """What ``Schema`` counts on to check ``uniqueItems`` in time: ``maxItems`` and
    ``items`` beside it, in the same schema."""
    schema = json.loads((platform_dir() / protocol / "schema.json").read_text(encoding="utf-8"))
    arrays = list(_unique_arrays(schema))
    assert arrays
    for array in arrays:
        assert "maxItems" in array and "items" in array, array
