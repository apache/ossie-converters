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

"""The `ossie-databricks` command.

`to-databricks` and `from-databricks` take `<input> -o <output>`, the argument shape of the
interop harness of this repository. Warnings go to stderr, one per line, so the
harness can pass them to the portability grader as diagnostics.
"""

import argparse
import sys
from pathlib import Path

from ossie_databricks.convert import (
    convert_ossie_model_from_databricks,
    convert_ossie_model_to_databricks,
)
from ossie_databricks.deploy import deploy_ossie_model_to_databricks
from ossie_databricks.result import ConversionError, Result
from ossie_databricks.wrap import wrap_metric_view_in_ossie_model


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _to_databricks(args: argparse.Namespace) -> Result:
    result = convert_ossie_model_to_databricks(_read(args.input))
    _write(args.output, result.value)
    return result


def _from_databricks(args: argparse.Namespace) -> Result:
    result = convert_ossie_model_from_databricks(_read(args.input))
    _write(args.output, result.value)
    return result


def _deploy(args: argparse.Namespace) -> Result:
    result = deploy_ossie_model_to_databricks(
        _read(args.input), catalog=args.catalog, schema=args.schema
    )
    files = {args.output / relative: text for relative, text in result.value.items()}
    existing = [str(path) for path in files if path.exists()]
    if existing and not args.overwrite:
        raise ConversionError(
            f"files exist, pass --overwrite to replace them: {', '.join(existing)}"
        )
    for path, text in files.items():
        _write(path, text)
    return result


def _wrap(args: argparse.Namespace) -> Result:
    result = wrap_metric_view_in_ossie_model(args.name, _read(args.input))
    _write(args.output, result.value)
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ossie-databricks", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run, summary: str, input_help: str, output_help: str):
        sub = commands.add_parser(name, help=summary)
        sub.add_argument("input", type=Path, help=input_help)
        sub.add_argument("-o", "--output", type=Path, required=True, help=output_help)
        sub.set_defaults(run=run)
        return sub

    command(
        "to-databricks",
        _to_databricks,
        "the Ossie model for Databricks (unchanged)",
        "Ossie model file",
        "Ossie model file",
    )
    command(
        "from-databricks",
        _from_databricks,
        "the Ossie model with portable expressions filled in",
        "Ossie model file",
        "Ossie model file",
    )
    sub = command(
        "deploy",
        _deploy,
        "metric view YAML files and the rewritten Ossie model",
        "Ossie model file",
        "folder for the deployment",
    )
    sub.add_argument("--catalog", required=True)
    sub.add_argument("--schema", required=True)
    sub.add_argument("--overwrite", action="store_true", help="replace existing files")
    sub = command(
        "wrap",
        _wrap,
        "one metric view to an Ossie model that reads it",
        "metric view file",
        "Ossie model file",
    )
    sub.add_argument("--name", required=True, help="the metric view, catalog.schema.view")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = args.run(args)
    except (ConversionError, OSError, RecursionError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
