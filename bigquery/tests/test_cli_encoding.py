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

"""Real CLI file I/O under a non-UTF-8 locale, including Unicode round trips."""

import pytest

from test_support.cli_encoding import TEXT, _model, _run, _write


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "escaped"])
def test_cli_preserves_unicode_with_locale_file_io(tmp_path, encoding):
    source, output = tmp_path / "model.yaml", tmp_path / "output.yaml"
    _write(source, _model(), encoding)
    _run("ossie_bigquery.cli", "-i", source, "-o", output)
    data = output.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert TEXT in data.decode("utf-8")


def test_cli_failure_includes_command_and_both_streams(tmp_path, monkeypatch):
    module = tmp_path / "encoding_failure.py"
    module.write_text(
        'import sys\nprint("stdout detail")\n'
        'print("stderr detail", file=sys.stderr)\nsys.exit(3)\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(AssertionError) as error:
        _run("encoding_failure", "--example", "value with spaces")
    message = str(error.value)
    for expected in (
        "exit code 3",
        "encoding_failure",
        "--example",
        "value with spaces",
        "stdout:\nstdout detail",
        "stderr:\nstderr detail",
    ):
        assert expected in message
