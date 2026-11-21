# SPDX-License-Identifier: MIT
"""ai-platform's test vectors (``platform/vectors/``, whose README says how to run them):
texts that every side's JSON reader takes or refuses alike, and messages that every side's
schema check finds valid or invalid alike, each within its file's ``seconds``."""

from __future__ import annotations

import json
import time
from collections import defaultdict
from functools import cache
from typing import Any

import pytest

from ai_robot.core.config import platform_dir
from ai_robot.core.data import Schema, read_json


def _file(name: str) -> dict[str, Any]:
    path = platform_dir().parent / "vectors" / f"{name}.json"
    content = json.loads(path.read_text(encoding="utf-8"))
    assert content["vectors"], path
    return content


READING = _file("reading")
VALIDATING = _file("validating")


def _bytes(vector: dict[str, Any]) -> bytes:
    """A vector's text: its ``hex``, or its ``text`` or ``parts`` in UTF-8."""
    if "hex" in vector:
        return bytes.fromhex(vector["hex"])
    if "text" in vector:
        return vector["text"].encode()
    return "".join(
        part["text"] * part.get("times", 1)
        if "text" in part
        else "".join(f"{part['before']}{n}{part['after']}" for n in range(part["times"]))
        for part in vector["parts"]
    ).encode()


def _answer(vector: dict[str, Any]) -> tuple[str, Any]:
    """How ``read_json`` answers a vector, given its bytes as a WebSocket text frame or a
    Bluetooth message brings them, and what it read; within the file's time."""
    data = _bytes(vector)
    started = time.perf_counter()
    answer, value = "read", None
    try:
        value = read_json(data)
    except ValueError:
        answer = "refused"
    assert time.perf_counter() - started <= READING["seconds"], vector["id"]
    return answer, value


def _ids(content: dict[str, Any]) -> list[Any]:
    return [pytest.param(vector, id=vector["id"]) for vector in content["vectors"]]


@pytest.mark.parametrize("vector", _ids(READING))
def test_each_text_is_read_as_every_side_reads_it(vector: dict[str, Any]) -> None:
    """``read`` and ``refused`` are ADR-0014's answers; ``either``, a receiver's choice. A
    vector's ``depth`` is past 64, as deep as the robot reads (``JSON_DEPTH``), so it keeps
    its result here."""
    answer, value = _answer(vector)
    if vector["result"] == "either":
        return
    assert answer == vector["result"]
    if answer == "read":
        # What the platform's standard parser returns, number types included.
        expected = json.loads(_bytes(vector).decode())
        assert json.dumps(value) == json.dumps(expected)


def test_each_group_of_texts_is_answered_alike() -> None:
    """A receiver may refuse a number past a double, for one, but in every spelling or none."""
    groups: dict[str, set[str]] = defaultdict(set)
    for vector in READING["vectors"]:
        if "group" in vector:
            groups[vector["group"]].add(_answer(vector)[0])
    assert groups
    for group, answers in groups.items():
        assert len(answers) == 1, group


@cache
def _schema(protocol: str, definition: str) -> Schema:
    """``definition`` of ``protocol``'s schema, checked as the robot checks what it
    receives: through ``Schema``, the one validator that it uses for every protocol."""
    name = f"platform/protocols/{protocol}/schema.json"
    schema = json.loads((platform_dir() / protocol / "schema.json").read_text(encoding="utf-8"))
    return Schema(name, {**schema, "$ref": f"#/$defs/{definition}"})


@pytest.mark.parametrize("vector", _ids(VALIDATING))
def test_each_message_is_checked_as_every_side_checks_it(vector: dict[str, Any]) -> None:
    schema = _schema(vector["protocol"], vector["definition"])
    data = _bytes(vector)
    started = time.perf_counter()
    problem = schema.problem(read_json(data))
    assert time.perf_counter() - started <= VALIDATING["seconds"]
    assert (problem is None) == (vector["result"] == "valid"), problem
