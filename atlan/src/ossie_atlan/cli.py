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

"""Command-line interface for the Apache Ossie <-> Atlan metric registry converter.

    ossie-atlan import -i RECORDS.json [-o model.yaml] [--name MODEL] [--workspace WS] [--metric NAME ...]
    ossie-atlan export -i model.yaml --metric NAME [-o definition.json]
    ossie-atlan write-back -i model.yaml --metric NAME --record RECORD.json [--registry-version N]

`import` converts registry metric records (one record, a list, or a registry export
`{"artifacts": [...]}`) into one Ossie document; with no `-o` the YAML goes to stdout. Every
metric Ossie cannot express is an issue on stderr, and the rest still convert. `export` does
the reverse for one Ossie metric and prints a registry definition. `write-back` prints the
registry patch an edited Ossie file implies for an existing record (`{}` when nothing changed).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import convert_ossie_to_registry, convert_registry_to_ossie, write_back
from ._common import ConversionError
from .converter import Conflict, NotConvertible, NotWritable


def _records(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    if isinstance(data, dict) and "artifacts" in data:
        return data["artifacts"]
    return data if isinstance(data, list) else [data]


def _build_parser():
    p = argparse.ArgumentParser(prog="ossie-atlan", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="command")
    sub.required = True
    imp = sub.add_parser("import", help="registry metric records -> Apache Ossie semantic model YAML")
    imp.add_argument("-i", "--input", required=True, help="registry records JSON")
    imp.add_argument("-o", "--output", help="output Ossie YAML file (default: stdout)")
    imp.add_argument("--name", default="atlan_registry", help="Ossie model name")
    imp.add_argument("--workspace", help="only records of this workspace_id (one Ossie file per workspace)")
    imp.add_argument("--metric", action="append", help="only these record names (repeatable)")
    exp = sub.add_parser("export", help="one Apache Ossie metric -> registry metric definition JSON")
    exp.add_argument("-i", "--input", required=True, help="Ossie YAML file")
    exp.add_argument("--metric", required=True, help="Ossie metric name")
    exp.add_argument("-o", "--output", help="output JSON file (default: stdout)")
    wb = sub.add_parser("write-back", help="the registry patch an edited Ossie file implies")
    wb.add_argument("-i", "--input", required=True, help="Ossie YAML file")
    wb.add_argument("--metric", required=True, help="Ossie metric name")
    wb.add_argument("--record", required=True, help="the registry record the file was made from")
    wb.add_argument("--registry-version", type=int, help="the registry's current definition version, when known")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        if args.command == "import":
            recs = [r for r in _records(args.input) if (not args.metric or (isinstance(r, dict) and r.get("name") in args.metric))
                    and (not args.workspace or r.get("workspace_id") == args.workspace)]
            doc, issues = convert_registry_to_ossie(recs, name=args.name)
            if issues:
                print(f"{len(issues)} conversion issue(s):", file=sys.stderr)
                for issue in issues:
                    print(f"  {issue}", file=sys.stderr)
            if not doc["metrics"]:
                print("nothing to write: no record converted", file=sys.stderr)
                return 1
            text = yaml.safe_dump(doc, sort_keys=False, width=100, allow_unicode=True)
            Path(args.output).write_text(text) if args.output else sys.stdout.write(text)
            print(f"{len(doc['metrics'])} Ossie metrics from {len(recs)} registry records; "
                  f"{len(issues)} issues", file=sys.stderr)
            return 0
        doc = yaml.safe_load(Path(args.input).read_text())
        if not isinstance(doc, dict) or "datasets" not in doc or "metrics" not in doc:
            raise ConversionError(f"{args.input} is not an Ossie document (no datasets/metrics)")
        if args.command == "export":
            out = json.dumps(convert_ossie_to_registry(doc, args.metric), indent=1, ensure_ascii=False)
            Path(args.output).write_text(out + "\n") if args.output else print(out)
            return 0
        recs = _records(args.record)
        if not recs:
            raise ConversionError(f"{args.record} holds no registry record")
        rec = recs[0]
        print(json.dumps(write_back(doc, args.metric, rec, args.registry_version), indent=1, ensure_ascii=False))
        return 0
    except (ConversionError, NotConvertible, NotWritable, Conflict) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError, yaml.YAMLError) as e:   # unreadable file
        print(f"error: cannot read input ({type(e).__name__}: {str(e).splitlines()[0]})", file=sys.stderr)
        return 1
    except (KeyError, TypeError, AttributeError, IndexError) as e:   # a file that is not what the verb expects
        print(f"error: input is not {'registry records' if args.command == 'import' else 'an Ossie document'} "
              f"({type(e).__name__}: {e})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
