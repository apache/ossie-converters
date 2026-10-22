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

"""Command line interface for the Ossie <> Lightdash converter."""

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

import yaml
from pydantic import ValidationError

from ossie import OssieDialect, OssieDocument
from ossie_lightdash.catalog import load_catalog
from ossie_lightdash.converter_issues import ISSUE_EXPLANATIONS, ConversionError
from ossie_lightdash.dbt_project import load_schema_with_skips
from ossie_lightdash.lightdash_to_ossie import LightdashToOssieConverter
from ossie_lightdash.ossie_to_lightdash import OssieToLightdashConverter


def _read_document(path: Path) -> OssieDocument:
    """The Ossie document at ``path``, or a ``ConversionError`` that says
    where it stops being one (the first schema violation, by location)."""
    text = path.read_text(encoding="utf-8")
    try:
        if path.suffix == ".json":
            return OssieDocument.model_validate_json(text)
        return OssieDocument.model_validate(yaml.safe_load(text))
    except yaml.YAMLError as exc:
        raise ConversionError(f"{path}: not valid YAML: {exc}") from exc
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(part) for part in first["loc"]) or "document"
        count = exc.error_count()
        more = f" (and {count - 1} more)" if count > 1 else ""
        raise ConversionError(
            f"{path} is not a valid Ossie document: {location}: {first['msg']}{more}"
        ) from exc


# Ossie dialects that name a Lightdash warehouse type.
_WAREHOUSE_BY_DIALECT = {
    OssieDialect.BIGQUERY: "bigquery",
    OssieDialect.SNOWFLAKE: "snowflake",
    OssieDialect.DATABRICKS: "databricks",
}


_PLACEHOLDER = "CHANGE_ME"


def _existing_warehouse_type(config: Path) -> Optional[str]:
    """warehouse.type of an existing config; None when there is no config or
    no readable type in it."""
    if not config.exists():
        return None
    try:
        document = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return None
    warehouse = document.get("warehouse") if isinstance(document, dict) else None
    if isinstance(warehouse, dict) and isinstance(warehouse.get("type"), str):
        return warehouse["type"]
    return None


def _write_lightdash_project(
    models, output: Path, *, name: str, warehouse: Optional[str]
) -> None:
    """Write one model file per dataset plus a starter lightdash.config.yml.

    Files go to ``<output>/lightdash/models/<model>.yml``, the layout
    ``lightdash deploy`` looks for; an existing config is left alone.
    """
    models_dir = output / "lightdash" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    for model in models:
        (models_dir / f"{model['name']}.yml").write_text(
            yaml.safe_dump(model, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
    # The config is rewritten while it still carries the placeholder and kept
    # once a real warehouse type is in it, whether we or the user put it there.
    config = output / "lightdash.config.yml"
    existing = _existing_warehouse_type(config)
    if existing is not None and existing != _PLACEHOLDER:
        print(f"{config}: kept (warehouse.type is {existing}).", file=sys.stderr)
        return
    config.write_text(
        yaml.safe_dump(
            {
                "name": name,
                "version": "1.0",
                "warehouse": {"type": warehouse or _PLACEHOLDER},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    if warehouse is None:
        print(
            f"{config}: warehouse.type is {_PLACEHOLDER}; pass --warehouse, or a "
            "--dialect Lightdash knows (BIGQUERY, SNOWFLAKE, DATABRICKS), and "
            "re-run. lightdash compile will refuse the placeholder.",
            file=sys.stderr,
        )


def _add_io_arguments(parser: argparse.ArgumentParser, input_help: str, output_help: str) -> None:
    """``-i/--input`` and ``-o/--output`` like the other converters; the two
    positionals are still accepted."""
    parser.add_argument("-v", "--verbose", action="store_true", help="list every affected element, not just the first few per issue type")
    parser.add_argument("-i", "--input", dest="input_flag", metavar="INPUT", type=Path, help=input_help)
    parser.add_argument("-o", "--output", dest="output_flag", metavar="OUTPUT", type=Path, help=output_help)
    parser.add_argument("input", nargs="?", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("output", nargs="?", type=Path, help=argparse.SUPPRESS)


def _resolve_io(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    args.input = args.input_flag or args.input
    args.output = args.output_flag or args.output
    if args.input is None or args.output is None:
        parser.error("both --input and --output are required")
    if not args.input.exists():
        parser.error(f"input not found: {args.input}")


_SHOWN_PER_TYPE = 8
_WRAP = 88


def _wrap(names, indent: str = "    ") -> str:
    lines, line = [], indent
    for name in names:
        piece = name + ", "
        if len(line) + len(piece) > _WRAP and line.strip():
            lines.append(line.rstrip(", "))
            line = indent
        line += piece
    lines.append(line.rstrip(", "))
    return "\n".join(lines)


def _print_issues(issues, verbose: bool = False) -> None:
    """A short block per issue type: header with the count, the explanation,
    then the affected elements (the first few, or all of them with
    ``verbose``)."""
    by_type: "dict" = {}
    for issue in issues:
        by_type.setdefault(issue.issue_type, []).append(issue.element_name)
    for issue_type, elements in by_type.items():
        unique = list(dict.fromkeys(elements))
        noun = "element" if len(unique) == 1 else "elements"
        print(f"{issue_type.value}  ({len(unique)} {noun})", file=sys.stderr)
        print(f"  {ISSUE_EXPLANATIONS.get(issue_type, '')}", file=sys.stderr)
        shown = unique if verbose else unique[:_SHOWN_PER_TYPE]
        print(_wrap(shown), file=sys.stderr)
        if len(unique) > len(shown):
            print(f"    ... and {len(unique) - len(shown)} more", file=sys.stderr)
        print(file=sys.stderr)
    if issues:
        hint = "" if verbose else " Pass --verbose to list every element."
        print(f"{len(issues)} issue(s); everything else converted cleanly.{hint}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="ossie-lightdash")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export_parser = subparsers.add_parser(
        "export",
        help="Ossie document (.json/.yaml) -> Lightdash model files, or a dbt schema.yml",
    )
    _add_io_arguments(
        export_parser,
        "Ossie document (.json or .yaml)",
        "project directory (lightdash-yml) or schema file (dbt-meta)",
    )
    export_parser.add_argument(
        "--format",
        choices=["lightdash-yml", "dbt-meta"],
        default="lightdash-yml",
        help="lightdash-yml: Lightdash's dbt-free model files, deployable as they are "
        "(default); dbt-meta: one dbt schema.yml with Lightdash meta blocks",
    )
    export_parser.add_argument(
        "--warehouse",
        default=None,
        help="warehouse.type for the generated lightdash.config.yml "
        "(default: derived from --dialect when possible)",
    )
    export_parser.add_argument(
        "--dialect",
        choices=[dialect.name for dialect in OssieDialect],
        default=OssieDialect.ANSI_SQL.name,
        help="preferred expression dialect (falls back to OSSIE_SQL_2026, then ANSI_SQL)",
    )
    export_parser.add_argument(
        "--meta-under-config",
        action="store_true",
        help="write Lightdash meta under `config:` (dbt 1.10+) instead of top-level `meta:`",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="Lightdash dbt schema.yml, or a dbt project directory -> Ossie document (.json/.yaml)",
    )
    _add_io_arguments(
        import_parser,
        "a dbt schema file, or a directory walked for models: and seeds:",
        "Ossie document to write (.json or .yaml)",
    )
    import_parser.add_argument("--database", default=None)
    import_parser.add_argument("--schema", default=None)
    import_parser.add_argument(
        "--semantic-model-name", default="lightdash_semantic_model"
    )
    import_parser.add_argument(
        "--dialect",
        choices=[dialect.name for dialect in OssieDialect],
        default=OssieDialect.ANSI_SQL.name,
        help="dialect the Lightdash SQL is written in (the project's warehouse)",
    )
    import_parser.add_argument(
        "--catalog",
        type=Path,
        default=None,
        help="dbt target/catalog.json (from `dbt docs generate`): warehouse column "
        "types fill in datatypes for columns without an authored type",
    )

    args = parser.parse_args(argv)
    _resolve_io(export_parser if args.command == "export" else import_parser, args)

    if args.command == "export":
        dialect = OssieDialect[args.dialect]
        try:
            document = _read_document(args.input)
            converter = OssieToLightdashConverter(dialect, meta_under_config=args.meta_under_config)
            result = (
                converter.convert_models(document)
                if args.format == "lightdash-yml"
                else converter.convert(document)
            )
        except ValueError as exc:
            export_parser.error(str(exc))
        if args.format == "lightdash-yml":
            _write_lightdash_project(
                result.output,
                args.output,
                name=document.name,
                warehouse=args.warehouse or _WAREHOUSE_BY_DIALECT.get(dialect),
            )
            summary = (
                f"Wrote {len(result.output)} model file(s) to {args.output / 'lightdash' / 'models'}"
                f" and {args.output / 'lightdash.config.yml'}; run `lightdash compile` there."
            )
        else:
            args.output.write_text(
                yaml.safe_dump(result.output, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            summary = f"Wrote {len(result.output['models'])} model(s) to {args.output}."
    else:
        try:
            schema_yml, skipped = load_schema_with_skips(args.input)
        except yaml.YAMLError as exc:
            import_parser.error(f"{args.input}: not valid YAML: {exc}")
        for file in skipped:
            print(f"Skipped {file}: not valid YAML (a template or Jinja-only file?)", file=sys.stderr)
        catalog = None
        if args.catalog:
            try:
                catalog = load_catalog(args.catalog)
            except OSError as exc:
                import_parser.error(f"cannot read --catalog {args.catalog}: {exc.strerror or exc}")
            except ConversionError as exc:
                import_parser.error(str(exc))
        try:
            result = LightdashToOssieConverter(OssieDialect[args.dialect]).convert(
                schema_yml,
                database=args.database,
                schema=args.schema,
                semantic_model_name=args.semantic_model_name,
                catalog=catalog,
            )
        except ConversionError as exc:
            import_parser.error(f"{args.input}: {exc}")
        semantic_model = result.output
        summary = (
            f"Wrote {len(semantic_model.datasets or [])} dataset(s), "
            f"{len(semantic_model.metrics or [])} metric(s), "
            f"{len(semantic_model.relationships or [])} relationship(s) to {args.output}."
        )
        document = result.output.model_dump(mode="json", by_alias=True, exclude_none=True)
        if args.output.suffix == ".json":
            args.output.write_text(
                json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        else:
            args.output.write_text(
                yaml.safe_dump(document, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )

    # Issues first, then what was written, all on stderr like the other converters.
    _print_issues(result.issues, verbose=args.verbose)
    print(summary, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
