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

"""The interchange verbs. Databricks reads Ossie models as they are, so both are light."""

import copy
from collections.abc import Iterator, Mapping
from typing import Any

from ossie_databricks import _ossie
from ossie_databricks.expressions import (
    DATABRICKS,
    OSSIE,
    Untranslatable,
    measure_names,
    parse,
    translate,
)
from ossie_databricks.result import Result, WarningCode

# The dialects that `sql` is translated from, in order.
_TRANSLATED_DIALECTS = (DATABRICKS, "ANSI_SQL")


def convert_ossie_model_to_databricks(ossie_model: str | Mapping[str, Any]) -> Result[str]:
    """The model unchanged, as YAML: Databricks reads Ossie models as they are.

    YAML text is returned as it is, comments and layout included.
    """
    model = _ossie.load(ossie_model)
    _ossie.check(model, "the input")
    return Result(ossie_model if isinstance(ossie_model, str) else _ossie.dump(model))


def convert_ossie_model_from_databricks(ossie_model: str | Mapping[str, Any]) -> Result[str]:
    """The model, as YAML, with the portable `sql` slot filled wherever possible.

    An existing `sql` takes precedence and is kept. Without one, an `OSSIE_SQL_2026` dialect entry
    moves into `sql`; otherwise `sql` is translated from the `DATABRICKS` entry, or else from the
    `ANSI_SQL` entry. Those entries stay. An expression with no verified OSSIE_SQL_2026 form is
    left as it is, and reported.
    """
    model = _ossie.load(ossie_model)
    _ossie.check(model, "the input")
    filled = copy.deepcopy(model)
    result = Result("")
    for path, owner, dataset_metric in _expression_owners(filled):
        _fill(owner, path, dataset_metric, result)
    _ossie.check(filled, "the converted model")
    result.value = _ossie.dump(filled)
    return result


def _expression_owners(model: dict[str, Any]) -> Iterator[tuple[str, dict[str, Any], bool]]:
    for dataset in model["datasets"]:
        for field in dataset.get("fields") or []:
            yield f"datasets[{dataset['name']}].fields[{field['name']}]", field, False
        for metric in dataset.get("metrics") or []:
            yield f"datasets[{dataset['name']}].metrics[{metric['name']}]", metric, True
    for metric in model.get("metrics") or []:
        yield f"metrics[{metric['name']}]", metric, False


def _fill(owner: dict[str, Any], path: str, dataset_metric: bool, result: Result) -> None:
    expression = owner["expression"]
    entries = list(expression.get("dialects") or [])
    sql = expression.get("sql")
    portable = next((entry for entry in entries if entry["dialect"] == OSSIE), None)
    if sql:
        # `sql` is authoritative over an OSSIE_SQL_2026 entry; an equal entry is redundant.
        if portable and portable["expression"] == sql:
            owner["expression"] = _expression(sql, [e for e in entries if e is not portable])
        return
    if portable:
        owner["expression"] = _expression(
            portable["expression"], [e for e in entries if e is not portable]
        )
        return

    texts = {entry["dialect"]: entry["expression"] for entry in entries}
    reasons = []
    for dialect in _TRANSLATED_DIALECTS:
        if dialect not in texts:
            continue
        try:
            tree = parse(texts[dialect], dialect)
            if dataset_metric and measure_names(tree):
                raise Untranslatable("an Ossie dataset metric cannot evaluate a measure")
            owner["expression"] = _expression(translate(tree, OSSIE), entries)
            return
        except Untranslatable as error:
            reasons.append(f"{dialect}: {error}")
    if reasons:
        result.warn(WarningCode.PORTABLE_EXPRESSION_MISSING, path, "; ".join(reasons))


def _expression(sql: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    return {"sql": sql, "dialects": entries} if entries else {"sql": sql}
