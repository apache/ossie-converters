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

import yaml
import json
from pathlib import Path
from ossie_denodo.vql_to_osi import convert_vql_to_ossie

FIXTURES = Path(__file__).parent / "fixtures_vql"

def _load_vql(vql_name: str) -> str:
    path = FIXTURES / vql_name
    with open(path, "r", encoding="utf-8") as f:
        return f.read()

class TestExample1:
    FIXTURE = "example1.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_version_is_correct(self):
        osi_dict = self._convert(self.FIXTURE)
        assert osi_dict.get("version") == "0.2.0.dev0"

    def test_no_semantic_model_at_root(self):
        osi_dict = self._convert(self.FIXTURE)
        assert "semantic_model" not in osi_dict

    def test_denodo_custom_extension(self):
        osi_dict = self._convert(self.FIXTURE)
        custom_extensions = osi_dict.get("custom_extensions", [])
        denodo_ext = next((ext for ext in custom_extensions if ext.get("vendor_name") == "DENODO"), None)
        assert denodo_ext is not None

        import json
        data = json.loads(denodo_ext["data"])
        assert data["folder"] == "/3. views"
        assert data["context"] == {"I18N": "us_pst"}
        assert sorted(data["tags"]) == sorted(["sales_data", "sensitive"])

    def test_dataset_customer_fields(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None

        field = next((f for f in customer.get("fields", []) if f.get("name") == "customer_address_full"), None)
        assert field is not None

        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == "concat(customer.c_address, '-', nation.n_name)"

    def test_dataset_nation_fields(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        nation = next((ds for ds in datasets if ds.get("name") == "nation"), None)
        assert nation is not None

        field = next((f for f in nation.get("fields", []) if f.get("name") == "nation_name"), None)
        assert field is not None

        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == "nation.n_name"

    def test_dataset_orders_fields(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        orders = next((ds for ds in datasets if ds.get("name") == "orders"), None)
        assert orders is not None

        field = next((f for f in orders.get("fields", []) if f.get("name") == "order_dim_extra"), None)
        assert field is not None

        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == "(orders.o_orderkey+5)"

    def test_relationships(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel1 = next((r for r in relationships if r.get("name") == "psql_tpch_nation_psql_tpch_customer"), None)
        assert rel1 is not None
        assert rel1["from"] == "customer"
        assert rel1["to"] == "nation"
        assert rel1["from_columns"] == ["c_nationkey"]
        assert rel1["to_columns"] == ["n_nationkey"]

        rel2 = next((r for r in relationships if r.get("name") == "psql_tpch_customer_psql_tpch_orders"), None)
        assert rel2 is not None
        assert rel2["from"] == "orders"
        assert rel2["to"] == "customer"
        assert rel2["from_columns"] == ["o_custkey"]
        assert rel2["to_columns"] == ["c_custkey"]

    def test_metrics(self):
        osi_dict = self._convert(self.FIXTURE)
        metrics = osi_dict.get("metrics", [])

        m1 = next((m for m in metrics if m.get("name") == "count_order_keys"), None)
        assert m1 is not None
        dialect1 = next((d for d in m1.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect1 is not None
        assert dialect1["expression"] == "count(orders.o_orderkey)"

        m2 = next((m for m in metrics if m.get("name") == "order_average_value"), None)
        assert m2 is not None
        dialect2 = next((d for d in m2.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect2 is not None
        assert dialect2["expression"] == "avg(orders.o_totalprice)"

class TestExample2:
    FIXTURE = "example2.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_version_is_correct(self):
        osi_dict = self._convert(self.FIXTURE)
        assert osi_dict.get("version") == "0.2.0.dev0"

    def test_dataset_sources_with_database_prefix(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])

        nation = next((ds for ds in datasets if ds.get("name") == "nation"), None)
        assert nation is not None
        assert nation["source"] == "demo_metricview_metadata_db.psql_tpch_nation"

        orders = next((ds for ds in datasets if ds.get("name") == "orders"), None)
        assert orders is not None
        assert orders["source"] == "demo_metricview_metadata_db.psql_tpch_orders"

        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None
        assert customer["source"] == "demo_metricview_metadata_db.psql_tpch_customer"

    def test_relationships_with_database_prefix(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel1 = next((r for r in relationships if r.get("name") == "demo_metricview_metadata_db.psql_tpch_nation_psql_tpch_customer"), None)
        assert rel1 is not None
        assert rel1["from"] == "customer"
        assert rel1["to"] == "nation"

        rel2 = next((r for r in relationships if r.get("name") == "demo_metricview_metadata_db.psql_tpch_customer_psql_tpch_orders"), None)
        assert rel2 is not None
        assert rel2["from"] == "orders"
        assert rel2["to"] == "customer"

class TestExample3:
    FIXTURE = "example3.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_metric_test_metric_expression(self):
        osi_dict = self._convert(self.FIXTURE)
        metrics = osi_dict.get("metrics", [])
        m = next((m for m in metrics if m.get("name") == "test_metric"), None)
        assert m is not None

        dialect_expr = next((d for d in m.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        expected_expr = "nullif(sum(case WHEN (orders.o_totalprice > 10000) THEN 1 ELSE 0 END), 0)"
        assert dialect_expr["expression"] == expected_expr

class TestExample4:
    FIXTURE = "example4.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_datasets_quoted_names(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])

        names = [ds.get("name") for ds in datasets]
        assert '"#nation"' in names
        assert '"#orders"' in names

    def test_dataset_nation_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        nation = next((ds for ds in datasets if ds.get("name") == '"#nation"'), None)
        assert nation is not None

        field = next((f for f in nation.get("fields", []) if f.get("name") == "nation_name"), None)
        assert field is not None
        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == '"#nation".n_name'

    def test_dataset_orders_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        orders = next((ds for ds in datasets if ds.get("name") == '"#orders"'), None)
        assert orders is not None

        field = next((f for f in orders.get("fields", []) if f.get("name") == "order_date"), None)
        assert field is not None
        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == '"#orders".o_orderdate'

    def test_dataset_customer_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None

        # Test concat with quoted nation
        field_full = next((f for f in customer.get("fields", []) if f.get("name") == "customer_address_full"), None)
        assert field_full is not None
        dialect_expr_full = next((d for d in field_full.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr_full is not None
        assert dialect_expr_full["expression"] == "concat(customer.c_address, '-', \"#nation\".n_name)"

        # Test quoted field name
        field_quoted = next((f for f in customer.get("fields", []) if f.get("name") == '"#customer_address"'), None)
        assert field_quoted is not None
        dialect_expr_quoted = next((d for d in field_quoted.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr_quoted is not None
        assert dialect_expr_quoted["expression"] == "customer.c_address"

    def test_relationships_quoted_names(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel1 = next((r for r in relationships if r.get("name") == "psql_tpch_nation_psql_tpch_customer"), None)
        assert rel1 is not None
        assert rel1["to"] == '"#nation"'

        rel2 = next((r for r in relationships if r.get("name") == "psql_tpch_customer_psql_tpch_orders"), None)
        assert rel2 is not None
        assert rel2["from"] == '"#orders"'

    def test_metrics_quoted_names_and_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        metrics = osi_dict.get("metrics", [])

        metric = next((m for m in metrics if m.get("name") == '"#count_order_keys"'), None)
        assert metric is not None

        dialect_expr = next((d for d in metric.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == 'count("#orders".o_orderkey)'

class TestExample4b:
    FIXTURE = "example4b.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_datasets_quoted_names(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])

        names = [ds.get("name") for ds in datasets]
        assert '"nat****ion"' in names
        assert '"#orders"' in names

    def test_dataset_nation_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        nation = next((ds for ds in datasets if ds.get("name") == '"nat****ion"'), None)
        assert nation is not None

        field = next((f for f in nation.get("fields", []) if f.get("name") == "nation_name"), None)
        assert field is not None
        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == '"nat****ion".n_name'

    def test_dataset_orders_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        orders = next((ds for ds in datasets if ds.get("name") == '"#orders"'), None)
        assert orders is not None

        field = next((f for f in orders.get("fields", []) if f.get("name") == "order_date"), None)
        assert field is not None
        dialect_expr = next((d for d in field.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr is not None
        assert dialect_expr["expression"] == '"#orders".o_orderdate'

    def test_dataset_customer_quoted_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None

        # Test concat with quoted nation
        field_full = next((f for f in customer.get("fields", []) if f.get("name") == "customer_address_full"), None)
        assert field_full is not None
        dialect_expr_full = next((d for d in field_full.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr_full is not None
        assert dialect_expr_full["expression"] == "concat(customer.c_address, '-', \"nat****ion\".n_name)"

        # Test quoted field names with special characters
        field_hash = next((f for f in customer.get("fields", []) if f.get("name") == '"#customer_address"'), None)
        assert field_hash is not None

        field_special = next((f for f in customer.get("fields", []) if f.get("name") == '"dim:special@field"'), None)
        assert field_special is not None

    def test_relationships_quoted_names(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel1 = next((r for r in relationships if r.get("name") == "psql_tpch_nation_psql_tpch_customer"), None)
        assert rel1 is not None
        assert rel1["to"] == '"nat****ion"'

        rel2 = next((r for r in relationships if r.get("name") == "psql_tpch_customer_psql_tpch_orders"), None)
        assert rel2 is not None
        assert rel2["from"] == '"#orders"'

    def test_metrics_quoted_names_and_expressions(self):
        osi_dict = self._convert(self.FIXTURE)
        metrics = osi_dict.get("metrics", [])

        metric1 = next((m for m in metrics if m.get("name") == '"#count_order_keys"'), None)
        assert metric1 is not None
        dialect_expr1 = next((d for d in metric1.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr1["expression"] == 'count("#orders".o_orderkey)'

        metric2 = next((m for m in metrics if m.get("name") == '"metric/average+value"'), None)
        assert metric2 is not None
        dialect_expr2 = next((d for d in metric2.get("expression", {}).get("dialects", []) if d.get("dialect") == "ANSI_SQL"), None)
        assert dialect_expr2["expression"] == 'avg("#orders".o_totalprice)'

class TestExample5:
    FIXTURE = "example5.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_version(self):
        osi_dict = self._convert(self.FIXTURE)
        assert osi_dict["version"] == "0.2.0.dev0"

    def test_custom_extensions_filter(self):
        osi_dict = self._convert(self.FIXTURE)
        custom_exts = osi_dict.get("custom_extensions", [])
        denodo_ext = next((ext for ext in custom_exts if ext.get("vendor_name") == "DENODO"), None)
        assert denodo_ext is not None

        data = json.loads(denodo_ext["data"])
        assert data.get("filter_condition") == 'nation.n_name <> "NON_EXISTENT_NATION"'
        assert data.get("folder") == "/3. views"
        assert data.get("tags") == ["sales_data", "sensitive"]
        assert data.get("context") == {"I18N": "us_pst"}

class TestExample6:
    FIXTURE = "example6.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_dataset_orders_dimension_extension(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        orders = next((ds for ds in datasets if ds.get("name") == "orders"), None)
        assert orders is not None

        custom_exts = orders.get("custom_extensions", [])
        denodo_ext = next((ext for ext in custom_exts if ext.get("vendor_name") == "DENODO"), None)
        assert denodo_ext is not None

        data = json.loads(denodo_ext["data"])
        fields_dim = data.get("fields_dimension", {})
        assert fields_dim.get("order_date") == "orders_dim"
        assert fields_dim.get("order_dim_extra") == "orders_dim"

    def test_dataset_customer_dimension_extension(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None

        custom_exts = customer.get("custom_extensions", [])
        denodo_ext = next((ext for ext in custom_exts if ext.get("vendor_name") == "DENODO"), None)
        assert denodo_ext is not None

        data = json.loads(denodo_ext["data"])
        fields_dim = data.get("fields_dimension", {})
        assert fields_dim.get("customer_address_full") == "customer_dim"
        assert fields_dim.get("customer_name") == "customer_dim"
        assert fields_dim.get("customer_address") == "customer_dim"
        assert fields_dim.get("customer_comment") == "customer_dim"
        assert fields_dim.get("generic_attribute") == "customer_dim"

    def test_dataset_nation_no_dimension_extension(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])
        nation = next((ds for ds in datasets if ds.get("name") == "nation"), None)
        assert nation is not None

        custom_exts = nation.get("custom_extensions", [])
        denodo_ext = next((ext for ext in custom_exts if ext.get("vendor_name") == "DENODO"), None)
        if denodo_ext:
            data = json.loads(denodo_ext["data"])
            assert "fields_dimension" not in data

class TestExample6b:
    FIXTURE = "example6b.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_cross_dataset_dimension_customer_data(self):
        osi_dict = self._convert(self.FIXTURE)
        datasets = osi_dict.get("datasets", [])

        # customer dataset: customer_name y customer_address_full
        customer = next((ds for ds in datasets if ds.get("name") == "customer"), None)
        assert customer is not None
        cust_ext = next((ext for ext in customer.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert cust_ext is not None
        cust_data = json.loads(cust_ext["data"])
        assert cust_data["fields_dimension"]["customer_name"] == "customer_data"
        assert cust_data["fields_dimension"]["customer_address_full"] == "customer_data"

        # orders dataset: order_customer_key
        orders = next((ds for ds in datasets if ds.get("name") == "orders"), None)
        assert orders is not None
        ord_ext = next((ext for ext in orders.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ord_ext is not None
        ord_data = json.loads(ord_ext["data"])
        assert ord_data["fields_dimension"]["order_customer_key"] == "customer_data"

class TestExample7:
    FIXTURE = "example7.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_no_global_custom_extensions(self):
        osi_dict = self._convert(self.FIXTURE)
        assert "custom_extensions" not in osi_dict

    def test_relationships_custom_extensions(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        # dw_dim_customers_dw_fact_sales
        rel1 = next((r for r in relationships if r.get("name") == "dw_dim_customers_dw_fact_sales"), None)
        assert rel1 is not None
        ext1 = next((ext for ext in rel1.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext1 is not None
        data1 = json.loads(ext1["data"])
        assert data1["from_columns"] == {"relation": "1", "fields_replace": [{"customer_key": "round(customer_key)"}]}

        # dw_dim_products_dw_fact_sales
        rel2 = next((r for r in relationships if r.get("name") == "dw_dim_products_dw_fact_sales"), None)
        assert rel2 is not None
        ext2 = next((ext for ext in rel2.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext2 is not None
        data2 = json.loads(ext2["data"])
        assert data2["from_columns"] == {"relation": "1"}

        # dw_dim_date_dw_fact_sales
        rel3 = next((r for r in relationships if r.get("name") == "dw_dim_date_dw_fact_sales"), None)
        assert rel3 is not None
        ext3 = next((ext for ext in rel3.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext3 is not None
        data3 = json.loads(ext3["data"])
        assert data3["from_columns"] == {"relation": "1"}

class TestExample9:
    FIXTURE = "example9.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_relationships_custom_extensions(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        # psql_tpch_region_nation
        rel = next((r for r in relationships if r.get("name") == "psql_tpch_region_nation"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        assert json.loads(ext["data"]) == {"from_columns": {"relation": "+"}}

        # psql_tpch_orders_lineitem
        rel = next((r for r in relationships if r.get("name") == "psql_tpch_orders_lineitem"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        assert json.loads(ext["data"]) == {"from_columns": {"relation": "+"}}

        # psql_tpch_nation_supplier
        rel = next((r for r in relationships if r.get("name") == "psql_tpch_nation_supplier"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        assert json.loads(ext["data"]) == {"from_columns": {"relation": "+"}}

        # psql_tpch_customer_orders
        rel = next((r for r in relationships if r.get("name") == "psql_tpch_customer_orders"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        assert json.loads(ext["data"]) == {"from_columns": {"relation": "0,1"}}

        # psql_tpch_nation_customer
        rel = next((r for r in relationships if r.get("name") == "psql_tpch_nation_customer"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        assert json.loads(ext["data"]) == {"from_columns": {"relation": "1"}}

class TestExample10:
    FIXTURE = "example10.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_relationship_psql_tpch_nation_customer_extension(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel = next((r for r in relationships if r.get("name") == "psql_tpch_nation_customer"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        data = json.loads(ext["data"])
        assert data["isReferentialConstraint"] is False
        assert data["from_columns"]["relation"] == "1"

class TestExample13:
    FIXTURE = "example13.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_relationships_join_types(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        # vdb_tpch.psql_tpch_nation_psql_tpch_customer -> RIGHT
        rel1 = next((r for r in relationships if r.get("name") == "vdb_tpch.psql_tpch_nation_psql_tpch_customer"), None)
        assert rel1 is not None
        ext1 = next((ext for ext in rel1.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext1 is not None
        data1 = json.loads(ext1["data"])
        assert data1.get("join_type") == "RIGHT"

        # vdb_tpch.psql_tpch_customer_psql_tpch_orders -> LEFT
        rel2 = next((r for r in relationships if r.get("name") == "vdb_tpch.psql_tpch_customer_psql_tpch_orders"), None)
        assert rel2 is not None
        ext2 = next((ext for ext in rel2.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext2 is not None
        data2 = json.loads(ext2["data"])
        assert data2.get("join_type") == "LEFT"

        # vdb_tpch.psql_tpch_region_psql_tpch_nation -> NO custom_extension (or no join_type)
        rel3 = next((r for r in relationships if r.get("name") == "vdb_tpch.psql_tpch_region_psql_tpch_nation"), None)
        assert rel3 is not None
        ext3 = next((ext for ext in rel3.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        if ext3:
            data3 = json.loads(ext3["data"])
            assert "join_type" not in data3

    def test_relationship_names_with_database_prefix(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])
        names = [r.get("name") for r in relationships]

        assert "vdb_tpch.psql_tpch_nation_psql_tpch_customer" in names
        assert "vdb_tpch.psql_tpch_region_psql_tpch_nation" in names
        assert "vdb_tpch.psql_tpch_customer_psql_tpch_orders" in names

class TestExample14:
    FIXTURE = "example14.vql"

    def _convert(self, vql_name: str) -> dict:
        vql = _load_vql(vql_name)
        yaml_str = convert_vql_to_ossie(vql)
        return yaml.safe_load(yaml_str)

    def test_relationship_custom_extension_folder(self):
        osi_dict = self._convert(self.FIXTURE)
        relationships = osi_dict.get("relationships", [])

        rel = next((r for r in relationships if r.get("name") == "dw_dim_products_dw_fact_sales"), None)
        assert rel is not None
        ext = next((ext for ext in rel.get("custom_extensions", []) if ext.get("vendor_name") == "DENODO"), None)
        assert ext is not None
        data = json.loads(ext["data"])
        assert data.get("folder") == "/99. assoc"
        assert data["to_columns"]["relation"] == "0,1"
