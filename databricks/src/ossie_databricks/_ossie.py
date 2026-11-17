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

"""Access to the Ossie checkout: its validator, its YAML loader, and its dialect table.

The package reuses `validation/validate.py` instead of re-implementing Ossie's checks. The
checkout is `$OSSIE_REPO`, or else the `ossie` submodule of this repository.
"""

import importlib.util
import os
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from ossie_databricks.result import ConversionError


@cache
def _validator() -> ModuleType:
    candidates = [os.environ.get("OSSIE_REPO"), Path(__file__).resolve().parents[3] / "ossie"]
    for root in filter(None, candidates):
        script = Path(root) / "validation" / "validate.py"
        if script.is_file():
            spec = importlib.util.spec_from_file_location("ossie_validate", script)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            return module
    raise ConversionError(
        "cannot find an apache/ossie checkout: set OSSIE_REPO to its root directory"
    )


def sqlglot_dialect(dialect: str) -> str | None:
    """The sqlglot dialect that the Ossie validator parses `dialect` with."""
    dialects = _validator().SQLGLOT_DIALECTS
    if dialect not in dialects:
        raise KeyError(dialect)
    return dialects[dialect]


def load(source: str | Mapping[str, Any]) -> dict[str, Any]:
    """Read a YAML document, rejecting duplicate keys as the Ossie validator does."""
    if isinstance(source, Mapping):
        return dict(source)
    try:
        data = yaml.load(source, Loader=_validator().UniqueKeyLoader)
    except yaml.YAMLError as error:
        raise ConversionError(f"invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise ConversionError("the document is not a YAML mapping")
    return data


class _Dumper(yaml.SafeDumper):
    """Writes multi-line text, such as a query source, as a block."""


_Dumper.add_representer(
    str,
    lambda dumper, text: dumper.represent_scalar(
        "tag:yaml.org,2002:str", text, style="|" if "\n" in text else None
    ),
)


def dump(data: Mapping[str, Any]) -> str:
    return yaml.dump(dict(data), Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100)


@cache
def _schema() -> dict[str, Any]:
    module = _validator()
    schema = module.load_schema_or_report(module.default_schema_path())
    if schema is None:
        raise ConversionError("the Ossie checkout has no readable core-spec/ossie-schema.json")
    return schema


def spec_version() -> str:
    return _schema()["properties"]["version"]["const"]


def validate(model: Mapping[str, Any]) -> list[str]:
    """The errors that the Ossie validator reports for `model`. Its warnings are left out."""
    module = _validator()
    return [
        str(error)
        for error in module.validate_document(dict(model), _schema())
        if not isinstance(error, module.ValidationWarning)
    ]


def check(model: Mapping[str, Any], what: str) -> None:
    errors = validate(model)
    if errors:
        raise ConversionError(f"{what} is not a valid Ossie model:\n  " + "\n  ".join(errors))
