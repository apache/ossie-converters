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

"""Resolve the static subset of Cube source SQL without evaluating model code."""

import re

from ._common import (ConversionError, _ESC_CLOSE, _ESC_OPEN,
                      join_source, read_stash)

# Cube YAML evaluates braces as f-strings; a preceding dollar is literal text.
_SOURCE_REF_RE = re.compile(r"\{\s*([^{}]*?)\s*\}")
_SQL_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.sql\(\)\Z")
# Match Cube's BaseQuery.cubeSql optimization exactly; other SQL is a subquery.
_SELECT_ALL = re.compile(r'^\s*select\s+\*\s+from\s+([a-zA-Z0-9_\-`".*]+)\s*$', re.I)
_MAX_EXPANSION = 1_000_000
_MAX_DEPTH = 100


class SourceResolver:
    """Cache resolved sources and the source bindings needed for native restoration."""

    def __init__(self, cubes, **source_options):
        self.cubes = cubes
        self.options = source_options
        self.sources = {}
        self.dependencies = {}
        self.depths = {}
        self.templates = {}
        self.visiting = []

    def resolve(self, name):
        if name in self.sources:
            return self.sources[name]
        if name in self.visiting:
            chain = self.visiting[self.visiting.index(name):] + [name]
            raise ConversionError("Cube source dependency cycle: " + " -> ".join(chain))
        if len(self.visiting) >= _MAX_DEPTH:
            raise ConversionError(f"Cube '{name}': source dependency depth exceeds the static expansion limit")
        if name not in self.cubes:
            raise ConversionError(
                f"Cube '{self.visiting[-1]}': source references '{name}', which is not a cube in this model")
        cube = self.cubes[name]
        raw = join_source(cube, name, **self.options)
        protected = raw.replace("\\{", _ESC_OPEN).replace("\\}", _ESC_CLOSE)
        if re.search(r"[{}]", _SOURCE_REF_RE.sub("", protected)):
            raise ConversionError(f"Cube '{name}': source contains an unsupported static template")
        self.visiting.append(name)
        dependencies = {name}
        depth = 1
        parts, cursor, size = [], 0, 0
        try:
            for match in _SOURCE_REF_RE.finditer(protected):
                call = _SQL_CALL.fullmatch(match.group(1).strip())
                if call is None:
                    raise ConversionError(
                        f"Cube '{name}': source template {match.group(0)!r} has no supported static form; "
                        "only named cube.sql() references can be resolved offline")
                target = call.group(1)
                source = self.resolve(target)
                dependencies.update(self.dependencies[target])
                depth = max(depth, self.depths[target] + 1)
                if depth > _MAX_DEPTH:
                    raise ConversionError(f"Cube '{name}': source dependency depth exceeds the static expansion limit")
                if self.cubes[target].get("sql_table") is None:
                    table = _SELECT_ALL.fullmatch(source)
                    source = table.group(1) if table else f"({source})"
                chunk = protected[cursor:match.start()] + source
                size += len(chunk)
                if size + len(protected) - match.end() > _MAX_EXPANSION:
                    raise ConversionError(f"Cube '{name}': source SQL expansion exceeds the static size limit")
                parts.append(chunk)
                cursor = match.end()
            parts.append(protected[cursor:])
            resolved = "".join(parts)
            resolved = resolved.replace(_ESC_OPEN, "{").replace(_ESC_CLOSE, "}")
            self.sources[name] = resolved
            self.dependencies[name] = dependencies
            self.depths[name] = depth
            if cursor:
                kind = "sql_table" if cube.get("sql_table") is not None else "sql"
                self.templates[name] = {"kind": kind, "value": cube[kind],
                                        "sources": {n: self.sources[n] for n in sorted(dependencies)}}
            return resolved
        finally:
            self.visiting.pop()


def restore_source_templates(cubes, datasets, cube_names):
    """Restore native templates only while every referenced source binding matches.

    A changed, removed or renamed dependency must not redirect an otherwise
    unchanged dataset to different rows when it returns to Cube.
    """
    for name, ds in datasets.items():
        template = read_stash(ds).get("source_template")
        if not template:
            continue
        sources = template.get("sources") or {}
        if name not in sources or not all(
                dep in datasets and cube_names.get(dep) == dep
                and datasets[dep].get("source") == source for dep, source in sources.items()):
            continue
        kind = template.get("kind")
        if kind not in ("sql", "sql_table"):
            continue
        cube = cubes[cube_names[name]]
        cube.pop("sql", None)
        cube.pop("sql_table", None)
        cube[kind] = template["value"]
