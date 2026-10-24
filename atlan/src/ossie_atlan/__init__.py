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

"""Bidirectional converter between Apache Ossie semantic models and Atlan metric registry
records. Pure offline transforms on dicts; the CLI reads and writes files.

    from ossie_atlan import convert_registry_to_ossie, convert_ossie_to_registry, write_back

    doc, issues = convert_registry_to_ossie(records)        # list of registry records -> Ossie dict
    definition = convert_ossie_to_registry(doc, "metric")   # one Ossie metric -> registry definition
    patch = write_back(doc, "metric", record)               # the registry patch an Ossie edit implies
"""

from __future__ import annotations

from . import converter as _c
from ._common import ConversionError, load
from .converter import Conflict, NotConvertible, NotWritable, agent_context, export
from .converter_issues import ConverterIssue, IssueLog, IssueType

OSSIE_VERSION = "0.2.0.dev0"


def convert_registry_to_ossie(records: list[dict], name: str = "atlan_registry",
                              description: str | None = None) -> tuple[dict, IssueLog]:
    """Registry records -> one Ossie document (strict form) plus the issues.

    Each record converts on its own; one that Ossie cannot express becomes a
    METRIC_NOT_CONVERTIBLE issue and the rest still convert. Records of one workspace share
    datasets and fields; two records that define one name differently raise a MERGE_CONFLICT
    and the later one is left out. Records of several workspaces cannot share a file, since
    the workspace is model-level: that is a ConversionError.
    """
    issues, parts = IssueLog(), []
    for r in records:
        try:
            parts += _c.convert(r)
        except NotConvertible as e:
            issues.add(IssueType.METRIC_NOT_CONVERTIBLE, (r.get("name") if isinstance(r, dict) else None) or "?", str(e))
    ws = {r.get("workspace_id") for r in records if isinstance(r, dict)} - {None}
    if len(ws) > 1:
        raise ConversionError(f"records of {len(ws)} workspaces in one file; convert one workspace at a time")
    if not parts:
        return {"version": OSSIE_VERSION, "name": name, "datasets": [], "metrics": []}, issues
    merged = _c.merge(parts, name=name, description=description, workspace_id=ws.pop() if ws else None,
                      on_conflict="skip")
    for kind, metric, why in merged.pop("_merge_notes", []):
        issues.add(IssueType.MERGE_CONFLICT if kind == "left_out" else IssueType.PROSE_DIFFERENCE_KEPT_FIRST, metric, why)
    return export(merged), issues


def convert_ossie_to_registry(doc: dict, metric: str) -> dict:
    """One Ossie metric -> a registry definition (the new-metric case: no registry id yet)."""
    try:
        return _c.to_registry(load(doc), metric)
    except (LookupError, TypeError) as e:
        raise ConversionError(str(e)) from None


def write_back(doc: dict, metric: str, record: dict, registry_version: int | None = None) -> dict:
    """The registry patch an edited Ossie file implies for an existing record ({} if unchanged).
    `registry_version` is the registry's current definition version when known; a file built
    from another version is a Conflict."""
    try:
        return _c.write_back(load(doc), metric, record, registry_version)
    except (LookupError, TypeError) as e:
        raise ConversionError(str(e)) from None


__all__ = [
    "Conflict",
    "ConversionError",
    "ConverterIssue",
    "IssueLog",
    "IssueType",
    "NotConvertible",
    "NotWritable",
    "agent_context",
    "convert_ossie_to_registry",
    "convert_registry_to_ossie",
    "write_back",
]
