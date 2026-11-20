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

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from interop.live_databricks import (
    DEFAULT_SOURCE,
    GRAINS,
    MEASURES,
    compare,
    comparison_sql,
    cube_requests,
    metric_view_definition,
)
from interop.manifest import Manifest
from interop.report import write_report
from interop.runner import Runner, exit_code


def test_cube_and_metric_view_query_the_same_members_at_each_grain():
    requests = cube_requests()
    assert set(requests) == {"total", "brand", "store"}
    assert requests["total"]["dimensions"] == []
    assert requests["brand"]["dimensions"] == ["tpcds_retail_model.i_brand"]
    assert requests["store"]["dimensions"] == ["tpcds_retail_model.s_store_name"]
    for grain, field in GRAINS.items():
        query = comparison_sql("SELECT * FROM fixture", "scratch.tmp.parity", field)
        assert requests[grain]["measures"] == [
            f"tpcds_retail_model.{m}" for m in MEASURES
        ]
        assert all(f"MEASURE(`{name}`)" in query for name in MEASURES)
        assert "FULL OUTER JOIN" in query
        if field:
            assert f"GROUP BY `{field}`" in query
            assert f"c.`tpcds_retail_model__{field}` <=> m.`{field}`" in query
        else:
            assert "GROUP BY" not in query


def test_sample_adapts_the_missing_month_only_for_the_builtin_sample():
    export = {
        "source": "tpcds.public.store_sales",
        "joins": [{"name": "date_dim", "source": "tpcds.public.date_dim"}],
        "measures": [{"name": name} for name in MEASURES],
        "dimensions": [{"name": "i_brand"}, {"name": "s_store_name"}],
    }
    sample = yaml.safe_load(
        metric_view_definition(yaml.safe_dump(export), DEFAULT_SOURCE)
    )
    assert sample["source"] == "samples.tpcds_sf1.store_sales"
    assert (
        "AS d_month_name FROM samples.tpcds_sf1.date_dim"
        in sample["joins"][0]["source"]
    )
    custom = yaml.safe_load(
        metric_view_definition(yaml.safe_dump(export), "other.fixture")
    )
    assert custom["joins"][0]["source"] == "other.fixture.date_dim"


@pytest.mark.parametrize("outcome", ["match", "mismatch", "missing", "empty", "error"])
def test_live_comparison_reports_differences_and_drops_the_scratch_view(outcome):
    queries = dict.fromkeys(GRAINS, "SELECT * FROM cube_fixture")
    calls = []

    def execute_statement(*, warehouse_id, statement, wait_timeout):
        assert warehouse_id == "test-warehouse"
        calls.append(statement)
        data = None
        if statement.startswith("WITH c"):
            if outcome == "error":
                return SimpleNamespace(
                    status=SimpleNamespace(
                        state=SimpleNamespace(value="FAILED"),
                        error=SimpleNamespace(message="rejected expression"),
                    )
                )
            data = [["1", "1", "0", "0", "0", "0", "0", "0"]]
            if outcome == "mismatch":
                data[0][-1] = "1"
            if outcome == "missing":
                data[0][2] = "1"
            if outcome == "empty":
                data[0][:2] = ["0", "0"]
        return SimpleNamespace(
            status=SimpleNamespace(state=SimpleNamespace(value="SUCCEEDED")),
            result=SimpleNamespace(data_array=data),
        )

    result = {"grade": "OK", "reason": "", "grains": {}}
    compare(
        SimpleNamespace(execute_statement=execute_statement),
        "test-warehouse",
        "catalog.scratch.parity",
        "version: 1.1",
        queries,
        30,
        result,
    )
    assert calls[0].startswith("CREATE VIEW catalog.scratch.parity WITH METRICS")
    assert calls[-1] == "DROP VIEW IF EXISTS catalog.scratch.parity"
    assert result["grade"] == ("OK" if outcome == "match" else "FAIL")
    if outcome == "error":
        assert "compare total grain: rejected expression" == result["reason"]
    else:
        assert list(result["grains"]) == list(GRAINS)
        assert result["grains"]["brand"]["measure_mismatches"][
            "store_productivity"
        ] == (1 if outcome == "mismatch" else 0)


def test_matrix_requires_live_check_only_when_opted_in(tmp_path, monkeypatch):
    monkeypatch.delenv("OSSIE_DATABRICKS_WAREHOUSE_ID", raising=False)
    root = Path(__file__).resolve().parents[2]
    manifest = Manifest({}, {})
    default = Runner(root, manifest, tmp_path).run()
    assert "live_databricks" not in default
    assert exit_code(default) == 0
    opted_in = Runner(root, manifest, tmp_path, live_databricks=True).run()
    assert "OSSIE_DATABRICKS_WAREHOUSE_ID" in opted_in["live_databricks"]["reason"]
    assert exit_code(opted_in) == 1
    write_report(opted_in, tmp_path)
    assert (
        "Live Databricks TPC-DS measure parity" in (tmp_path / "report.md").read_text()
    )
