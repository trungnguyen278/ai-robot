# SPDX-License-Identifier: MIT
"""Plain JSON data: read-only copies, encoding, and checks against a JSON Schema.

Data that crosses a boundary (scene data for the screen, capability arguments and
results, events for the cloud) is made of JSON values only, and checked where it is
made, so that a bad value fails at its publisher rather than inside another module.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator, Mapping
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError, ValidationError, best_match


def frozen(name: str, data: Mapping[str, Any]) -> Mapping[str, Any]:
    """A deep, read-only copy of ``data``, made of JSON values only.

    Mappings become read-only mappings and lists become tuples, so that nobody can
    change what others hold. Raises ``ValueError``, naming ``name``, for data that does
    not convert to JSON: keys that are not strings, numbers that are not finite (JSON has
    no NaN), other objects such as dates.
    """
    if not isinstance(data, Mapping):
        raise ValueError(f"{name} must be a mapping, got {type(data).__name__}")
    try:
        text = json.dumps(data, allow_nan=False, default=_thaw)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must convert to JSON: {error}") from None
    return _freeze(json.loads(text))


def encode(value: Any) -> str:
    """The JSON text of ``value``. Read-only mappings encode as objects and tuples as
    arrays; NaN is refused, as JSON has none and the receiver would drop the message."""
    return json.dumps(value, allow_nan=False, default=_thaw)


def plain(value: Any) -> Any:
    """``value`` with read-only mappings and tuples turned back into dicts and lists."""
    return json.loads(encode(value))


# Fixed by ai-platform's ADR-0014, for every protocol it holds: arrays and objects nest at
# most this deep, the outermost counted.
JSON_DEPTH = 64

# What nests in a JSON text, found past its strings: a string, or a bracket.
_NESTING = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[\[\]{}]')
# What no side can hold in a name or a string (ADR-0014): U+0000, and a lone surrogate,
# which UTF-8 cannot encode. ``json`` joins an escaped pair into the code point it spells.
_UNHOLDABLE = re.compile(r"[\x00\ud800-\udfff]")


def read_json(text: str | bytes) -> Any:
    """``text``, UTF-8 if it is bytes, as JSON that every side can hold (ai-platform's
    ADR-0014): ``ValueError`` for a text that is not JSON, NaN and Infinity included, that
    nests deeper than ``JSON_DEPTH``, that holds a U+0000 or a lone surrogate, or a number
    past a double. The nesting is counted before the text is parsed, so that the answer
    depends on the text, not on where the parser gives up. The error names the rule
    broken, never the text."""
    if isinstance(text, bytes):
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("the text is not UTF-8") from None
    depth = 0
    for token in _NESTING.finditer(text):
        bracket = token.group()
        if bracket in ("[", "{"):
            depth += 1
            if depth > JSON_DEPTH:
                raise ValueError(f"the text nests deeper than {JSON_DEPTH}")
        elif bracket in ("]", "}"):
            depth -= 1
    try:
        value = json.loads(text, parse_constant=_no_constant, parse_float=_double)
    except json.JSONDecodeError:
        raise ValueError("the text is not JSON") from None
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
            pending.extend(item.keys())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str) and _UNHOLDABLE.search(item):
            raise ValueError("the text holds a U+0000 or a lone surrogate")
    return value


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _double(literal: str) -> float:
    number = float(literal)
    if not math.isfinite(number):
        raise ValueError("a number is past a double")
    return number


def _thaw(value: Any) -> Any:
    """For ``json.dumps``: read-only mappings convert like dicts; nothing else does."""
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError(f"{type(value).__name__} is not JSON")


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def ecma_end(pattern: str) -> str:
    r"""``pattern`` for Python's ``re``, whose ``$`` also matches before a last newline:
    JSON Schema's patterns are ECMA-262's, whose ``$`` matches at the very end only, as
    Python's ``\Z`` does. Only a ``$`` that ends the pattern, unescaped, is an anchor here;
    the protocols' patterns have no other (tests/test_data.py)."""
    body = pattern.removesuffix("$")
    if body == pattern:
        return pattern
    backslashes = len(body) - len(body.rstrip("\\"))
    return pattern if backslashes % 2 else body + r"\Z"


def _pattern(
    validator: Any, pattern: str, instance: Any, _schema: Mapping[str, Any]
) -> Iterator[ValidationError]:
    """JSON Schema's ``pattern``, with its end read as ECMA-262 reads it."""
    if validator.is_type(instance, "string") and not re.search(ecma_end(pattern), instance):
        yield ValidationError("does not match the pattern")


# Read-only mappings and tuples pass as JSON objects and arrays, and patterns end where
# ECMA-262's end.
_Validator = validators.extend(
    Draft202012Validator,
    {"pattern": _pattern},
    type_checker=Draft202012Validator.TYPE_CHECKER.redefine_many(
        {
            "object": lambda _, value: isinstance(value, Mapping),
            "array": lambda _, value: isinstance(value, list | tuple),
        }
    ),
)


class Schema:
    """A JSON Schema (2020-12 unless it names another dialect), checked when it is
    made: ``ValueError`` names ``name`` if it is not a valid schema."""

    def __init__(self, name: str, schema: Mapping[str, Any]) -> None:
        self.schema = plain(schema)
        try:
            _Validator.check_schema(self.schema)
        except SchemaError as error:
            raise ValueError(f"{name} is not a valid JSON Schema: {error.message}") from None
        self._validator = _Validator(self.schema)

    def problem(self, instance: Any) -> str | None:
        """What is wrong with ``instance``, or ``None`` if it matches: where, and the rule
        it breaks, never the value, which may be a password or a token."""
        error = best_match(self._validator.iter_errors(instance))
        return None if error is None else _describe(error)


def _describe(error: ValidationError) -> str:
    """Where ``error`` is and which rule it breaks: jsonschema's own messages quote the
    value, and problems are logged and sent."""
    where = ".".join(str(part) for part in error.absolute_path)
    match error.validator:
        case "required":
            # It quotes the missing property's name, which the schema defines.
            rule = error.message
        case "type":
            rule = f"must be of type {error.validator_value}"
        case None:
            # A subschema that is false.
            rule = "is not allowed"
        case _:
            rule = f"breaks {error.validator} {json.dumps(error.validator_value)}"
    return f"{where}: {rule}" if where else rule
