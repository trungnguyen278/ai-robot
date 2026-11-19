# SPDX-License-Identifier: MIT
"""The robot's MCP server: the cloud calls the robot's capabilities as MCP tools
(platform/protocols/cloud/README.md, Capabilities: MCP; docs/decisions/0008).

It does not know what carries its messages: the session hands it each MCP message from
the cloud, with the number of the control channel's message that carried it, and it
sends its own through the session.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

from ai_robot import software_version
from ai_robot.core.data import encode, plain
from ai_robot.ports.cloud import Capability, ToolCall

# Fixed by MCP's revision 2026-07-28, and by the protocol
# (platform/protocols/cloud/README.md, Capabilities: MCP).
MCP_VERSION = "2026-07-28"
SAFETY_KEY = "ai-platform/safety"  # a tool's safety class, in its _meta
VERSION_KEY = "ai-platform/version"  # a tool's version, in its _meta
SERVER_NAME = "ai-robot"  # the software that answers, in serverInfo

# JSON-RPC's and MCP's error codes
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
UNSUPPORTED_PROTOCOL_VERSION = -32022

# A result that the cloud cannot take: larger than the protocol allows, or not JSON that
# every side can hold (ai-platform's ADR-0014).
UNSENDABLE = "the result is too large for the protocol, or not JSON that every side can hold"

# How the server sends: an MCP message, and the number of the message it answers, if
# any. It says whether the message went: one that the cloud cannot take does not.
Send = Callable[[dict[str, Any], int | None], bool]


class Rejected(Exception):
    """A request the robot answers with a JSON-RPC error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class McpServer:
    """The MCP server of one session of the control channel.

    - ``catalogue`` gives the capabilities the cloud may call now, by name, and
      ``catalogue_ttl`` how long the cloud may cache them, in seconds.
    - ``called`` hands a call to the robot, and ``cancelled`` withdraws one by its
      identifier, which ``number`` keeps apart from another session's.
    """

    def __init__(
        self,
        *,
        number: int,
        catalogue: Callable[[], Mapping[str, Capability]],
        catalogue_ttl: float,
        send: Send,
        called: Callable[[ToolCall], None],
        cancelled: Callable[[str], None],
    ) -> None:
        self.number = number
        self.catalogue = catalogue
        self.catalogue_ttl = catalogue_ttl
        self._send = send
        self._called = called
        self._cancelled = cancelled
        # subscriptions/listen: key -> the request's id, and the message that carried it
        self.subscriptions: dict[str, tuple[Any, int]] = {}
        self.calls: dict[str, tuple[Any, int]] = {}  # call id -> the same

    # ---- the cloud's messages ----

    def handle(self, payload: Mapping[str, Any], seq: int) -> None:
        """An MCP message from the cloud, carried by the control channel's message
        ``seq``."""
        method = payload.get("method")
        if "id" not in payload:
            if method == "notifications/cancelled":
                self._cancel(payload.get("params"))
            return  # other notifications, and responses: the robot sends no requests
        request = payload["id"]
        valid = isinstance(request, str | int) and not isinstance(request, bool)
        try:
            if not valid:
                raise Rejected(INVALID_REQUEST, "the request id must be a string or an integer")
            if not isinstance(method, str):
                raise Rejected(INVALID_REQUEST, "the request has no method")
            handler = {
                "server/discover": self._discover,
                "tools/list": self._list,
                "tools/call": self._call,
                "subscriptions/listen": self._listen,
            }.get(method)
            if handler is None:
                raise Rejected(METHOD_NOT_FOUND, f"Method not found: {method}")
            params = payload.get("params")
            _check_meta(params)
            result = handler(request, params, seq)
        except Rejected as rejected:
            error: dict[str, Any] = {"code": rejected.code, "message": str(rejected)}
            if rejected.data is not None:
                error["data"] = rejected.data
            response: dict[str, Any] = {"jsonrpc": "2.0", "error": error}
            if valid:
                response["id"] = request
            self._send(response, seq)
            return
        if result is not None:
            result.setdefault("resultType", "complete")
            result["_meta"] = {**result.get("_meta", {}), **_server_info()}
            if not self._send({"jsonrpc": "2.0", "id": request, "result": result}, seq):
                error = {"code": INTERNAL_ERROR, "message": UNSENDABLE}
                self._send({"jsonrpc": "2.0", "id": request, "error": error}, seq)

    def _discover(self, request: Any, params: Mapping[str, Any], seq: int) -> dict[str, Any]:
        return {
            "supportedVersions": [MCP_VERSION],
            "capabilities": {"tools": {"listChanged": True}},
            "ttlMs": self._ttl_ms(),
            "cacheScope": "private",
        }

    def _list(self, request: Any, params: Mapping[str, Any], seq: int) -> dict[str, Any]:
        if params.get("cursor") is not None:
            raise Rejected(INVALID_PARAMS, "Invalid cursor: the catalogue has one page")
        tools = [_tool(capability) for _, capability in sorted(self.catalogue().items())]
        return {"tools": tools, "ttlMs": self._ttl_ms(), "cacheScope": "private"}

    def _ttl_ms(self) -> int:
        """How long the cloud may cache the catalogue: MCP's ``ttlMs``."""
        return round(self.catalogue_ttl * 1000)

    def _call(self, request: Any, params: Mapping[str, Any], seq: int) -> None:
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str):
            raise Rejected(INVALID_PARAMS, "params.name must be a string")
        if not isinstance(arguments, dict):
            raise Rejected(INVALID_PARAMS, "params.arguments must be an object")
        if name not in self.catalogue():
            raise Rejected(INVALID_PARAMS, f"Unknown tool: {name}")
        call = ToolCall(self._call_id(request), name, arguments)
        if call.id in self.calls:
            raise Rejected(INVALID_REQUEST, f"request {request!r} is already in progress")
        self.calls[call.id] = (request, seq)
        self._called(call)
        return None  # the answer comes with reply()

    def _listen(self, request: Any, params: Mapping[str, Any], seq: int) -> None:
        wanted = params.get("notifications")
        if not isinstance(wanted, dict):
            raise Rejected(INVALID_PARAMS, "params.notifications must be an object")
        honoured = {"toolsListChanged": True} if wanted.get("toolsListChanged") is True else {}
        self._send(
            {
                "jsonrpc": "2.0",
                "method": "notifications/subscriptions/acknowledged",
                "params": {
                    "_meta": {"io.modelcontextprotocol/subscriptionId": request},
                    "notifications": honoured,
                },
            },
            seq,
        )
        if honoured:
            self.subscriptions[json.dumps(request)] = (request, seq)
        return None  # the subscription stays open until it is cancelled

    def _cancel(self, params: Any) -> None:
        request = params.get("requestId") if isinstance(params, dict) else None
        if not isinstance(request, str | int) or isinstance(request, bool):
            return
        self.subscriptions.pop(json.dumps(request), None)
        call = self._call_id(request)
        if self.calls.pop(call, None) is not None:
            self._cancelled(call)

    def _call_id(self, request: Any) -> str:
        return f"{self.number}.{json.dumps(request)}"

    # ---- what the robot sends ----

    def reply(self, call: ToolCall, data: Mapping[str, Any] | None, error: str | None) -> None:
        """Answer ``call`` with the result's ``data``, or with ``error``. A call this
        session does not hold, cancelled or another session's, gets no answer."""
        pending = self.calls.pop(call.id, None)
        if pending is None:
            return
        request, seq = pending
        if error is None:
            structured = plain(data or {})
            result = {
                "content": [{"type": "text", "text": encode(structured)}],
                "structuredContent": structured,
                "isError": False,
            }
            if self._result(request, result, seq):
                return
            error = UNSENDABLE
        self._result(request, {"content": [{"type": "text", "text": error}], "isError": True}, seq)

    def _result(self, request: Any, result: dict[str, Any], seq: int) -> bool:
        result = {"resultType": "complete", **result, "_meta": _server_info()}
        return self._send({"jsonrpc": "2.0", "id": request, "result": result}, seq)

    def catalogue_changed(self) -> None:
        """Tell the subscribers that the catalogue changed."""
        for request, _ in list(self.subscriptions.values()):
            self._send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                    "params": {"_meta": {"io.modelcontextprotocol/subscriptionId": request}},
                },
                None,
            )

    def end(self) -> None:
        """End the subscriptions, as MCP has them end: with the response to the request
        that opened each. The session is about to end."""
        for request, seq in list(self.subscriptions.values()):
            meta = {"io.modelcontextprotocol/subscriptionId": request}
            result = {"resultType": "complete", "_meta": meta}
            self._send({"jsonrpc": "2.0", "id": request, "result": result}, seq)
        self.subscriptions.clear()

    def abandon(self) -> list[str]:
        """The session is over: the calls still in progress, which it can no longer
        answer."""
        calls = list(self.calls)
        self.calls.clear()
        self.subscriptions.clear()
        return calls


def _tool(capability: Capability) -> dict[str, Any]:
    """The MCP tool definition of ``capability``."""
    tool: dict[str, Any] = {"name": capability.name}
    if capability.title is not None:
        tool["title"] = capability.title
    tool["description"] = capability.description
    tool["inputSchema"] = plain(capability.input_schema)
    if capability.output_schema is not None:
        tool["outputSchema"] = plain(capability.output_schema)
    tool["_meta"] = {SAFETY_KEY: str(capability.safety), VERSION_KEY: capability.version}
    return tool


def _check_meta(params: Any) -> None:
    """Every MCP request carries its protocol version and the client's capabilities."""
    if not isinstance(params, dict):
        raise Rejected(INVALID_PARAMS, "params must be an object")
    meta = params.get("_meta")
    version = (
        meta.get("io.modelcontextprotocol/protocolVersion") if isinstance(meta, dict) else None
    )
    capabilities = (
        meta.get("io.modelcontextprotocol/clientCapabilities") if isinstance(meta, dict) else None
    )
    if not isinstance(version, str) or not isinstance(capabilities, dict):
        raise Rejected(
            INVALID_PARAMS,
            "params._meta must carry io.modelcontextprotocol/protocolVersion and "
            "io.modelcontextprotocol/clientCapabilities",
        )
    if version != MCP_VERSION:
        raise Rejected(
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {"supported": [MCP_VERSION], "requested": version},
        )


def _server_info() -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/serverInfo": {"name": SERVER_NAME, "version": software_version()}
    }
