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

"""Run from the repository root: uv run --project interop python -m interop."""

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import math
from pathlib import Path
import shutil
import sys
import traceback

from .manifest import Manifest, ManifestError, load_manifest
from .report import write_report
from .runner import Runner, exit_code


def main(argv=None):
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=root / "interop/manifest.yaml")
    parser.add_argument(
        "--converters", help="comma-separated registered converters (default: all)"
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="new output directory; never overwrites a previous run",
    )
    parser.add_argument(
        "--timeout", type=float, default=600, help="seconds per command"
    )
    parser.add_argument(
        "--no-prepare",
        action="store_true",
        help="use already prepared converter environments/JARs",
    )
    parser.add_argument(
        "--native", action="store_true", help="attempt optional native compiler gates"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 1 for failed, empty, or skipped required checks",
    )
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        parser.error("--timeout must be finite and positive")
    try:
        manifest = load_manifest(args.manifest, root)
        if args.converters is not None:
            names = [name.strip() for name in args.converters.split(",")]
            if (
                not all(names)
                or len(set(names)) != len(names)
                or set(names) - manifest.converters.keys()
            ):
                raise ManifestError(
                    "--converters must be a non-empty list of distinct registered converters"
                )
            manifest = Manifest(
                {name: manifest.converters[name] for name in names},
                manifest.canonical,
                {
                    name: regression
                    for name, regression in manifest.regressions.items()
                    if regression.source in names and regression.via in names
                },
                {
                    name: replace(
                        regression,
                        targets=[
                            target for target in regression.targets if target in names
                        ],
                    )
                    for name, regression in manifest.document_regressions.items()
                    if set(regression.targets) & set(names)
                },
            )
    except ManifestError as error:
        print(f"Manifest error: {error}", file=sys.stderr)
        return 2
    output = (
        args.output
        or root
        / "interop/.runs"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    ).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except OSError as error:
        print(f"Cannot create a fresh output directory: {error}", file=sys.stderr)
        return 2
    runner = None
    try:
        shutil.copyfile(args.manifest, output / "manifest.yaml")
        runner = Runner(
            root,
            manifest,
            output,
            timeout=args.timeout,
            prepare=not args.no_prepare,
            native=args.native,
        )
        report = runner.run()
    except (Exception, SystemExit) as error:
        traceback.print_exc(file=sys.stderr)
        report = (
            runner.report
            if runner
            else {
                "schema_version": 1,
                "metadata": {
                    "repo_commit": None,
                    "ossie_commit": None,
                    "converters": list(manifest.converters),
                },
                "preparations": {},
                "sources": [],
                "cases": [],
                "regressions": [],
                "document_regressions": [],
                "harness_errors": [],
            }
        )
        report["harness_errors"].append(f"{type(error).__name__}: {error}")
    try:
        write_report(report, output)
    except OSError as error:
        print(f"Cannot write report: {error}", file=sys.stderr)
        return 2
    print(f"Report: {output / 'report.md'}\nJSON:   {output / 'results.json'}")
    return exit_code(report, strict=args.strict)


if __name__ == "__main__":
    sys.exit(main())
