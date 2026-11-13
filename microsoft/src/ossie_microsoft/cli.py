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

"""Command-line interface for the Microsoft Power BI <-> Apache Ossie converter.

    ossie-microsoft import -i model.bim   [-o model.yaml]
    ossie-microsoft import -i Report.pbip [-o model.yaml]
    ossie-microsoft export -i model.yaml  [-o model.bim]

With no ``-o``, the result is written to stdout.

Every construct the converter cannot carry across faithfully is reported on stderr, so
the log is a complete account of what a conversion changed, skipped or could not
represent. ``--strict`` turns any such report into a non-zero exit, which is the useful
mode in a pipeline that must not silently degrade a model.
"""

import argparse
import json
import logging
import sys
import warnings
from pathlib import Path

import yaml

from ._common import LOGGER, ConversionError
from ._report import collect_entries
from .ossie_to_semantic_model import convert_ossie_to_semantic_model
from .pbip import import_powerbi_project, reraise_tom_load_failure
from .semantic_model_to_ossie import convert_semantic_model_to_ossie


class _CountingHandler(logging.Handler):
    """Counts reported conversion issues so the CLI can summarize and exit on them."""

    def __init__(self, quiet, report):
        super().__init__()
        self.count = 0
        self.quiet = quiet
        self.report = report

    def emit(self, record):
        if record.levelno < logging.WARNING:
            return
        self.count += 1
        message = record.getMessage()
        if self.report is not None:
            self.report["warnings"].append(message)
        if not self.quiet:
            print(message, file=sys.stderr)


def _add_common_flags(parser, suppress):
    """Add the flags shared by the root parser and every subcommand.

    `suppress` is used for the subcommand copies: without it argparse would write the
    subparser's own default over a value the root parser already parsed, so
    `--strict` before the subcommand would be silently ignored.
    """
    default = argparse.SUPPRESS if suppress else False
    parser.add_argument(
        "--strict",
        action="store_true",
        default=default,
        help="exit non-zero if anything could not be converted faithfully",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=default,
        help="suppress the report of constructs that could not be converted",
    )
    parser.add_argument(
        "--report", type=Path, default=argparse.SUPPRESS if suppress else None,
        help="write a JSON conversion report to this file",
    )


def _build_parser():
    # The flags are accepted both before and after the subcommand, because that is where
    # people actually type them.
    sub_flags = argparse.ArgumentParser(add_help=False)
    _add_common_flags(sub_flags, suppress=True)

    parser = argparse.ArgumentParser(
        prog="ossie-microsoft",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _add_common_flags(parser, suppress=False)
    sub = parser.add_subparsers(dest="command")
    sub.required = True

    imp = sub.add_parser(
        "import",
        parents=[sub_flags],
        help="Power BI semantic model -> Apache Ossie semantic model",
    )
    imp.add_argument(
        "-i",
        "--input",
        required=True,
        help="model.bim, a .pbip file, a .SemanticModel folder, or its definition folder",
    )
    imp.add_argument("-o", "--output", help="output Apache Ossie YAML (default: stdout)")
    imp.add_argument(
        "--translate-dax",
        action="store_true",
        default=False,
        help="translate DAX measures to OSSIE_SQL_2026 where the translation is exact "
        "(see _dax_to_sql), keeping the original DAX as a fallback (default: off, keep DAX only)",
    )

    exp = sub.add_parser(
        "export",
        parents=[sub_flags],
        help="Apache Ossie semantic model -> Power BI semantic model (TMSL model.bim)",
    )
    exp.add_argument("-i", "--input", required=True, help="Apache Ossie YAML file")
    exp.add_argument("-o", "--output", help="output model.bim (default: stdout)")
    return parser


def _run_import(args):
    path = Path(args.input)
    if path.is_dir() or path.suffix.lower() == ".pbip":
        return import_powerbi_project(path, translate_dax=args.translate_dax)
    text = path.read_text(encoding="utf-8-sig")
    stripped = text.lstrip("\ufeff").lstrip()
    if path.suffix.lower() == ".bim" or stripped.startswith("{"):
        return convert_semantic_model_to_ossie(
            json.loads(stripped), translate_dax=args.translate_dax
        )
    try:
        return convert_semantic_model_to_ossie(text, translate_dax=args.translate_dax)
    except Exception as exc:
        reraise_tom_load_failure(exc)


def _run_export(args):
    with open(args.input, encoding="utf-8-sig") as fh:
        ossie_yaml = fh.read()
    # Power BI writes model.bim as UTF-8 JSON with non-ASCII characters left as-is.
    return json.dumps(
        convert_ossie_to_semantic_model(ossie_yaml), indent=2, ensure_ascii=False
    ) + "\n"


def main(argv=None):
    args = _build_parser().parse_args(argv)
    handler = _run_import if args.command == "import" else _run_export
    report = None
    if args.report is not None:
        try:
            _check_report_path(args.report, args.input, args.output)
        except (OSError, ValueError) as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        report = {"version": 1, "direction": args.command, "entries": [], "warnings": []}

    # Each lossy step is reported through both `warnings` and the logger. For a command
    # line the log is the better channel -- it is ordered with respect to the rest of the
    # output and carries no file/line noise -- so the warning channel is silenced here to
    # stop every message being printed twice.
    counter = _CountingHandler(args.quiet, report)
    LOGGER.addHandler(counter)
    error = None
    entries = report["entries"] if report is not None else None
    try:
        with collect_entries(entries), warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            out = handler(args)
    except (ConversionError, TypeError, ValueError, OSError, yaml.YAMLError) as e:
        error = str(e)
    finally:
        LOGGER.removeHandler(counter)

    if error is not None:
        print(f"Error: {error}", file=sys.stderr)
    if report is not None:
        report["completed"] = error is None
        if error is not None:
            report["error"] = error
        try:
            args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as e:
            print(f"Error writing conversion report: {e}", file=sys.stderr)
            return 1
    if error is not None:
        return 1

    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out)
    else:
        sys.stdout.write(out)

    if counter.count and not args.quiet:
        print(
            f"{counter.count} construct(s) could not be converted faithfully; "
            "see the messages above.",
            file=sys.stderr,
        )
    return 1 if args.strict and counter.count else 0


def _check_report_path(report, input_path, output_path):
    for path in (input_path, output_path):
        if path is None:
            continue
        other = Path(path)
        if report.resolve() == other.resolve() or (
            report.exists() and other.exists() and report.samefile(other)
        ):
            raise ValueError("--report must be separate from the model input and output")


if __name__ == "__main__":
    sys.exit(main())
