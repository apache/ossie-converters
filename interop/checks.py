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

"""Use the pinned Ossie validator, and inspect actual native model content."""

from collections import Counter
import importlib.util
import json
import re

import yaml


class ContentError(ValueError):
    pass


class Validator:
    def __init__(self, root):
        path = root / "ossie/validation/validate.py"
        spec = importlib.util.spec_from_file_location("interop_ossie_validator", path)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.schema = json.loads(
            (root / "ossie/core-spec/ossie-schema.json").read_text()
        )
        self.module.Draft202012Validator.check_schema(self.schema)
        if not self.module.SQLGLOT_AVAILABLE:
            raise RuntimeError("SQL validation requires sqlglot")

    def validate(self, path):
        try:
            data = self.module.load_yaml_named(
                path.read_text(encoding="utf-8"), str(path)
            )
        except (OSError, yaml.YAMLError, ValueError, RecursionError) as error:
            return None, {"errors": [str(error)], "warnings": []}
        # Exceptions inside the validator are harness errors, not rejected documents.
        messages = self.module.validate_document(data, self.schema)
        return data, {
            "errors": [
                str(x)
                for x in messages
                if not isinstance(x, self.module.ValidationWarning)
            ],
            "warnings": [
                str(x) for x in messages if isinstance(x, self.module.ValidationWarning)
            ],
        }


def inventory(data):
    datasets = data.get("datasets") or []
    return {
        "datasets": len(datasets),
        "fields": sum(len(ds.get("fields") or []) for ds in datasets),
        "metrics": len(data.get("metrics") or []),
        "relationships": len(data.get("relationships") or []),
        "primary_keys": sum(bool(ds.get("primary_key")) for ds in datasets),
        "unique_keys": sum(len(ds.get("unique_keys") or []) for ds in datasets),
        "extensions": sum(1 for _ in _extensions(data)),
    }


def _extensions(value):
    if isinstance(value, dict):
        yield from value.get("custom_extensions") or []
        for key, child in value.items():
            if key != "custom_extensions":
                yield from _extensions(child)
    elif isinstance(value, list):
        for child in value:
            yield from _extensions(child)


def meaningful_ossie(data):
    counts = inventory(data)
    return bool(counts["fields"] or counts["metrics"])


def native_content(path, native_format, output_shape):
    if output_shape == "directory":
        if not path.is_dir():
            return {"files": [], "members": 0}
        paths = sorted(
            p
            for p in path.rglob("*")
            if p.is_file()
            and p.suffix
            in (
                {".tml"}
                if native_format == "thoughtspot"
                else {".aml"}
                if native_format == "holistics"
                else {".yaml", ".yml"}
            )
        )
    else:
        paths = [path] if path.is_file() and path.stat().st_size else []
    if native_format == "omni":
        paths = [p for p in paths if p.name.endswith((".view.yaml", ".view.yml"))]
    members = 0
    model_files = []
    for item in paths:
        try:
            if native_format == "holistics":
                # Source-content check only; AML compilation is a separate prerequisite.
                text = item.read_text(encoding="utf-8")
                count = len(
                    re.findall(r"(?m)^\s*(?:dimension|measure)\s+\w+\s*\{", text)
                )
                if item.suffix == ".aml" and count:
                    model_files.append(str(item))
                    members += count
                continue
            data = (
                json.loads(item.read_text(encoding="utf-8"))
                if native_format
                in ("dbt", "gooddata", "microsoft", "sigma", "wisdom", "salesforce")
                else yaml.safe_load(item.read_text(encoding="utf-8"))
            )
            if not isinstance(data, dict):
                raise ContentError(f"{item}: expected a native model mapping")
            if native_format == "cube":
                models = (data.get("cubes") or []) + (data.get("views") or [])
                count = sum(
                    len(model.get("dimensions") or [])
                    + len(model.get("measures") or [])
                    + sum(
                        len(cube.get("includes") or [])
                        for cube in model.get("cubes") or []
                    )
                    for model in models
                )
                is_model = bool(models)
            elif native_format == "dbt":
                models = data.get("semantic_models") or []
                count = len(data.get("metrics") or []) + sum(
                    sum(
                        len(model.get(key) or [])
                        for key in ("entities", "dimensions", "measures")
                    )
                    for model in models
                )
                is_model = bool(models)
            elif native_format != "databricks":
                count = _additional_members(data, native_format)
                is_model = count > 0
            else:
                count = len(data.get("dimensions") or []) + len(
                    data.get("measures") or []
                )
                is_model = bool(data.get("source"))
            if is_model:
                model_files.append(str(item))
                members += count
        except (
            OSError,
            ValueError,
            TypeError,
            AttributeError,
            yaml.YAMLError,
        ) as error:
            raise ContentError(f"cannot inspect {item}: {error}") from error
    return {"files": model_files, "members": members}


def _additional_members(data, native_format):
    """Count format-specific native members, never metadata or arbitrary file bytes.

    This is a structural smoke check, not a substitute for vendor compilation.
    Unexpected shapes raise through native_content as ContentError.
    """
    if native_format == "omni":
        return len(data.get("dimensions") or {}) + len(data.get("measures") or {})
    if native_format == "hex":
        # Hex resource files wrap a model, dimension, measure or relation.
        return sum(len(data.get(key) or []) for key in ("dimensions", "measures"))
    if native_format == "honeydew":
        return len(data.get("attributes") or []) + int(
            data.get("type") in ("calculated_attribute", "metric")
            and bool(data.get("sql") and data.get("name"))
        )
    if native_format == "gooddata":
        ldm = data.get("ldm") or {}
        return sum(
            len(ds.get("attributes") or []) + len(ds.get("facts") or [])
            for ds in ldm.get("datasets") or []
        ) + len(ldm.get("dateInstances") or [])
    if native_format == "microsoft":
        model = data.get("model") or {}
        return sum(
            len(table.get("columns") or []) + len(table.get("measures") or [])
            for table in model.get("tables") or []
        )
    if native_format == "nvidia":
        return sum(
            len(table.get("columns") or [])
            for db in (data.get("data_layer") or {}).get("databases") or []
            for schema in db.get("schemas") or []
            for table in schema.get("tables") or []
        )
    if native_format == "orionbelt":
        return sum(
            len(model.get("columns") or {})
            for model in (data.get("dataObjects") or {}).values()
        )
    if native_format == "sigma":
        return sum(
            len(element.get("columns") or []) + len(element.get("metrics") or [])
            for page in data.get("pages") or []
            for element in page.get("elements") or []
        )
    if native_format == "snowflake":
        return sum(
            sum(
                len(table.get(key) or [])
                for key in (
                    "dimensions",
                    "time_dimensions",
                    "measures",
                    "facts",
                    "metrics",
                )
            )
            for table in data.get("tables") or []
        )
    if native_format == "semantic-view":
        return _additional_members(data, "snowflake") + len(data.get("metrics") or [])
    if native_format == "thoughtspot":
        return len(
            (data.get("table") or data.get("sql_view") or {}).get("columns") or []
        ) + len((data.get("model") or {}).get("formulas") or [])
    if native_format == "wisdom":
        return sum(
            sum(
                len((sheet.get("zsheet_json") or {}).get(key) or [])
                for key in ("columns", "formulas", "measures")
            )
            for sheet in data.get("tables") or []
        )
    if native_format == "salesforce":
        return sum(
            len(obj.get("semanticDimensions") or [])
            + len(obj.get("semanticMeasurements") or [])
            for obj in data.get("semanticDataObjects") or []
        ) + len(data.get("semanticCalculatedMeasurements") or [])
    raise ContentError(f"no native inspector for {native_format}")


def compare_inventory(before, after):
    """Named structural evidence, not an assertion of general SQL equivalence.

    Names alone can change with no semantic loss. Report differences conservatively
    as changes, and retain both complete inventories for inspection.
    """

    def items(data):
        result = Counter()

        def extensions(node, *owner):
            for extension in node.get("custom_extensions") or []:
                result[
                    ("extension", *owner, extension["vendor_name"], extension["data"])
                ] += 1

        extensions(data, "model")
        for ds in data.get("datasets") or []:
            name = ds["name"]
            result[("dataset", name, ds["source"])] += 1
            extensions(ds, "dataset", name)
            for field in ds.get("fields") or []:
                result[("field", name, field["name"])] += 1
                extensions(field, "field", name, field["name"])
            if ds.get("primary_key"):
                result[("primary_key", name, *sorted(ds["primary_key"]))] += 1
            for key in ds.get("unique_keys") or []:
                result[("unique_key", name, *sorted(key))] += 1
        for metric in data.get("metrics") or []:
            result[("metric", metric["name"])] += 1
            extensions(metric, "metric", metric["name"])
        for rel in data.get("relationships") or []:
            extensions(rel, "relationship", rel["name"])
            pairs = sorted(zip(rel["from_columns"], rel["to_columns"]))
            result[
                (
                    "relationship",
                    rel["from"],
                    rel["to"],
                    *[f"{a}={b}" for a, b in pairs],
                )
            ] += 1
        return result

    left, right = items(before), items(after)
    return {
        "removed": [list(x) for x in sorted((left - right).elements())],
        "added": [list(x) for x in sorted((right - left).elements())],
        "unchecked": [
            "SQL expression equivalence and physical metric bindings (except separately registered native regressions)",
            "metadata, data types, and AI context",
        ],
    }
