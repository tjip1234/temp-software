"""``tjiptemp-sim`` -- run one or more virtual boards.

Examples::

    tjiptemp-sim                       # one board on TCP 127.0.0.1:3737
    tjiptemp-sim --boards 3            # three boards on 3737, 3738, 3739
    tjiptemp-sim --pty                 # a serial device the app opens like real USB
    tjiptemp-sim --scenario oven --rate 100
    tjiptemp-sim --fault max31856      # start with the thermocouple open-circuit
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys

from ..protocol.messages import DEFAULT_TCP_PORT
from .board import SimulatedBoard
from .server import SimulatorRuntime, serve_pty, serve_tcp


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tjiptemp-sim", description="Virtual TjipTemp board")
    p.add_argument("--host", default="127.0.0.1", help="TCP bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=DEFAULT_TCP_PORT, help="TCP port of the first board")
    p.add_argument("--boards", type=int, default=1, help="how many boards to simulate")
    p.add_argument("--pty", action="store_true",
                   help="also expose each board as a pty (POSIX only), for the USB code path")
    p.add_argument("--no-tcp", action="store_true", help="pty only, do not listen on TCP")
    p.add_argument("--rate", type=float, default=10.0, help="acquisition rate in Hz")
    p.add_argument("--ring", type=float, default=600.0, help="on-board ring buffer, in seconds")
    p.add_argument("--scenario", default="ambient",
                   choices=["ambient", "oven", "fridge", "ramp"],
                   help="what the simulated probes are doing")
    p.add_argument("--seed", type=int, default=None, help="RNG seed for reproducible runs")
    p.add_argument("--serial", default=None, help="fix the serial number of the first board")
    p.add_argument("--fault", action="append", default=[],
                   choices=["max31865", "max31856", "aht20"],
                   help="start with a sensor faulted (repeatable)")
    p.add_argument("--reboot-after", type=float, default=0.0,
                   help="reboot every N seconds, to exercise reconnect and resync")
    p.add_argument("-v", "--verbose", action="count", default=0)
    return p


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.DEBUG if args.verbose > 1 else logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    boards: list[SimulatedBoard] = []
    runtimes: list[SimulatorRuntime] = []
    servers = []
    pty_tasks = []

    for i in range(args.boards):
        board = SimulatedBoard(
            serial=args.serial if (i == 0 and args.serial) else None,
            rate_hz=args.rate,
            ring_seconds=args.ring,
            seed=None if args.seed is None else args.seed + i,
            scenario=args.scenario,
        )
        for fault in args.fault:
            board.inject_fault(fault)
        boards.append(board)
        runtime = SimulatorRuntime(board)
        runtimes.append(runtime)

        if not args.no_tcp:
            port = args.port + i
            server, _ = await serve_tcp(board, args.host, port, runtime)
            servers.append(server)
            print(f"board {board.serial}  TCP  {args.host}:{port}")
        if args.pty:
            try:
                path, _, task = await serve_pty(board, runtime)
                pty_tasks.append(task)
                print(f"board {board.serial}  PTY  {path}")
            except RuntimeError as exc:
                print(f"pty unavailable: {exc}", file=sys.stderr)
        await runtime.start()

    print(f"\n{len(boards)} board(s) running at {args.rate:g} Hz, scenario {args.scenario!r}.")
    print("Connect from the app, or:  tjiptemp --connect "
          f"{args.host}:{args.port}\nCtrl-C to stop.\n")

    reboot_task = None
    if args.reboot_after > 0:
        async def reboot_loop() -> None:
            while True:
                await asyncio.sleep(args.reboot_after)
                for board in boards:
                    board.reboot()
                print(f"-- simulated reboot of {len(boards)} board(s)")

        reboot_task = asyncio.create_task(reboot_loop())

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        if reboot_task:
            reboot_task.cancel()
        for task in pty_tasks:
            task.cancel()
        for server in servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        for runtime in runtimes:
            await runtime.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
