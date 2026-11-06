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

"""Conservative provenance for publicly queryable, generated ratio helpers."""

from copy import deepcopy
import hashlib
import json
import re
from collections import Counter
from typing import Dict, List, Sequence

from metricflow_semantic_interfaces.implementations.element_config import PydanticSemanticLayerElementConfig
from metricflow_semantic_interfaces.implementations.metric import PydanticMetric
from metricflow_semantic_interfaces.implementations.saved_query import PydanticSavedQuery

_KEY = "generated_ratio_helpers"


def _definition(metric: PydanticMetric) -> dict:
    # Source-location metadata is not semantic and may change on serialization.
    value = json.loads(metric.json(exclude_none=True, exclude={"metadata"}))
    config = value.get("config", {})
    meta = config.get("meta", {})
    ossie = meta.get("ossie")
    if isinstance(ossie, dict):
        ossie.pop(_KEY, None)
        if not ossie:
            meta.pop("ossie", None)
    if not meta:
        config.pop("meta", None)
    if not config:
        value.pop("config", None)
    return value


def _signature(metrics: Sequence[PydanticMetric]) -> str:
    payload = [_definition(metric) for metric in sorted(metrics, key=lambda metric: metric.name)]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def mark_ratio_helpers(metrics: List[PydanticMetric]) -> None:
    """Record the exact generated family, without changing its query visibility."""
    if len(metrics) < 2:
        return
    owner = metrics[-1]
    marker = {
        "version": 1,
        "names": [metric.name for metric in metrics[:-1]],
        "signature": _signature(metrics),
    }
    config = deepcopy(owner.config) if owner.config is not None else PydanticSemanticLayerElementConfig()
    # Generated metrics currently have no config; retain any future annotations.
    ossie = config.meta.setdefault("ossie", {})
    if not isinstance(ossie, dict):
        return
    ossie[_KEY] = marker
    owner.config = config


def unchanged_ratio_helpers(
    metrics: Sequence[PydanticMetric], saved_queries: Sequence[PydanticSavedQuery] = ()
) -> Dict[str, List[str]]:
    """Return unchanged, isolated generated families; stale metadata keeps metrics.

    Compare before MetricFlow transforms definitions (e.g. COUNT into SUM). Scan
    all external serialized definitions conservatively for helper names, including
    SQL/Jinja filters, saved queries, and new input kinds, without duplicating
    resolver shapes.
    Unmarked/legacy projects and user-written lookalike names are never folded.
    """
    index = {metric.name: metric for metric in metrics}
    counts = Counter(metric.name for metric in metrics)
    families = {}
    for owner in metrics:
        ossie = owner.config.meta.get("ossie") if owner.config is not None else None
        marker = ossie.get(_KEY) if isinstance(ossie, dict) else None
        if not isinstance(marker, dict) or marker.get("version") != 1:
            continue
        names = marker.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(name, str) for name in names):
            continue
        group = {owner.name, *names}
        if len(group) != len(names) + 1 or any(counts[name] != 1 for name in group):
            continue
        if marker.get("signature") != _signature([index[name] for name in group]):
            continue
        pattern = re.compile(
            r"(?<![A-Za-z0-9_])(?:" + "|".join(re.escape(name) for name in names) + r")(?![A-Za-z0-9_])"
        )
        if any(pattern.search(json.dumps(_definition(metric))) for metric in metrics if metric.name not in group):
            continue
        if any(pattern.search(query.json()) for query in saved_queries):
            continue
        families[owner.name] = names
    return families
