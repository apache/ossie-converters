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

"""Command line for the offline Context Studio converter."""

import argparse
import sys
import warnings
from pathlib import Path

from .converter import (
    ConversionError,
    convert_ossie_to_starburst,
    convert_starburst_to_ossie,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="Ossie YAML to Context Studio YAML")
    import_ = commands.add_parser("import", help="Context Studio YAML to Ossie YAML")
    for command in (export, import_):
        command.add_argument("-i", "--input", required=True, type=Path)
        command.add_argument(
            "-o", "--output", type=Path, help="output file (default: stdout)"
        )
    export.add_argument("--domain", help="override the Context Studio data domain")
    export.add_argument(
        "--skip-unsupported-metrics",
        action="store_true",
        help="warn and omit unsupported metrics",
    )
    import_.add_argument("--name", help="override the Ossie model name")
    args = parser.parse_args(argv)

    try:
        text = args.input.read_text(encoding="utf-8")
        with warnings.catch_warnings(record=True) as messages:
            warnings.simplefilter("always")
            if args.command == "export":
                output = convert_ossie_to_starburst(
                    text,
                    domain=args.domain,
                    skip_unsupported_metrics=args.skip_unsupported_metrics,
                )
            else:
                output = convert_starburst_to_ossie(text, model_name=args.name)
        for message in messages:
            print(f"Warning: {message.message}", file=sys.stderr)
        if args.output is None:
            sys.stdout.write(output)
        else:
            args.output.write_text(output, encoding="utf-8")
    except (ConversionError, OSError, UnicodeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
