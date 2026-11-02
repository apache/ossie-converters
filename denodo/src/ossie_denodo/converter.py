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

"""
Entry point for the Ossie <-> Denodo Metric View converters.

Usage (CLI):
    # OSI YAML → Denodo VQL
    python -m ossie_denodo.converter osi-to-vql -i model.yaml -o output.vql

    # Denodo VQL → OSI YAML
    python -m ossie_denodo.converter vql-to-osi -i input.vql -o model.yaml
"""

from __future__ import annotations

import argparse
import sys

from ossie_denodo.osi_to_vql import OssieConversionError, convert_ossie_to_vql
from ossie_denodo.vql_to_osi import VqlConversionError, convert_vql_to_ossie

__all__ = [
    "OssieConversionError",
    "VqlConversionError",
    "convert_ossie_to_denodo",
    "convert_denodo_to_ossie",
]


def convert_ossie_to_denodo(ossie_yaml_str: str) -> str:
    """Convert an Ossie YAML string to a Denodo VQL string.

    This is the primary public entry point for the OSI → VQL direction.

    Args:
        ossie_yaml_str: Ossie YAML document as a string.

    Returns:
        Denodo VQL as a string containing ``CREATE OR REPLACE ASSOCIATION``
        statements followed by a ``CREATE OR REPLACE METRIC VIEW`` statement.

    Raises:
        OssieConversionError: If the input is invalid or cannot be converted.
    """
    return convert_ossie_to_vql(ossie_yaml_str)


def convert_denodo_to_ossie(vql_str: str) -> str:
    """Convert a Denodo VQL string to an Ossie YAML string.

    Args:
        vql_str: Denodo VQL string containing association and metric view
            statements.

    Returns:
        Ossie YAML as a string.

    Raises:
        OssieConversionError: If the input is invalid or cannot be converted.
    """
    return convert_vql_to_ossie(vql_str)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Convert between Ossie YAML and Denodo VQL"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # osi-to-vql sub-command
    p_osi = subparsers.add_parser("osi-to-vql", help="Convert Ossie YAML to Denodo VQL")
    p_osi.add_argument("-i", "--input", required=True, help="Input Ossie YAML file")
    p_osi.add_argument("-o", "--output", required=True, help="Output Denodo VQL file")

    # vql-to-osi sub-command (placeholder)
    p_vql = subparsers.add_parser("vql-to-osi", help="Convert Denodo VQL to Ossie YAML")
    p_vql.add_argument("-i", "--input", required=True, help="Input Denodo VQL file")
    p_vql.add_argument("-o", "--output", required=True, help="Output Ossie YAML file")

    args = parser.parse_args()

    try:
        if args.command == "osi-to-vql":
            with open(args.input, "r", encoding="utf-8") as f:
                ossie_content = f.read()
            vql_result = convert_ossie_to_denodo(ossie_content)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(vql_result)
            print(f"Successfully converted '{args.input}' → '{args.output}'")

        elif args.command == "vql-to-osi":
            with open(args.input, "r", encoding="utf-8") as f:
                vql_content = f.read()
            ossie_result = convert_denodo_to_ossie(vql_content)
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(ossie_result)
            print(f"Successfully converted '{args.input}' → '{args.output}'")

    except (OssieConversionError, VqlConversionError) as e:
        print(f"Conversion error: {e}", file=sys.stderr)
        sys.exit(1)
    except OSError as e:
        print(f"File error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
