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

"""Tests for the LookML to Ossie converter."""

import pytest
import yaml

from ossie_bigquery.lookml_to_ossie import convert_lookml_to_ossie
from ossie_bigquery.ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


def _convert(*views, **kwargs):
    """Converts views given as LookML strings, one file each."""
    files = {f"view_{i}.view.lkml": text for i, text in enumerate(views)}
    return yaml.safe_load(convert_lookml_to_ossie(files, **kwargs))


class TestModel:
    def test_document_header(self):
        model = _convert("view: orders { sql_table_name: analytics.orders ;; }")
        assert model["version"] == SUPPORTED_VERSION
        assert model["name"] == "lookml_model"

    def test_model_name(self):
        model = _convert("view: orders {}", model_name="shop")
        assert model["name"] == "shop"

    def test_empty_input(self):
        with pytest.raises(OssieConversionError, match="non-empty mapping"):
            convert_lookml_to_ossie({})

    def test_invalid_lookml_names_the_file(self):
        with pytest.raises(OssieConversionError, match="'bad.view.lkml'"):
            convert_lookml_to_ossie({"bad.view.lkml": "view: orders {"})

    def test_duplicate_view(self):
        with pytest.raises(OssieConversionError, match="View 'orders' is defined in both"):
            _convert("view: orders {}", "view: orders {}")


class TestViewSource:
    def test_table_name(self):
        model = _convert("view: orders { sql_table_name: analytics.orders ;; }")
        assert model["datasets"] == [{"name": "orders", "source": "analytics.orders"}]

    def test_backticks_are_removed(self):
        model = _convert("view: orders { sql_table_name: `proj.analytics.orders` ;; }")
        assert model["datasets"][0]["source"] == "proj.analytics.orders"

    def test_no_table_uses_view_name(self):
        model = _convert("view: orders {}")
        assert model["datasets"][0]["source"] == "orders"

    def test_derived_table_sql(self):
        model = _convert(
            "view: big_orders { derived_table: { sql: SELECT * FROM orders WHERE total > 100 ;; } }"
        )
        assert model["datasets"][0]["source"] == "SELECT * FROM orders WHERE total > 100"

    def test_native_derived_table_is_skipped(self):
        lookml = "view: summary { derived_table: { explore_source: orders { column: id {} } } }"
        with pytest.warns(UserWarning, match="view 'summary'.*explore_source"):
            model = _convert(lookml)
        assert model["datasets"] == []

    def test_refinement_is_skipped(self):
        with pytest.warns(UserWarning, match="view '\\+orders'.*refinements"):
            model = _convert("view: orders {}", "view: +orders {}")
        assert [d["name"] for d in model["datasets"]] == ["orders"]
