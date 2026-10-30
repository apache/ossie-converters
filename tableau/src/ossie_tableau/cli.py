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

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path

from .convert import Loss, _ident, convert_file
from .losses import KNOWN_KINDS
from .warehouse import WAREHOUSES, Connection

EXTS = {".tds", ".twb", ".twbx", ".tdsx"}


def _expand(paths: list[str]) -> list[Path]:
    out: list[Path] = []
    for p in map(Path, paths):
        out.extend(sorted(q for q in p.rglob("*") if q.suffix.lower() in EXTS) if p.is_dir() else [p])
    return out


def _tds_stem(name: str) -> str:
    for suf in (".ossie.yaml", ".ossie.yml", ".yaml", ".yml"):
        if name.lower().endswith(suf):
            return name[: -len(suf)] or name
    return name


def _kinds(text: str) -> list[str]:
    kinds = [k.strip() for k in text.split(",") if k.strip()]
    bad = [k for k in kinds if k not in KNOWN_KINDS]
    if bad:
        raise argparse.ArgumentTypeError(f"unknown loss kind(s): {', '.join(bad)}")
    return kinds


def _add_strict(p: argparse.ArgumentParser) -> None:
    p.add_argument("--strict", action="store_true", help="exit 1 if any loss has severity 'error'")
    p.add_argument("--strict-kinds", type=_kinds, default=[], metavar="K1,K2",
                   help="exit 1 if any loss of these kinds is recorded")


def _strict_violations(losses: list[Loss], strict: bool, kinds: list[str]) -> list[Loss]:
    """Losses that make a strict run fail: any `error` severity (--strict) or any listed kind (--strict-kinds)."""
    return [x for x in losses if (strict and x.severity == "error") or x.kind in kinds]


def _strict_exit(losses: list[Loss], args) -> int:
    bad = _strict_violations(losses, args.strict, args.strict_kinds)
    if bad:
        counts: dict[str, int] = {}
        for x in bad:
            counts[x.kind] = counts.get(x.kind, 0) + 1
        print("STRICT: " + ", ".join(f"{k} x{n}" for k, n in sorted(counts.items())), file=sys.stderr)
        return 1
    return 0


def _saved_connection(doc, warehouse) -> dict:
    for x in doc.custom_extensions or []:
        if x.vendor_name == "TABLEAU":
            try:
                c = json.loads(x.data).get("connection") or {}
            except ValueError:
                return {}
            return c if c.get("class") == warehouse.tableau_class else {}
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="ossie-tableau")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("convert", help="convert Tableau files (or directories) to Ossie YAML")
    c.add_argument("paths", nargs="+")
    c.add_argument("-o", "--out", required=True)
    c.add_argument("--inline-parameters", action="store_true", help="substitute current parameter values into calculations (approximated; they no longer follow parameter changes)")
    c.add_argument("--warehouse-type", choices=sorted(WAREHOUSES), default=None, help="force the target warehouse dialect instead of detecting it from the connection class")
    c.add_argument("--no-clobber", action="store_true", help="refuse to overwrite existing output files")
    c.add_argument("-q", "--quiet", action="store_true", help="only print problems (SKIP, WARN, FAIL)")
    _add_strict(c)
    t = sub.add_parser("to-tds", help="write a Tableau logical-layer TDS from an Ossie YAML file")
    t.add_argument("ossie_yaml")
    t.add_argument("-o", "--out", required=True)
    t.add_argument("--server", default="", help="defaults to the connection stored in the Ossie document")
    t.add_argument("--warehouse-type", default="snowflake", choices=sorted(WAREHOUSES), help="target warehouse (connection class and SQL dialect)")
    t.add_argument("--warehouse", default="", help="Snowflake compute warehouse (a connection attribute, not the warehouse type)")
    t.add_argument("--database", default="", help="Snowflake database, or the BigQuery data project")
    t.add_argument("--schema", default="", help="Snowflake schema, or the BigQuery dataset")
    t.add_argument("--project", default="", help="BigQuery project that runs queries (billing); defaults to the data project")
    t.add_argument("--role", default="")
    t.add_argument("--username", default="", help="defaults to the username stored in the Ossie document")
    _add_strict(t)
    args = ap.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if args.cmd == "to-tds":
        import yaml
        from ossie.models import OssieDocument
        from .tds_writer import write_tds
        doc = OssieDocument.model_validate(yaml.safe_load(Path(args.ossie_yaml).read_text()))
        wh = WAREHOUSES[args.warehouse_type]
        saved = _saved_connection(doc, wh)
        server = args.server or saved.get("server", "") or wh.default_server
        if not server:
            ap.error(f"--server is required (the Ossie document has no stored {wh.name} connection)")
        db = args.database or saved.get(wh.database_attribute, "") or saved.get("dbname", "")
        extra = {}
        if "warehouse" in wh.extra_attributes:
            extra["warehouse"] = args.warehouse or saved.get("warehouse", "")
        if "EXECCATALOG" in wh.extra_attributes:
            extra["EXECCATALOG"] = args.project or saved.get("EXECCATALOG", "") or db
            extra["project"] = db or saved.get("project", "")
            extra["authentication"] = saved.get("authentication", "")
        res = write_tds(doc, Connection(wh, server, db, args.schema or saved.get("schema", ""),
                                        args.role or saved.get("service", ""),
                                        args.username or saved.get("username", ""), extra=extra))
        stem = _tds_stem(Path(args.ossie_yaml).name)
        (out / f"{stem}.tds").write_text(res.xml)
        (out / f"{stem}.tds.loss.json").write_text(json.dumps([dataclasses.asdict(x) for x in res.losses], indent=2))
        print(f"OK   {stem}.tds losses={len(res.losses)}")
        return _strict_exit(res.losses, args)
    rc = 0
    all_losses: list[Loss] = []
    used: dict[str, str] = {}
    manifest: list[dict] = []
    for f in sorted(_expand(args.paths), key=lambda q: str(q)):
        try:
            results = convert_file(f, args.inline_parameters, WAREHOUSES[args.warehouse_type] if args.warehouse_type else None)
        except Exception as ex:  # noqa: BLE001
            print(f"FAIL {f.name}: {type(ex).__name__}: {ex}", file=sys.stderr)
            manifest.append({"source": str(f), "status": "failed", "error": f"{type(ex).__name__}: {ex}"})
            rc = 1
            continue
        for r in results:
            all_losses.extend(r.losses)
            stem = base = f"{f.stem}__{_ident(r.datasource)}"
            src = f"{f}::{r.datasource}"
            if stem in used:
                stem = f"{f.parent.name}__{base}" if f.parent.name else base
                n = 2
                while stem in used:
                    stem = f"{f.parent.name}__{base}__{n}"
                    n += 1
                print(f"WARN {src} collides with {used[base]}; writing as {stem}")
            used[stem] = src
            clash = [q for q in (out / f"{stem}.loss.json", out / f"{stem}.ossie.yaml") if q.exists()]
            if args.no_clobber and clash:
                print(f"FAIL {src}: {clash[0].name} exists (--no-clobber)", file=sys.stderr)
                rc = 1
                continue
            (out / f"{stem}.loss.json").write_text(json.dumps([dataclasses.asdict(x) for x in r.losses], indent=2))
            manifest.append({"source": str(f), "datasource": r.datasource, "output": f"{stem}.ossie.yaml" if r.document else None,
                             "status": "converted" if r.document else "skipped", "losses": len(r.losses)})
            if r.document is None:
                print(f"SKIP {stem}: {[x.kind for x in r.losses]}")
                continue
            (out / f"{stem}.ossie.yaml").write_text(r.document.to_ossie_yaml())
            if not args.quiet:
                print(f"OK   {stem}: {r.stats} losses={len(r.losses)}")
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return rc or _strict_exit(all_losses, args)


if __name__ == "__main__":
    sys.exit(main())
