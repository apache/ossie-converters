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

"""Command-line entry point for the converter between Apache Ossie and Databricks Metric Views.

::

    ossie-databricks export <model.yaml> [-o <view.yaml>] [--source <dataset>]
    ossie-databricks import <view.yaml>  [-o <model.yaml>] [--name <model>]

The directions are named from the Apache Ossie model's point of view: ``export`` converts an
Apache Ossie semantic model to a Metric View; ``import`` converts a Metric View to an Apache Ossie
model. Output goes to the ``-o`` file, or to stdout when omitted. Conversion notices are written
to stderr. A broken input raises :class:`ConversionError`, reported as a non-zero exit.
"""

from __future__ import annotations

import sys
from typing import TextIO

from ossie_databricks.ossie_converter import (
    ConversionError,
    convert_metric_view_to_ossie,
    convert_ossie_to_metric_view,
)


class ExitError(Exception):
    """Signals a clean CLI exit with a message and status code (kept out of the library core)."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __str__(self) -> str:
        return self.message


# command name -> the one selector flag it accepts
_COMMANDS = {"export": "--source", "import": "--name"}


def usage() -> str:
    return (
        "Usage:\n"
        "  ossie-databricks export <model.yaml> [-o <view.yaml>] [--source <dataset>]\n"
        "  ossie-databricks import <view.yaml>  [-o <model.yaml>] [--name <model>]"
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    try:
        run(args, sys.stdout, sys.stderr)
    except ExitError as e:
        print(e.message, file=sys.stderr)
        return e.code
    except ConversionError as e:
        print(f"Conversion failed: {e}", file=sys.stderr)
        return 1
    return 0


def run(args: list[str], out: TextIO, err: TextIO) -> None:
    """Testable core: parses args, runs the conversion, and writes output."""
    if not args:
        raise ExitError(2, usage())
    first = args[0]
    if first in ("-h", "--help", "help"):
        print(usage(), file=out)
        return
    # Resolve the command before parsing the rest, so an unknown command reports itself rather
    # than whatever the argument parser trips over first.
    if first not in _COMMANDS:
        raise ExitError(2, f"Unknown command '{first}'.\n{usage()}")
    input_path, output_path, option = _parse_args(args, first)

    text = _read(input_path)
    if first == "export":
        result = convert_ossie_to_metric_view(text, option)
    else:
        result = convert_metric_view_to_ossie(text, option)

    _write(output_path, result.yaml, out)
    if result.notices:
        print(f"Conversion notices ({len(result.notices)}):", file=err)
        for notice in result.notices:
            print(f"  {notice}", file=err)


def _parse_args(args: list[str], command: str) -> tuple[str, str | None, str | None]:
    option_flag = _COMMANDS[command]
    input_path: str | None = None
    output_path: str | None = None
    option: str | None = None
    i = 1
    while i < len(args):
        arg = args[i]
        if arg in ("-o", "--output"):
            i += 1
            output_path = _require_value(args, i, arg)
        elif arg in ("--source", "--name"):
            if arg != option_flag:
                raise ExitError(
                    2,
                    f"Option '{arg}' is not valid for '{command}'; use '{option_flag}'.\n"
                    f"{usage()}",
                )
            i += 1
            option = _require_value(args, i, arg)
        elif arg.startswith("-"):
            raise ExitError(2, f"Unknown option '{arg}'.\n{usage()}")
        elif input_path is not None:
            raise ExitError(2, f"Unexpected extra argument '{arg}'.\n{usage()}")
        else:
            input_path = arg
        i += 1
    if input_path is None:
        raise ExitError(2, f"Missing input file.\n{usage()}")
    return input_path, output_path, option


def _require_value(args: list[str], index: int, flag: str) -> str:
    if index >= len(args):
        raise ExitError(2, f"Option '{flag}' requires a value.\n{usage()}")
    return args[index]


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", newline="") as f:
            return f.read()
    except OSError as e:
        raise ExitError(1, f"Cannot read input file '{path}': {e}") from e


def _write(path: str | None, content: str, out: TextIO) -> None:
    if path is None:
        # write, not print: the serialized YAML already ends in a newline, so stdout and `-o`
        # produce the same bytes.
        out.write(content)
        out.flush()
        return
    try:
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(content)
    except OSError as e:
        raise ExitError(1, f"Cannot write output file '{path}': {e}") from e


if __name__ == "__main__":
    sys.exit(main())
