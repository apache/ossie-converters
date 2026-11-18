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

import pytest
import yaml
from conftest import assert_golden, fixture, warnings_of

from ossie_databricks import wrap_metric_view_in_ossie_model


@pytest.mark.parametrize("name", ["orders", "joins", "parameters"])
def test_wrap(name):
    result = wrap_metric_view_in_ossie_model(
        f"main.sales.{name}", fixture(f"{name}.metric_view.yaml")
    )
    model = yaml.safe_load(result.value)
    assert_golden(f"wrap_{name}", {"model": model, "warnings": warnings_of(result)})


def test_dataset_name():
    view = fixture("parameters.metric_view.yaml")
    result = wrap_metric_view_in_ossie_model("main.sales.orders", view, dataset_name="sales")
    dataset = yaml.safe_load(result.value)["datasets"][0]
    assert dataset["name"] == "sales"
    assert dataset["source"] == "main.sales.orders"
