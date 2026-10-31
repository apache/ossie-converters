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

"""Golden cases: registry input -> expected Ossie -> expected registry, checked by tests.

    uv run python tests/_golden.py            # check: exit 1 if any case differs
    uv run python tests/_golden.py --write    # regenerate tests/fixtures/<case>/

Each case directory holds
    input.registry.json           the registry record, as the registry API returns it
    expected.ossie.yaml           the Ossie document the converter must produce (strict 0.2.0 form:
                                  extension data as JSON strings, so every converter can read it)
    expected.registry.json        each Ossie metric converted back to a registry definition
    expected.writeback.json       write_back() of the unedited Ossie file against the input: must be {}
    edit.json                     (some cases) an edit made in the Ossie file, and
    expected.writeback_edit.json  the registry patch write_back() must produce for it
or, for a metric Ossie cannot express,
    expected.refusal.json         the reason the converter gives

A converter change that alters any of these files is a visible diff in review.
"""

from __future__ import annotations

import json
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from _synthetic import records  # noqa: E402
from ossie_atlan import converter as A  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"

HEADER = "\n".join(line for line in pathlib.Path(__file__).read_text().splitlines()[:16]) + "\n"

# case -> why it exists
CASES = {
    "count_rows": "COUNT(*) on one table: the simplest metric",
    "count_filtered": "a WHERE clause becomes CASE WHEN inside the aggregate",
    "count_distinct_filtered": "COUNT(DISTINCT CASE ...)",
    "max_timestamp": "a non-additive aggregate (MAX) with a time field",
    "refused_join_from_sql_sum": "a LEFT JOIN read from the SQL with no declared cardinality: refused",
    "refused_join_from_sql_split": "four outputs over two joins read from the SQL: refused until the joins are declared",
    "ratio_joins_dims_window": "ratio, two joins, dimensions, filters, parameters and a date window",
    "ratio_round_window": "ROUND ratio and a count from one query; its fixed date range is named in the instructions only",
    "refused_list_aggregate": "ARRAY_AGG returns a list: refused",
    "refused_bind_placeholder": "a ? parameter with no name or default: refused",
    "bare_columns_ratio_dims": "a ratio over bare column names with six dimensions; the display-style name is slugged",
    "mandatory_filter_time_dim": "COUNT DISTINCT with a mandatory filter and a time dimension",
    "declared_join_clause": "a declared join whose sql is a full `JOIN table alias ON ...`, plus a time column",
    "fragment_join_qualified": "source.sql is a FROM fragment with a LEFT JOIN, columns are fully "
                                      "qualified by table name, and there are example questions",
    "bound_placeholder_param": "a {{window_days}} placeholder bound to its declared default; a ROUND ratio",
    "refused_non_sql_dialect": "dialect promql: no SQL, no Ossie expression",
    "account_param_as_dimension": "a per-account parameter with no default becomes a dimension on that column",
    "refused_window_cte": "a trailing average over a CTE: Ossie has no window field, so refused",
    "refused_row_listing": "a row lookup, not a measure: refused",
    "bigquery_backticked_names": "BigQuery names: a path in one pair of backticks, a join target quoted per part",
}

# An edit made in the Ossie file, and the registry patch write_back must produce for it.
EDITS = {"count_filtered": {"metric": "preferred_customer_count",
                            "description": "Customers whose preferred flag is set.",
                            "agent_context.grain": "one row per preferred customer"}}


def _apply_edit(doc: dict, edit: dict) -> dict:
    doc = json.loads(json.dumps(doc))
    m = next(x for x in doc["metrics"] if x["name"] == edit["metric"])
    for k, v in edit.items():
        if k == "description":
            m["description"] = v
        elif k.startswith("agent_context."):
            ext = next(x for x in m["custom_extensions"] if x["vendor_name"] == "ATLAN")
            ext["data"]["agent_context"][k.split(".", 1)[1]] = v
    return doc


def _dump_yaml(obj) -> str:
    return HEADER + "\n" + yaml.safe_dump(obj, sort_keys=False, width=100, allow_unicode=True)


def _dump_json(obj) -> str:
    return json.dumps(obj, indent=1, ensure_ascii=False) + "\n"


def build(name: str) -> dict[str, str]:
    """The files a case directory should contain, as text."""
    why = CASES[name]
    rec = records()[name]
    files = {"input.registry.json": _dump_json(rec)}
    try:
        parts = A.convert(rec)
    except A.NotConvertible as e:
        files["expected.refusal.json"] = _dump_json({"reason": str(e)})
        return files
    doc = A.merge(parts, name=name, description=why, workspace_id=rec.get("workspace_id"))
    files["expected.ossie.yaml"] = _dump_yaml(A.export(doc))
    files["expected.registry.json"] = _dump_json({m["name"]: A.to_registry(doc, m["name"]) for m in doc["metrics"]})
    files["expected.writeback.json"] = _dump_json({m["name"]: A.write_back(doc, m["name"], rec) for m in doc["metrics"]})
    if name in EDITS:
        e = EDITS[name]
        files["edit.json"] = _dump_json(e)
        files["expected.writeback_edit.json"] = _dump_json(A.write_back(_apply_edit(doc, e), e["metric"], rec))
    return files


def check(write: bool = False) -> list[str]:
    diffs = []
    for name in CASES:
        d = FIXTURES / name
        want = build(name)
        if write:
            d.mkdir(parents=True, exist_ok=True)
            for old in d.glob("*"):
                if old.name not in want:
                    old.unlink()
            for fn, text in want.items():
                (d / fn).write_text(text)
            continue
        have = {p.name: p.read_text() for p in d.glob("*")} if d.exists() else {}
        for fn in sorted(set(want) | set(have)):
            if want.get(fn) != have.get(fn):
                diffs.append(f"{name}/{fn}")
    return diffs


if __name__ == "__main__":
    out = check(write="--write" in sys.argv)
    if out:
        print("differs from golden:", *out, sep="\n  ")
        sys.exit(1)
    print("golden cases:", "written" if "--write" in sys.argv else "match", f"({len(CASES)})")
