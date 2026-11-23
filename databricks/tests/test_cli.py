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

"""The command line as the interop harness of apache/ossie-converters calls it."""

import yaml
from conftest import FIXTURES

from ossie_databricks.cli import main


def test_to_databricks_copies_the_model(tmp_path):
    source = FIXTURES / "retail.ossie.yaml"
    out = tmp_path / "retail.ossie.yaml"
    assert main(["to-databricks", str(source), "-o", str(out)]) == 0
    assert out.read_text() == source.read_text()


def test_from_databricks(tmp_path, capsys):
    out = tmp_path / "model.ossie.yaml"
    argv = ["from-databricks", str(FIXTURES / "databricks_first.ossie.yaml"), "-o", str(out)]
    assert main(argv) == 0
    fields = yaml.safe_load(out.read_text())["datasets"][0]["fields"]
    assert fields[0]["expression"]["sql"] == "o_orderkey"
    stderr = capsys.readouterr().err
    assert "[PORTABLE_EXPRESSION_MISSING] at datasets[orders].metrics[big_orders]: " in stderr


def test_deploy_and_wrap(tmp_path):
    folder = tmp_path / "deployment"
    argv = ["deploy", str(FIXTURES / "retail.ossie.yaml"), "-o", str(folder)]
    assert main([*argv, "--catalog", "main", "--schema", "sales"]) == 0
    assert (folder / "retail.ossie.yaml").is_file()
    assert (folder / "metric_views" / "orders.yaml").is_file()

    out = tmp_path / "wrapped.yaml"
    argv = ["wrap", str(FIXTURES / "joins.metric_view.yaml"), "--name", "main.sales.joins"]
    assert main([*argv, "-o", str(out)]) == 0
    assert yaml.safe_load(out.read_text())["datasets"][0]["source"] == "main.sales.joins"


def test_deploy_needs_overwrite_for_existing_files(tmp_path, capsys):
    argv = ["deploy", str(FIXTURES / "sources.ossie.yaml"), "-o", str(tmp_path)]
    argv += ["--catalog", "main", "--schema", "sales"]
    assert main(argv) == 0
    assert main(argv) == 1
    assert "pass --overwrite" in capsys.readouterr().err
    assert main([*argv, "--overwrite"]) == 0


def test_error_exit_code(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("version: 0.3.0.dev0\nname: m\ndatasets: []\n")
    assert main(["to-databricks", str(bad), "-o", str(tmp_path / "out")]) == 1
    assert capsys.readouterr().err.startswith("error: the input is not a valid Ossie model")


def test_deploy_reports_each_omission_on_stderr(tmp_path, capsys):
    folder = tmp_path / "deployment"
    assert (
        main(
            [
                "deploy",
                str(FIXTURES / "retail.ossie.yaml"),
                "-o",
                str(folder),
                "--catalog",
                "main",
                "--schema",
                "sales",
            ]
        )
        == 0
    )
    stderr = capsys.readouterr().err
    for path in (
        "metrics[revenue_per_customer]",
        "metrics[average_amount]",
        "relationships[orders_to_customers]",
    ):
        assert f"[NOT_DEPLOYED] at {path}:" in stderr


def test_deploy_rejects_invalid_rewritten_metric_before_writing_files(tmp_path, capsys):
    source = tmp_path / "model.yaml"
    model = yaml.safe_load((FIXTURES / "retail.ossie.yaml").read_text())
    model["metrics"] = [{"name": "invalid", "expression": {"sql": "SUM(orders.o_totalprice)"}}]
    source.write_text(yaml.safe_dump(model))
    folder = tmp_path / "deployment"
    folder.mkdir()
    existing = folder / "retail.ossie.yaml"
    existing.write_text("previous successful deployment")
    assert (
        main(
            [
                "deploy",
                str(source),
                "-o",
                str(folder),
                "--overwrite",
                "--catalog",
                "main",
                "--schema",
                "sales",
            ]
        )
        == 1
    )
    assert existing.read_text() == "previous successful deployment"
    assert sorted(p.name for p in folder.iterdir()) == ["retail.ossie.yaml"]
    stderr = capsys.readouterr().err
    assert "invalid" in stderr and "o_totalprice" in stderr
