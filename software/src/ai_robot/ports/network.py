# SPDX-License-Identifier: MIT
"""The robot's network: the Wi-Fi networks it sees and joins. NetworkManager on the robot
(docs/decisions/0015-set-up.md)."""

from __future__ import annotations

from abc import abstractmethod
from dataclasses import dataclass
from enum import StrEnum

from ai_robot.ports.base import Port


class Security(StrEnum):
    """How a Wi-Fi network is protected, as the set-up protocol names it."""

    OPEN = "open"  # no password, encrypted (Enhanced Open) or not
    WPA2 = "wpa2"  # WPA2 Personal, or WPA2 and WPA3 Personal mixed
    WPA3 = "wpa3"  # WPA3 Personal only
    OTHER = "other"  # one the robot does not join, such as WPA Enterprise


@dataclass(frozen=True, slots=True)
class WifiNetwork:
    """A network the robot sees."""

    ssid: bytes  # its name: 1 to 32 bytes, usually but not always UTF-8
    security: Security
    signal: int  # from 0 to 100
    saved: bool  # whether the robot already knows its password


def shown_name(ssid: bytes) -> str:
    """A network's name as a person reads it: its bytes as UTF-8, what is not UTF-8
    replaced by U+FFFD, and its NUL bytes left out, which no text the robot sends may hold
    (ai-platform's ADR-0014). The owner's app shows a name so too (``shownSsid`` in
    ai-app's ``src/setup/domain/wifi.ts``, with the Encoding Standard's decoder, which
    replaces as Python's does). The robot names a network by its bytes still."""
    return ssid.decode("utf-8", errors="replace").replace("\0", "")


class NetworkState(StrEnum):
    CONNECTED = "connected"
    CONNECTING = "connecting"
    DISCONNECTED = "disconnected"


@dataclass(frozen=True, slots=True)
class NetworkStatus:
    """The network the robot is on."""

    state: NetworkState
    ssid: bytes | None = None  # when connected: the network's name
    address: str | None = None  # when connected: the robot's IP address
    internet: bool | None = None  # when connected: whether it reaches the Internet; None: unknown


class JoinFailure(StrEnum):
    """Why joining a network failed: the set-up protocol's error codes."""

    WRONG_PASSWORD = "wrong_password"
    NOT_FOUND = "not_found"
    NO_ADDRESS = "no_address"
    OTHER = "network"


class JoinError(Exception):
    """Joining a network failed, for ``failure``."""

    def __init__(self, failure: JoinFailure, message: str) -> None:
        super().__init__(message)
        self.failure = failure


class Network(Port):
    """The Wi-Fi networks the robot sees, and the one it is on."""

    @abstractmethod
    async def scan(self) -> list[WifiNetwork]:
        """Look for networks; return those seen, the strongest first."""

    @abstractmethod
    async def join(
        self,
        ssid: bytes,
        password: str | None,
        *,
        hidden: bool,
        security: Security | None,
        timeout: float,
    ) -> NetworkStatus:
        """Join the network ``ssid``, and keep it among the networks the robot knows,
        with the others. ``password`` is needed for a protected network the robot does
        not know yet. A network that does not announce itself is ``hidden``, and needs
        its ``security``. ``JoinError`` if it fails, or takes more than ``timeout``
        seconds."""

    @abstractmethod
    async def status(self) -> NetworkStatus:
        """The network the robot is on now."""
