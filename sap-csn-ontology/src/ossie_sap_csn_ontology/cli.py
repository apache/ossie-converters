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

"""Command line entry point: ``ossie-sap-csn-ontology``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .csn import load_csn
from .csn_to_ossie import ConversionError, convert, render
from .curation import CurationError, load_curation
from .yaml_io import dump_yaml


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ossie-sap-csn-ontology",
        description="Convert a CAP-style CSN document to an Apache Ossie ontology.",
    )
    parser.add_argument("csn", type=Path, help="path to the CSN JSON document")
    parser.add_argument(
        "-c",
        "--curation",
        type=Path,
        default=None,
        help="path to a curation overlay (business names, verbalizations, dataset bindings)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=None, help="write YAML here instead of stdout"
    )
    parser.add_argument("--name", default=None, help="ontology name (overrides the overlay)")
    parser.add_argument("--description", default=None, help="ontology description")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="fail if the conversion produced warnings or the overlay has unused entries",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        curation = load_curation(args.curation)
        conversion = convert(load_csn(args.csn), curation)
        document = render(conversion, name=args.name, description=args.description)
    except (ConversionError, CurationError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    problems = list(conversion.warnings) + [
        f"curation entry matched nothing in the CSN: {key}" for key in curation.unused_keys()
    ]
    for problem in problems:
        print(f"warning: {problem}", file=sys.stderr)
    if problems and args.strict:
        print("error: --strict and the conversion reported warnings", file=sys.stderr)
        return 1

    text = dump_yaml(document)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
