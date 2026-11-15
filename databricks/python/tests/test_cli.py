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

"""Port of OssieDatabricksConverterCliSuite.java.

Tests for the command-line wrapper: which command means which direction, which selector flag
each command accepts, and that stdout carries the same bytes as ``-o``.
"""

import io

import pytest

from ossie_databricks.cli import ExitError, run

OSSIE_MODEL = (
    "version: \"0.2.0.dev0\"\n"
    "name: sales\n"
    "datasets:\n"
    "  - name: orders\n"
    "    source: cat.sch.orders\n"
    "    fields:\n"
    "      - name: o_status\n"
    "        expression:\n"
    "          dialects: [{dialect: DATABRICKS, expression: o_orderstatus}]\n"
)

METRIC_VIEW = (
    "version: '1.1'\n"
    "source: cat.sch.orders\n"
    "dimensions:\n"
    "- {name: o_status, expr: o_orderstatus}\n"
)


def write(directory, name, content):
    path = directory / name
    # Bytes, so Windows newline translation cannot alter the content.
    path.write_bytes(content.encode("utf-8"))
    return path


def stdout(*args):
    """Runs the CLI core and returns whatever it wrote to stdout."""
    out = io.StringIO()
    err = io.StringIO()
    run(list(args), out, err)
    return out.getvalue()


def exit_from(*args):
    with pytest.raises(ExitError) as excinfo:
        stdout(*args)
    return excinfo.value


def test_export_converts_an_ossie_model_to_a_metric_view(tmp_path):
    # The directions are named from the Apache Ossie model's point of view, matching the library
    # Javadoc: `export` takes a model OUT to a Metric View.
    out = stdout("export", str(write(tmp_path, "model.yaml", OSSIE_MODEL)))
    assert "version: \"1.1\"" in out, "expected a Metric View, got:\n" + out
    assert "cat.sch.orders" in out, "expected the source, got:\n" + out


def test_import_converts_a_metric_view_to_an_ossie_model(tmp_path):
    out = stdout("import", str(write(tmp_path, "view.yaml", METRIC_VIEW)))
    assert "datasets:" in out, "expected an Apache Ossie model, got:\n" + out
    assert "0.2.0.dev0" in out, "expected the Apache Ossie version, got:\n" + out


def test_stdout_carries_the_same_bytes_as_the_output_file(tmp_path):
    model = write(tmp_path, "model.yaml", OSSIE_MODEL)
    piped = stdout("export", str(model))
    file = tmp_path / "view.yaml"
    stdout("export", str(model), "-o", str(file))
    # println used to add a second newline, so piping the CLI produced different bytes than -o.
    assert file.read_bytes().decode("utf-8") == piped, "stdout and -o must agree byte-for-byte"


def test_export_rejects_the_import_selector_flag(tmp_path):
    # --name is the import's flag. Both flags used to share one field, so this was silently
    # accepted and used as the model name -- an export naming its own output model.
    e = exit_from("export", str(write(tmp_path, "model.yaml", OSSIE_MODEL)), "--name", "zzz")
    assert e.code == 2
    assert "'--name' is not valid for 'export'" in str(e), (
        "expected the flag to be rejected, got: " + str(e))


def test_import_rejects_the_export_selector_flag(tmp_path):
    e = exit_from("import", str(write(tmp_path, "view.yaml", METRIC_VIEW)), "--source", "zzz")
    assert e.code == 2
    assert "'--source' is not valid for 'import'" in str(e), (
        "expected the flag to be rejected, got: " + str(e))


def test_import_uses_name_for_the_model_name(tmp_path):
    out = stdout("import", str(write(tmp_path, "view.yaml", METRIC_VIEW)), "--name", "custom")
    assert "name: \"custom\"" in out, "expected the named model, got:\n" + out


def test_help_prints_usage():
    # Arguments used to be parsed before the command was looked at, so `--help` reported a
    # missing input file.
    out = stdout("--help")
    assert "Usage:" in out, "expected usage, got:\n" + out
    assert "export <model.yaml>" in out, "expected the export line, got:\n" + out


def test_unknown_command_names_itself(tmp_path):
    e = exit_from("frobnicate", str(write(tmp_path, "model.yaml", OSSIE_MODEL)))
    assert e.code == 2
    assert "Unknown command 'frobnicate'" in str(e), (
        "expected the command to be named, got: " + str(e))


def test_missing_input_file_is_reported():
    e = exit_from("export")
    assert e.code == 2
    assert "Missing input file" in str(e), "expected a missing-input error, got: " + str(e)
