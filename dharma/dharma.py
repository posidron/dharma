#!/usr/bin/env python3
"""Command-line interface for Dharma."""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
import struct
import sys
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

from .core.cdp import DharmaCDPRunner
from .core.dharma import DharmaMachine
from .core.websocket import DharmaWebSocketServer

try:
    __version__ = version("dharma")
except PackageNotFoundError:
    __version__ = "2.0.0-dev"

_EXAMPLES = """\
examples:
  %(prog)s -grammars grammar.dg
  %(prog)s -grammars grammar.dg -count 100 -storage ./output
  %(prog)s -grammars a.dg b.dg -server -template tpl.html
  %(prog)s -grammars grammar.dg -cdp -cdp-port 9222
"""


class DharmaCommandLine:
    """Entry point for the ``dharma`` command."""

    @staticmethod
    def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
        parser = argparse.ArgumentParser(
            prog="dharma",
            description="Dharma — generation-based, context-free grammar fuzzer",
            epilog=_EXAMPLES,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        )

        # --- required ---
        req = parser.add_argument_group("required")
        req.add_argument(
            "-grammars",
            "--grammars",
            metavar="FILE",
            type=argparse.FileType(),
            nargs="+",
            required=True,
            help="Grammar files (.dg)",
        )

        # --- generation ---
        gen = parser.add_argument_group("generation")
        gen.add_argument(
            "-count",
            "--count",
            metavar="N",
            type=int,
            default=1,
            help="Number of test cases (default: 1)",
        )
        gen.add_argument(
            "-format",
            "--format",
            metavar="EXT",
            default="html",
            help="Output file extension (default: html)",
        )
        gen.add_argument(
            "-seed",
            "--seed",
            metavar="N",
            type=int,
            help="Random seed (default: from os.urandom)",
        )
        gen.add_argument(
            "-recursion-limit",
            "--recursion-limit",
            metavar="N",
            type=int,
            default=20000,
            help="Python recursion limit (default: 20000)",
        )

        # --- server ---
        srv = parser.add_argument_group("server")
        srv.add_argument(
            "-server", "--server", action="store_true", help="Run WebSocket server"
        )
        srv.add_argument(
            "-server-host",
            "--server-host",
            metavar="HOST",
            default="127.0.0.1",
            help="Bind address (default: 127.0.0.1)",
        )
        srv.add_argument(
            "-server-port",
            "--server-port",
            metavar="PORT",
            type=int,
            default=9090,
            help="Bind port (default: 9090)",
        )
        srv.add_argument(
            "-server-mode",
            "--server-mode",
            metavar="MODE",
            choices=["iframe", "tab"],
            default="iframe",
            help="Browser harness execution mode (default: iframe)",
        )

        # --- CDP ---
        cdp = parser.add_argument_group("cdp")
        cdp.add_argument(
            "-cdp", "--cdp", action="store_true",
            help="Feed test cases via Chrome DevTools Protocol",
        )
        cdp.add_argument(
            "-cdp-host", "--cdp-host", metavar="HOST", default="127.0.0.1",
            help="Chrome debug host (default: 127.0.0.1)",
        )
        cdp.add_argument(
            "-cdp-port", "--cdp-port", metavar="PORT", type=int, default=9222,
            help="Chrome debug port (default: 9222)",
        )
        cdp.add_argument(
            "-cdp-ttl", "--cdp-ttl", metavar="SEC", type=float, default=2.0,
            help="Seconds to wait per test case (default: 2.0)",
        )

        # --- file I/O ---
        fio = parser.add_argument_group("files")
        fio.add_argument(
            "-storage",
            "--storage",
            metavar="DIR",
            help="Output directory for test cases",
        )
        fio.add_argument(
            "-prefix",
            "--prefix",
            metavar="FILE",
            type=argparse.FileType(),
            help="Content prepended to every test case",
        )
        fio.add_argument(
            "-suffix",
            "--suffix",
            metavar="FILE",
            type=argparse.FileType(),
            help="Content appended to every test case",
        )
        fio.add_argument(
            "-template",
            "--template",
            metavar="FILE",
            type=argparse.FileType(),
            help="Template file (use $testcase_content placeholder)",
        )

        # --- debug ---
        dbg = parser.add_argument_group("debug")
        dbg.add_argument(
            "-logging",
            "--logging",
            metavar="LVL",
            type=int,
            default=20,
            choices=range(10, 60, 10),
            help="Logging level (10=DEBUG … 50=CRITICAL, default: 20)",
        )
        dbg.add_argument(
            "-version",
            "--version",
            action="version",
            version=f"%(prog)s {__version__}",
        )

        return parser.parse_args(argv)

    @classmethod
    def main(cls, argv: list[str] | None = None) -> int:
        args = cls.parse_args(argv)

        sys.setrecursionlimit(args.recursion_limit)
        logging.basicConfig(
            format="[dharma] %(asctime)s %(levelname)s: %(message)s",
            level=args.logging,
        )

        # Seed
        if args.seed is None:
            args.seed = struct.unpack("q", os.urandom(8))[0]
        random.seed(args.seed)
        logging.info("Random seed: %d", args.seed)

        # Build machine
        prefix = args.prefix.read() if args.prefix else ""
        suffix = args.suffix.read() if args.suffix else ""
        template = args.template.read() if args.template else ""

        # Close file handles opened by argparse
        for fh in (args.prefix, args.suffix, args.template):
            if fh is not None:
                fh.close()

        dharma = DharmaMachine(prefix, suffix, template)

        try:
            dharma.process_grammars(args.grammars)
        except Exception as exc:
            logging.error("Grammar processing failed: %s", exc)
            return 1

        # Run
        if args.storage:
            try:
                dharma.generate_testcases(args.storage, args.format, args.count)
            except Exception as exc:
                logging.error("Failed writing test cases: %s", exc)
                return 1
        elif args.server:
            server = DharmaWebSocketServer(
                dharma, args.server_host, args.server_port,
                harness_mode=args.server_mode,
            )
            try:
                asyncio.run(server.start())
            except KeyboardInterrupt:
                pass
        elif args.cdp:
            runner = DharmaCDPRunner(
                dharma, args.cdp_host, args.cdp_port, args.cdp_ttl
            )
            try:
                asyncio.run(runner.run())
            except KeyboardInterrupt:
                pass
        else:
            for _ in range(args.count):
                print(dharma.generate_content())

        return 0
