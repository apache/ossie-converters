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

"""Tests for the ossie-snowflake CLI.

The entry point moved out of the converter module, so these cover the behaviour
it has always had: read the input file, write the converted output, and report a
conversion failure on stderr with a non-zero exit status.
"""

import pytest
import yaml

from ossie_snowflake import cli

OSSIE_MODEL = {
    "version": "0.3.0.dev0",
    "name": "retail",
    "datasets": [
        {
            "name": "orders",
            "source": "DB.SCH.FCT_ORDER",
            "fields": [
                {
                    "name": "order_id",
                    "expression": {
                        "dialects": [{"dialect": "ANSI_SQL", "expression": "ORDER_ID"}]
                    },
                    "dimension": {"is_time": False},
                }
            ],
        }
    ],
}


@pytest.fixture
def ossie_file(tmp_path):
    path = tmp_path / "ossie.yaml"
    path.write_text(yaml.dump(OSSIE_MODEL))
    return path


class TestConversion:
    def test_ossie_to_snowflake_writes_a_cortex_analyst_document(self, ossie_file, tmp_path):
        out = tmp_path / "out.yaml"
        cli.main(["-i", str(ossie_file), "-o", str(out)])
        document = yaml.safe_load(out.read_text())
        assert document["name"] == "retail"
        assert [t["name"] for t in document["tables"]] == ["orders"]

    def test_it_reports_what_it_wrote(self, ossie_file, tmp_path, capsys):
        out = tmp_path / "out.yaml"
        cli.main(["-i", str(ossie_file), "-o", str(out)])
        assert f"Converted {ossie_file} -> {out}" in capsys.readouterr().out

    def test_long_option_names_work(self, ossie_file, tmp_path):
        out = tmp_path / "out.yaml"
        cli.main(["--input", str(ossie_file), "--output", str(out)])
        assert out.exists()

    def test_snowflake_to_ossie_subcommand_writes_a_flat_ossie_document(
        self, tmp_path, capsys
    ):
        source = tmp_path / "snowflake.yaml"
        source.write_text(
            "name: retail\ntables:\n  - name: orders\n"
            "    base_table:\n      database: DB\n"
            "      schema: SCHEMA\n      table: ORDERS\n",
            encoding="utf-8",
        )
        out = tmp_path / "ossie.yaml"
        cli.main(
            ["snowflake-to-ossie", "-i", str(source), "-o", str(out)]
        )
        document = yaml.safe_load(out.read_text(encoding="utf-8"))
        assert document["version"] == "0.3.0.dev0"
        assert document["name"] == "retail"
        assert document["datasets"][0]["source"] == "DB.SCHEMA.ORDERS"
        assert f"Converted {source} -> {out}" in capsys.readouterr().out


class TestErrors:
    def test_a_conversion_error_exits_non_zero_without_a_traceback(self, tmp_path, capsys):
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.dump({"version": "0.3.0.dev0"}))
        with pytest.raises(SystemExit) as excinfo:
            cli.main(["-i", str(bad), "-o", str(tmp_path / "out.yaml")])
        assert excinfo.value.code == 1
        assert "Error: " in capsys.readouterr().err

    def test_a_failed_conversion_writes_no_output_file(self, tmp_path):
        bad = tmp_path / "bad.yaml"
        bad.write_text(yaml.dump({"version": "0.3.0.dev0"}))
        out = tmp_path / "out.yaml"
        with pytest.raises(SystemExit):
            cli.main(["-i", str(bad), "-o", str(out)])
        assert not out.exists()

    def test_malformed_snowflake_yaml_exits_cleanly(self, tmp_path, capsys):
        bad = tmp_path / "bad-snowflake.yaml"
        bad.write_text("name: [unterminated", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            cli.main(
                ["snowflake-to-ossie", "-i", str(bad), "-o", str(tmp_path / "out.yaml")]
            )
        assert excinfo.value.code == 1
        assert "Error: Invalid Snowflake YAML" in capsys.readouterr().err
