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

"""Test helpers: schema loading and order-insensitive comparison."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

_EXTERNAL_REF = re.compile(r'"\$ref"\s*:\s*"(https://[^"#]+)')
_WHITESPACE = re.compile(r"\s+")

#: Keys that identify an item within a list, so lists can be compared as sets.
_IDENTITY_KEYS = ("concept", "name", "relationship", "dialect")


def ontology_validator(ossie_root: Path) -> Draft202012Validator:
    """A validator for ``ontology/ontology.json`` from the pinned ``ossie`` submodule.

    The ontology schema refers to the core spec schema by absolute URL. The core
    spec is resolved from the submodule checkout rather than fetched, so the
    test suite validates against the pinned schemas and runs offline. Run
    ``git submodule update --init`` first.
    """
    ontology_path = ossie_root / "ontology" / "ontology.json"
    core_path = ossie_root / "core-spec" / "ossie-schema.json"
    if not ontology_path.is_file():
        raise FileNotFoundError(f"{ontology_path} missing: run `git submodule update --init ossie`")
    ontology_text = ontology_path.read_text(encoding="utf-8")

    core = Resource.from_contents(json.loads(core_path.read_text(encoding="utf-8")))
    registry = Registry().with_resources(
        [(url, core) for url in sorted(set(_EXTERNAL_REF.findall(ontology_text)))]
    )
    return Draft202012Validator(json.loads(ontology_text), registry=registry)


def schema_errors(validator: Draft202012Validator, document: dict[str, Any]) -> list[str]:
    """Human-readable schema errors, in document order."""
    return [
        f"{'/'.join(str(p) for p in error.path)}: {error.message}"
        for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path))
    ]


def _sort_key(item: Any) -> tuple[str, str]:
    if isinstance(item, dict):
        for key in _IDENTITY_KEYS:
            if key in item:
                return (key, str(item[key]))
    return ("", json.dumps(item, sort_keys=True, default=str))


def canonical(node: Any) -> Any:
    """Normalize a document for semantic comparison.

    Two normalizations, both of them things the ontology spec does not give
    meaning to: whitespace inside prose (a folded YAML scalar and a wrapped
    plain scalar are the same description), and the order of items in a list of
    named objects (concepts, relationships, fields).
    """
    if isinstance(node, dict):
        return {key: canonical(value) for key, value in node.items()}
    if isinstance(node, list):
        items = [canonical(value) for value in node]
        if items and all(isinstance(item, dict) for item in items):
            return sorted(items, key=_sort_key)
        return items
    if isinstance(node, str):
        return _WHITESPACE.sub(" ", node).strip()
    return node


def describe_difference(left: Any, right: Any, path: str = "") -> list[str]:
    """Differences between two canonical documents, as readable lines."""
    differences: list[str] = []
    if type(left) is not type(right):
        return [f"{path or '<root>'}: {type(left).__name__} vs {type(right).__name__}"]
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                differences.append(f"{path}.{key}: missing from generated")
            elif key not in right:
                differences.append(f"{path}.{key}: unexpected in generated")
            else:
                differences += describe_difference(left[key], right[key], f"{path}.{key}")
    elif isinstance(left, list):
        if len(left) != len(right):
            left_keys = [_sort_key(i)[1] for i in left]
            right_keys = [_sort_key(i)[1] for i in right]
            extra = sorted(set(left_keys) - set(right_keys))
            missing = sorted(set(right_keys) - set(left_keys))
            differences.append(f"{path}: {len(left)} vs {len(right)} items; extra={extra} missing={missing}")
        else:
            for index, (a, b) in enumerate(zip(left, right)):
                differences += describe_difference(a, b, f"{path}[{index}]")
    elif left != right:
        differences.append(f"{path}:\n    generated: {left!r}\n    expected:  {right!r}")
    return differences
