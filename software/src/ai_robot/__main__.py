# SPDX-License-Identifier: MIT
"""Command line: ``ai-robot`` or ``python -m ai_robot``."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import math
import os
import signal
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ai_robot import software_version
from ai_robot.app import App
from ai_robot.core.config import CONFIG_DIR_ENV, ConfigError, get_table, load_config

PROFILE_ENV = "AI_ROBOT_PROFILE"
EXIT_INTERRUPTED = 130  # 128 + SIGINT: how shells report a command stopped by Ctrl+C
LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

log = logging.getLogger("ai_robot")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the robot; return the exit status (0: stopped normally, 1: error, 2: bad
    configuration, 130: interrupted)."""
    args = _parse_args(argv)

    def build(config: Mapping[str, Any]) -> App:
        app = App(config)
        app.build()
        return app

    try:
        config = load_config(args.profile, args.config_dir)
        level = _log_level(args.log_level or get_table(config, "log").get("level", "INFO"))
        app = build(config)
    except ConfigError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    logging.basicConfig(level=level, format=LOG_FORMAT)
    log.info("starting %r with profile %r", app.robot.display_name, args.profile)
    try:
        asyncio.run(
            _run(app, args.duration, lambda: build(load_config(args.profile, args.config_dir)))
        )
    except KeyboardInterrupt:
        # A second Ctrl+C, or the first one where signal handlers are unavailable
        # (Windows; the robot has then already been stopped).
        return EXIT_INTERRUPTED
    except Exception:
        log.exception("stopped by an error")
        return 1
    return 0


async def _run(app: App, duration: float | None, rebuild: Callable[[], App]) -> None:
    """Run ``app`` until stopped. When it stops to restart, as after a factory reset,
    run a new one, built from the configuration and files as they are now."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def on_signal(sig: signal.Signals) -> None:
        log.info("%s received: stopping (send it again to force quit)", sig.name)
        stop.set()
        loop.remove_signal_handler(sig)  # the next one gets the default behavior

    for sig in (signal.SIGINT, signal.SIGTERM):
        # Unavailable on Windows and outside the main thread.
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, on_signal, sig)
    if duration is not None:
        loop.call_later(duration, stop.set)
    await app.run(stop)
    while app.restart and not stop.is_set():
        log.info("starting again")
        app = rebuild()
        await app.run(stop)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="ai-robot", description="Run the robot.")
    parser.add_argument(
        "--profile",
        default=os.environ.get(PROFILE_ENV, "desktop"),
        help=f"configuration profile (default: ${PROFILE_ENV}, or desktop)",
    )
    parser.add_argument(
        "--config-dir",
        type=Path,
        help=f"configuration directory (default: ${CONFIG_DIR_ENV}, or software/config)",
    )
    parser.add_argument(
        "--duration", type=_seconds, metavar="SECONDS", help="stop after this many seconds"
    )
    parser.add_argument(
        "--log-level",
        type=str.upper,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="override the level set in the configuration",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {software_version()}")
    return parser.parse_args(argv)


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not (math.isfinite(value) and value >= 0):
        raise argparse.ArgumentTypeError(f"expected a number of seconds, got {text!r}")
    return value


def _log_level(name: object) -> int:
    level = logging.getLevelNamesMapping().get(str(name).upper())
    if level is None:
        raise ConfigError(f"log.level: unknown level {name!r}")
    return level


if __name__ == "__main__":
    sys.exit(main())
