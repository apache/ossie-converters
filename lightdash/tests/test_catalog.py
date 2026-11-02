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

"""Reading a dbt ``catalog.json``: which node supplies a model's types, and
what the CLI reports when the file can't be read."""

import json

import pytest

from ossie_lightdash import ConversionError
from ossie_lightdash.catalog import load_catalog
from ossie_lightdash.cli import main


def _write(tmp_path, nodes):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"nodes": nodes}), encoding="utf-8")
    return path


def test_models_are_keyed_by_node_name_not_by_unique_id_segment(tmp_path):
    """`model.pkg.orders.v2` is model `orders`, version 2, not model `v2`."""
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "model.shop.orders.v2": {
                    "name": "orders",
                    "package_name": "shop",
                    "version": 2,
                    "columns": {"amount": {"type": "NUMERIC(12,2)"}},
                },
            },
        )
    )
    assert catalog == {"orders": {"amount": "NUMERIC(12,2)"}}


def test_the_latest_version_of_a_model_supplies_its_types(tmp_path):
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "model.shop.orders.v1": {
                    "name": "orders", "package_name": "shop", "version": 1, "latest_version": 2,
                    "columns": {"amount": {"type": "INT64"}},
                },
                "model.shop.orders.v2": {
                    "name": "orders", "package_name": "shop", "version": 2, "latest_version": 2,
                    "columns": {"amount": {"type": "NUMERIC(12,2)"}},
                },
            },
        )
    )
    assert catalog["orders"] == {"amount": "NUMERIC(12,2)"}
    # The same with the versions listed in the other order.
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "model.shop.orders.v2": {
                    "name": "orders", "package_name": "shop", "version": 2,
                    "columns": {"amount": {"type": "NUMERIC(12,2)"}},
                },
                "model.shop.orders.v1": {
                    "name": "orders", "package_name": "shop", "version": 1,
                    "columns": {"amount": {"type": "INT64"}},
                },
            },
        )
    )
    assert catalog["orders"] == {"amount": "NUMERIC(12,2)"}


def test_a_name_defined_by_two_packages_is_an_error_not_an_overwrite(tmp_path):
    path = _write(
        tmp_path,
        {
            "model.shop.orders": {"name": "orders", "package_name": "shop", "columns": {"amount": {"type": "INT64"}}},
            "model.legacy.orders": {"name": "orders", "package_name": "legacy", "columns": {"amount": {"type": "STRING"}}},
        },
    )
    with pytest.raises(ConversionError) as excinfo:
        load_catalog(path)
    assert "orders" in str(excinfo.value)
    assert "shop" in str(excinfo.value) and "legacy" in str(excinfo.value)


def test_a_node_without_a_name_falls_back_to_its_unique_id(tmp_path):
    catalog = load_catalog(
        _write(
            tmp_path,
            {
                "model.shop.customers": {"columns": {"id": {"type": "INT64"}}},
                "model.shop.orders.v3": {"version": 3, "columns": {"id": {"type": "INT64"}}},
                "source.shop.raw.events": {"columns": {"id": {"type": "INT64"}}},
            },
        )
    )
    assert set(catalog) == {"customers", "orders"}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{not json", "not valid JSON"),
        ("[]", "expected a JSON object"),
        ('{"nodes": []}', "`nodes` must be an object"),
        ('{"nodes": {"model.shop.orders": "nope"}}', "must be an object"),
        ('{"nodes": {"model.shop.orders": {"name": "orders", "columns": []}}}', "columns of model.shop.orders"),
    ],
)
def test_a_catalog_that_is_not_a_catalog_raises_conversion_error(tmp_path, text, message):
    path = tmp_path / "catalog.json"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ConversionError) as excinfo:
        load_catalog(path)
    assert message in str(excinfo.value)


def _schema(tmp_path):
    schema = tmp_path / "schema.yml"
    schema.write_text("models:\n  - name: orders\n    columns:\n      - name: amount\n", encoding="utf-8")
    return schema


@pytest.mark.parametrize(
    ("catalog_text", "message"),
    [
        (None, "cannot read --catalog"),
        ("{not json", "not valid JSON"),
        ('{"nodes": {"model.a.orders": {"name": "orders"}, "model.b.orders": {"name": "orders"}}}', "two packages"),
    ],
)
def test_cli_reports_an_unusable_catalog_without_a_traceback(tmp_path, capsys, catalog_text, message):
    catalog = tmp_path / "catalog.json"
    if catalog_text is not None:
        catalog.write_text(catalog_text, encoding="utf-8")
    with pytest.raises(SystemExit) as excinfo:
        main(["import", "-i", str(_schema(tmp_path)), "-o", str(tmp_path / "out.yaml"), "--schema", "s", "--catalog", str(catalog)])
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert message in err
    assert "Traceback" not in err
    assert not (tmp_path / "out.yaml").exists()


def test_cli_matches_a_versioned_model_by_name(tmp_path):
    import yaml

    catalog = _write(
        tmp_path,
        {"model.shop.orders.v2": {"name": "orders", "version": 2, "columns": {"amount": {"type": "NUMERIC(12,2)"}}}},
    )
    out = tmp_path / "out.yaml"
    assert main(["import", "-i", str(_schema(tmp_path)), "-o", str(out), "--schema", "s", "--catalog", str(catalog)]) == 0
    (field,) = yaml.safe_load(out.read_text())["datasets"][0]["fields"]
    assert field["datatype"] == "Decimal"
