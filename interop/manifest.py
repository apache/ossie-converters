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

"""The small, versioned contract between the hub runner and converter CLIs."""

from dataclasses import dataclass, field
from pathlib import Path
import re
from string import Formatter

import yaml


class ManifestError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    """Reject duplicate configuration keys instead of silently losing entries."""


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ManifestError("mapping keys must be strings")
        if key in result:
            raise ManifestError(f"duplicate key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


@dataclass(frozen=True)
class Converter:
    name: str
    cwd: Path
    toolchain: str
    prepare: list[str]
    import_command: list[str] | None
    export_command: list[str] | None
    native_format: str
    output: str
    fixtures: dict[str, Path]
    native_gate: dict | None
    skip: str | None = None
    import_skip: str | None = None
    roundtrip_skip: str | None = None
    import_requires_files: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class BindingRegression:
    source: str
    fixture: str
    via: str
    expectations: Path
    metrics: dict[str, list[dict]]


@dataclass(frozen=True)
class DocumentRegression:
    category: str
    input: Path
    targets: list[str]
    expectations: Path
    assertions: dict


@dataclass(frozen=True)
class Manifest:
    converters: dict[str, Converter]
    canonical: dict[str, Path]
    regressions: dict[str, BindingRegression] = field(default_factory=dict)
    document_regressions: dict[str, DocumentRegression] = field(default_factory=dict)


# Format names describe exported artifacts, independently of the converter name.
NATIVE_SHAPES = {
    **dict.fromkeys(
        ("cube", "hex", "honeydew", "omni", "holistics", "thoughtspot"), "directory"
    ),
    **dict.fromkeys(
        (
            "dbt",
            "databricks",
            "gooddata",
            "microsoft",
            "nvidia",
            "orionbelt",
            "sigma",
            "snowflake",
            "wisdom",
            "semantic-view",
            "salesforce",
        ),
        "file",
    ),
}


def _keys(value, required, optional, where):
    if not isinstance(value, dict):
        raise ManifestError(f"{where}: expected a mapping")
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing or unknown:
        raise ManifestError(
            f"{where}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}"
        )


def _name(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]*", value):
        raise ManifestError(f"invalid identifier: {value!r}")
    return value


def _path(root, value, *, must_exist=True):
    if not isinstance(value, str):
        raise ManifestError("paths must be strings")
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or (must_exist and not path.exists()):
        raise ManifestError(f"path must exist inside the repository: {value}")
    return path


def _command(value, allowed, required=()):
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(x, str) or not x for x in value)
    ):
        raise ManifestError(f"command must be a non-empty argument array: {value!r}")
    fields = set()
    try:
        for argument in value:
            for _, field, spec, conversion in Formatter().parse(argument):
                if field is not None:
                    if field not in allowed or spec or conversion:
                        raise ManifestError(f"unsupported command placeholder: {field}")
                    if field == "files" and argument != "{files}":
                        raise ManifestError("{files} must occupy a complete argument")
                    fields.add(field)
    except ValueError as error:
        raise ManifestError(str(error)) from error
    if not set(required) <= fields:
        raise ManifestError(f"command requires placeholders: {sorted(required)}")
    return value


def _strings(value, where, *, nonempty=True):
    if (
        not isinstance(value, list)
        or (nonempty and not value)
        or any(not isinstance(item, str) or not item for item in value)
        or len(value) != len(set(value))
    ):
        raise ManifestError(f"{where}: expected distinct non-empty strings")


def _document_assertions(path, category):
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ManifestError(str(error)) from error
    required = (
        {"version", "dialect", "datasets", "metrics"}
        if category == "dialect"
        else {"version", "datasets"}
        if category == "keys"
        else {"version", "sources"}
    )
    _keys(data, required, set(), str(path))
    if type(data["version"]) is not int or data["version"] != 1:
        raise ManifestError("unsupported document expectations version")
    if category == "sources":
        _strings(data["sources"], "sources")
    else:
        if not isinstance(data["datasets"], dict) or not data["datasets"]:
            raise ManifestError("datasets must be a non-empty assertion mapping")
        for name, entry in data["datasets"].items():
            _name(name)
            if category == "keys":
                _keys(entry, {"primary_key", "unique_keys"}, set(), name)
                _strings(entry["primary_key"], name, nonempty=False)
                if not isinstance(entry["unique_keys"], list):
                    raise ManifestError(
                        f"{name}: unique_keys must be an array of key arrays"
                    )
                for key in entry["unique_keys"]:
                    _strings(key, name)
                if len({tuple(sorted(key)) for key in entry["unique_keys"]}) != len(
                    entry["unique_keys"]
                ):
                    raise ManifestError(f"{name}: duplicate unique-key assertion")
            elif (
                not isinstance(entry, dict)
                or not entry
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(sql, str)
                    or not sql
                    for key, sql in entry.items()
                )
            ):
                raise ManifestError(
                    f"{name}: expected a non-empty field-to-SQL mapping"
                )
        if category == "dialect":
            if data["dialect"] not in ("DATABRICKS", "SNOWFLAKE", "OSSIE_SQL_2026"):
                raise ManifestError("unsupported dialect-only assertion dialect")
            if len(data["datasets"]) != 1:
                raise ManifestError(
                    "dialect expression assertions currently require one dataset"
                )
            metrics = data["metrics"]
            if (
                not isinstance(metrics, dict)
                or not metrics
                or any(
                    not isinstance(key, str)
                    or not key
                    or not isinstance(sql, str)
                    or not sql
                    for key, sql in metrics.items()
                )
            ):
                raise ManifestError("metrics must be a non-empty metric-to-SQL mapping")
    return data


def load_manifest(path: Path, root: Path) -> Manifest:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    except (OSError, yaml.YAMLError) as error:
        raise ManifestError(str(error)) from error
    _keys(
        data,
        {"version", "canonical", "converters"},
        {"regressions", "document_regressions"},
        "manifest",
    )
    if type(data["version"]) is not int or data["version"] != 1:
        raise ManifestError("unsupported manifest version (expected 1)")
    if not isinstance(data["canonical"], dict) or not data["canonical"]:
        raise ManifestError("canonical must be a non-empty fixture mapping")
    canonical = {
        _name(name): _path(root, value) for name, value in data["canonical"].items()
    }
    if any(not path.is_file() for path in canonical.values()):
        raise ManifestError("canonical fixtures must be files")
    if not isinstance(data["converters"], dict) or not data["converters"]:
        raise ManifestError("converters must be a non-empty mapping")
    converters = {}
    for name, entry in data["converters"].items():
        _name(name)
        if name == "canonical":
            raise ManifestError("canonical is reserved for core examples")
        if isinstance(entry, dict) and "skip" in entry:
            _keys(entry, {"cwd", "skip"}, set(), name)
            if not isinstance(entry["skip"], str) or not entry["skip"].strip():
                raise ManifestError(f"{name}: skip requires an explicit reason")
            cwd = _path(root, entry["cwd"])
            if not cwd.is_dir():
                raise ManifestError(f"{name}: cwd must be a directory")
            converters[name] = Converter(
                name,
                cwd,
                "none",
                [],
                None,
                None,
                "unavailable",
                "file",
                {},
                None,
                skip=entry["skip"],
            )
            continue
        _keys(
            entry,
            {
                "cwd",
                "toolchain",
                "prepare",
                "import",
                "export",
                "native_format",
                "output",
                "fixtures",
            },
            {"native_gate", "import_skip", "roundtrip_skip", "import_requires_files"},
            name,
        )
        if not isinstance(entry["toolchain"], str) or entry["toolchain"] not in (
            "uv",
            "maven",
        ):
            raise ManifestError(f"{name}: unsupported toolchain")
        if (
            not isinstance(entry["native_format"], str)
            or entry["native_format"] not in NATIVE_SHAPES
        ):
            raise ManifestError(f"{name}: unsupported native format")
        if entry["output"] not in ("file", "directory"):
            raise ManifestError(f"{name}: output must be file or directory")
        if entry["output"] != NATIVE_SHAPES[entry["native_format"]]:
            raise ManifestError(f"{name}: output shape does not match native format")
        for key in ("import_skip", "roundtrip_skip"):
            if key in entry and (
                not isinstance(entry[key], str) or not entry[key].strip()
            ):
                raise ManifestError(f"{name}: {key} requires an explicit reason")
        if (entry["import"] is None) != bool(entry.get("import_skip")):
            raise ManifestError(
                f"{name}: null import requires import_skip; supported import cannot have it"
            )
        if not isinstance(entry["fixtures"], dict) or (
            not entry["fixtures"] and entry["import"] is not None
        ):
            raise ManifestError(
                f"{name}: importing converters require a non-empty fixture mapping"
            )
        if entry["import"] is None and entry["fixtures"]:
            raise ManifestError(
                f"{name}: unsupported imports cannot register native fixtures"
            )
        fixtures = {
            _name(key): _path(root, value) for key, value in entry["fixtures"].items()
        }
        prerequisites = entry.get("import_requires_files", [])
        _strings(prerequisites, f"{name}.import_requires_files", nonempty=False)
        import_requires_files = [
            _path(root, value, must_exist=False) for value in prerequisites
        ]
        if import_requires_files and entry["import"] is None:
            raise ManifestError(
                f"{name}: unsupported import cannot have file prerequisites"
            )
        cwd = _path(root, entry["cwd"])
        if not cwd.is_dir():
            raise ManifestError(f"{name}: cwd must be a directory")
        gate = entry.get("native_gate")
        if gate is not None:
            _keys(gate, {"command", "requires_env"}, set(), f"{name}.native_gate")
            if not isinstance(gate["requires_env"], str) or not gate["requires_env"]:
                raise ManifestError("native gate requires an environment variable name")
            _command(gate["command"], {"root", "input", "files"})
        converters[name] = Converter(
            name,
            cwd,
            entry["toolchain"],
            _command(entry["prepare"], {"root"}),
            _command(entry["import"], {"root", "input", "output"}, {"input", "output"})
            if entry["import"] is not None
            else None,
            _command(entry["export"], {"root", "input", "output"}, {"input", "output"}),
            entry["native_format"],
            entry["output"],
            fixtures,
            gate,
            import_skip=entry.get("import_skip"),
            roundtrip_skip=entry.get("roundtrip_skip"),
            import_requires_files=import_requires_files,
        )
    regressions = {}
    entries = data.get("regressions", {})
    if not isinstance(entries, dict):
        raise ManifestError("regressions must be a mapping")
    for name, entry in entries.items():
        _name(name)
        _keys(entry, {"source", "fixture", "via", "expectations"}, set(), name)
        source, fixture, via = (
            _name(entry[key]) for key in ("source", "fixture", "via")
        )
        if source not in converters or via not in converters or source == via:
            raise ManifestError(
                f"{name}: regression requires distinct registered source/via converters"
            )
        if converters[source].native_format != "cube":
            raise ManifestError(
                f"{name}: native binding inspector currently supports Cube sources"
            )
        if fixture not in converters[source].fixtures:
            raise ManifestError(f"{name}: unknown source fixture {fixture}")
        path = _path(root, entry["expectations"])
        try:
            expectations = yaml.load(
                path.read_text(encoding="utf-8"), Loader=UniqueLoader
            )
        except (OSError, yaml.YAMLError) as error:
            raise ManifestError(str(error)) from error
        _keys(expectations, {"version", "metrics"}, set(), str(path))
        if type(expectations["version"]) is not int or expectations["version"] != 1:
            raise ManifestError(f"{name}: unsupported binding expectations version")
        metrics = expectations["metrics"]
        if not isinstance(metrics, dict) or not metrics:
            raise ManifestError(f"{name}: expected non-empty metric binding assertions")
        for metric, bindings in metrics.items():
            _name(metric)
            if not isinstance(bindings, list) or not bindings:
                raise ManifestError(f"{name}.{metric}: expected non-empty binding list")
            for binding in bindings:
                _keys(binding, {"function", "source", "column", "grain"}, set(), metric)
                if any(
                    not isinstance(value, str) or not value
                    for value in binding.values()
                ):
                    raise ManifestError(
                        f"{metric}: binding values must be non-empty strings"
                    )
                if binding["function"] not in {
                    "sum",
                    "avg",
                    "min",
                    "max",
                    "count",
                    "count_distinct",
                }:
                    raise ManifestError(
                        f"{metric}: unsupported binding aggregate function"
                    )
        regressions[name] = BindingRegression(source, fixture, via, path, metrics)
    documents = {}
    entries = data.get("document_regressions", {})
    if not isinstance(entries, dict):
        raise ManifestError("document_regressions must be a mapping")
    for name, entry in entries.items():
        _name(name)
        _keys(entry, {"category", "input", "targets", "expectations"}, set(), name)
        if entry["category"] not in ("dialect", "keys", "sources"):
            raise ManifestError(f"{name}: unsupported document regression category")
        _strings(entry["targets"], name)
        if set(entry["targets"]) - converters.keys():
            raise ManifestError(f"{name}: target converter is not registered")
        input_path, expectations = (
            _path(root, entry[key]) for key in ("input", "expectations")
        )
        if not input_path.is_file() or not expectations.is_file():
            raise ManifestError(f"{name}: input and expectations must be files")
        documents[name] = DocumentRegression(
            entry["category"],
            input_path,
            entry["targets"],
            expectations,
            _document_assertions(expectations, entry["category"]),
        )
    return Manifest(converters, canonical, regressions, documents)


def expand(command, root, input_path=None, output_path=None, files=()):
    values = {"root": str(root), "input": str(input_path), "output": str(output_path)}
    return [
        part
        for arg in command
        for part in (
            [str(path) for path in files]
            if arg == "{files}"
            else [arg.format(**values)]
        )
    ]
