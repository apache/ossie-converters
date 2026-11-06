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

"""Command line entry point for the BigQuery / LookML converter.

Usage:
    ossie-bigquery export -i model.yaml -o output.lkml
    ossie-bigquery import -i lookml_project/ [-o model.yaml]
                         [--explore NAME | --all-explores] [--model NAME] [--name NAME]

`export` converts an Ossie model to LookML; `import` converts a LookML
project (a directory of .lkml files, or one file) to an Ossie model, written
to stdout when no `-o` is given. `ossie-bigquery -i ... -o ...` without a
command still means `export`. Conversions that drop information warn on stderr.
"""

import argparse
import os
import sys
from pathlib import Path

from .lookml_to_ossie import convert_lookml_to_ossie
from .ossie_to_lookml import OssieConversionError, convert_ossie_to_lookml


def _export_arguments(parser):
    parser.add_argument(
        "-i", "--input", required=True, help="Path to the Ossie YAML input file"
    )
    parser.add_argument(
        "-o", "--output", required=True, help="Path to write the LookML output"
    )


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="ossie-bigquery",
        description="Convert between Ossie YAML semantic models and LookML (Looker / BigQuery)",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    _export_arguments(sub.add_parser("export", help="Ossie YAML -> LookML"))
    imp = sub.add_parser("import", help="LookML project -> Ossie YAML")
    imp.add_argument("-i", "--input", required=True,
                     help="LookML project directory, or a single .lkml file")
    imp.add_argument("-o", "--output", help="Path to write the Ossie YAML (default: stdout)")
    which = imp.add_mutually_exclusive_group()
    which.add_argument("--explore", help="Explore to convert (needed when there are several)")
    which.add_argument("--all-explores", action="store_true",
                       help="Merge every explore into one Ossie model")
    imp.add_argument("--model", help="Looker model whose includes to follow "
                                     "(needed when the explore is in several)")
    imp.add_argument("--name", help="Ossie model name (default: the explore's name)")
    return parser


def _read_lookml(path):
    """Returns {relative path: text} for a .lkml file, or every .lkml file
    under a directory (hidden directories skipped)."""
    if os.path.isfile(path):
        with open(path) as f:
            return {os.path.basename(path): f.read()}
    if not os.path.isdir(path):
        raise OSError(f"'{path}' is not a file or directory")
    root = Path(path)
    files = {str(p.relative_to(root)): p.read_text() for p in sorted(root.rglob("*.lkml"))
             if not any(part.startswith(".") for part in p.relative_to(root).parts)}
    if not files:
        raise OSError(f"no .lkml files under '{path}'")
    return files


def _export(args):
    with open(args.input, "r") as f:
        ossie_yaml_str = f.read()
    lookml_str = convert_ossie_to_lookml(ossie_yaml_str)
    with open(args.output, "w") as f:
        f.write(lookml_str)
    print(f"Converted {args.input} -> {args.output}")


def _import(args):
    ossie_yaml_str = convert_lookml_to_ossie(
        _read_lookml(args.input), explore=args.explore, model_name=args.name,
        model=args.model, all_explores=args.all_explores,
    )
    if args.output:
        with open(args.output, "w") as f:
            f.write(ossie_yaml_str)
        print(f"Converted {args.input} -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(ossie_yaml_str)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in ("export", "import"):
        args = _build_parser().parse_args(argv)
    else:
        # The original form, `ossie-bigquery -i model.yaml -o out.lkml`.
        legacy = argparse.ArgumentParser(
            prog="ossie-bigquery",
            description="Convert Ossie YAML semantic model to LookML (Looker / BigQuery). "
                        "Use `ossie-bigquery import -h` for LookML to Ossie.",
        )
        _export_arguments(legacy)
        args = legacy.parse_args(argv)
        args.command = "export"

    try:
        if args.command == "export":
            _export(args)
        else:
            _import(args)
    except (OssieConversionError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
