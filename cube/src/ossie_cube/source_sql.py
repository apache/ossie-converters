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

import hashlib
import re

from ._common import (ConversionError, _ESC_CLOSE, _ESC_OPEN,
                      join_source, read_stash, skipped_files_hint)

# Cube YAML evaluates braces as f-strings; a preceding dollar is literal text.
_SOURCE_REF_RE = re.compile(r"\{\s*([^{}]*?)\s*\}")
_SQL_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\.sql\(\)\Z")
# Match Cube's BaseQuery.cubeSql optimization exactly; other SQL is a subquery.
_SELECT_ALL = re.compile(r'^\s*select\s+\*\s+from\s+([a-zA-Z0-9_\-`".*]+)\s*$', re.I)
_MAX_EXPANSION = 1_000_000
_MAX_DEPTH = 100


def _protect(text):
    """Hide braces escaped for Cube: they are literal SQL, not templates."""
    return text.replace("\\{", _ESC_OPEN).replace("\\}", _ESC_CLOSE)


def _unprotect(text):
    return text.replace(_ESC_OPEN, "{").replace(_ESC_CLOSE, "}")


def _static_problem(protected):
    """Why protected source text has no static form, or None when it has one."""
    if re.search(r"[{}]", _SOURCE_REF_RE.sub("", protected)):
        return "source contains an unsupported static template"
    for match in _SOURCE_REF_RE.finditer(protected):
        if _SQL_CALL.fullmatch(match.group(1).strip()) is None:
            return (f"source template {_unprotect(match.group(0))!r} has no supported "
                    "static form; only named cube.sql() references can be resolved offline")
    return None


def _referenced_cube(match):
    return _SQL_CALL.fullmatch(match.group(1).strip()).group(1)


def source_signature(source):
    """Change guard for a source a native template was resolved against."""
    return hashlib.sha256(str(source).encode("utf-8")).hexdigest()


def unresolvable_sources(cubes, skipped_files=()):
    """The cubes whose source has no static form, mapped to the reason, in model order.

    A runtime template (`{FILTER_PARAMS...}`, `{COMPILE_CONTEXT...}`, `{CUBE}`) is only
    known when Cube compiles a query. A `{cube.sql()}` reference to such a cube, or to
    a cube that only a skipped file can define, has no static form either. A reference
    to a cube defined nowhere is left to `SourceResolver`, which refuses it, as Cube
    would not compile that model.
    """
    reasons, references = {}, {}
    for name, cube in cubes.items():
        table, sql = cube.get("sql_table"), cube.get("sql")
        if (table is None) == (sql is None):
            continue  # `join_source` reports a missing or doubled source.
        protected = _protect(str(sql if table is None else table))
        problem = _static_problem(protected)
        if problem is not None:
            reasons[name] = problem
        else:
            references[name] = [_referenced_cube(m) for m in _SOURCE_REF_RE.finditer(protected)]
    changed = True
    while changed:
        changed = False
        for name, targets in references.items():
            if name in reasons:
                continue
            for target in targets:
                if target in reasons:
                    reasons[name] = (f"source references '{target}.sql()', and cube "
                                     f"'{target}' has no static source")
                elif target not in cubes and skipped_files:
                    reasons[name] = (f"source references '{target}', which is not a cube "
                                     f"in a converted file"
                                     + skipped_files_hint(target, skipped_files))
                else:
                    continue
                changed = True
                break
    return {name: reasons[name] for name in cubes if name in reasons}


class SourceResolver:
    """Cache resolved sources and the source bindings needed for native restoration."""

    def __init__(self, cubes, *, skipped_files=(), **source_options):
        self.cubes = cubes
        self.skipped_files = skipped_files
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
                f"Cube '{self.visiting[-1]}': source references '{name}', which is not a cube "
                f"in this model{skipped_files_hint(name, self.skipped_files)}")
        cube = self.cubes[name]
        raw = join_source(cube, name, **self.options)
        protected = _protect(raw)
        problem = _static_problem(protected)
        if problem is not None:
            raise ConversionError(f"Cube '{name}': {problem}")
        self.visiting.append(name)
        dependencies = {name}
        depth = 1
        parts, cursor, size = [], 0, 0
        try:
            for match in _SOURCE_REF_RE.finditer(protected):
                target = _referenced_cube(match)
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
            resolved = _unprotect("".join(parts))
            self.sources[name] = resolved
            self.dependencies[name] = dependencies
            self.depths[name] = depth
            if cursor:
                kind = "sql_table" if cube.get("sql_table") is not None else "sql"
                # Signatures, not the expanded text: a deep chain would otherwise
                # repeat every dependency's full source in each dependent's stash.
                self.templates[name] = {
                    "kind": kind, "value": cube[kind],
                    "source_signatures": {n: source_signature(self.sources[n])
                                          for n in sorted(dependencies)}}
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
        if not isinstance(template, dict):
            continue
        signatures = template.get("source_signatures")
        kind = template.get("kind")
        value = template.get("value")
        # Vendor extensions may be hand-edited or come from another tool. An
        # incomplete snapshot cannot safely replace the portable source.
        if (kind not in ("sql", "sql_table") or not isinstance(value, str)
                or not value.strip() or not isinstance(signatures, dict)
                or not all(isinstance(dep, str) and isinstance(signature, str)
                           for dep, signature in signatures.items())):
            continue
        if name not in signatures or not all(
                dep in datasets and cube_names.get(dep) == dep
                and isinstance(datasets[dep].get("source"), str)
                and source_signature(datasets[dep]["source"]) == signature
                for dep, signature in signatures.items()):
            continue
        cube = cubes[cube_names[name]]
        # In place, so the restored source keeps the position the export ordered it at.
        items = [(kind, value) if key in ("sql", "sql_table") else (key, item)
                 for key, item in cube.items()]
        cube.clear()
        cube.update(items)
