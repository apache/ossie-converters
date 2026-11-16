# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Command-line interface for the Apache Ossie <-> Hex converter.

    ossie-hex export -i model.yaml [-o hex_project/] [--dialect DIALECT] [-v]

``export`` converts one Apache Ossie semantic model document to a Hex project directory.
If ``-o`` is omitted, files are written to the current working directory.
"""

from __future__ import annotations

import argparse
import sys

from ossie import OssieDialect

from ..ossie_to_hex import convert_ossie_to_hex
from .report import format_export_report

_OSSIE_DIALECT_VALUES = ", ".join(dialect.value.lower() for dialect in OssieDialect)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ossie-hex",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    export = sub.add_parser(
        "export",
        help="Apache Ossie semantic model -> Hex project directory",
    )
    export.add_argument(
        "-i",
        "--input",
        required=True,
        help="Apache Ossie YAML file",
    )
    export.add_argument(
        "-o",
        "--output",
        default=".",
        help="output directory for the Hex project files (default: current directory)",
    )
    export.add_argument(
        "-d",
        "--dialect",
        metavar="DIALECT",
        help=(
            "prefer this dialect of Ossie expressions (case-insensitive). "
            "Accepts a freeform string or a well-known dialect name: "
            f"{_OSSIE_DIALECT_VALUES}"
        ),
    )
    export.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="show grouped problems; repeat (-vv) for phase and cause details",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        parser = _build_parser()
        args = parser.parse_args(argv)
        if args.command == "export":
            hex_project, problems = convert_ossie_to_hex(
                input=args.input,
                output=args.output,
                dialect=args.dialect,
            )
            report = format_export_report(
                input=args.input,
                output=args.output,
                project=hex_project,
                problems=problems,
                verbosity=args.verbose,
            )
            print(report, file=sys.stderr)
            if any(problem.severity in ("fatal", "error") for problem in problems):
                return 1
            return 0
        else:
            raise ValueError(f"Unknown command: {args.command}")
    except Exception as e:  # noqa: BLE001
        print(f"Unexpected error: {e}", file=sys.stderr)
        return 1
    return 0
