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
import yaml

from test_support.cli_encoding import TEXT, _model, _run, _write


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "escaped"])
def test_cli_unicode_directory_round_trip_with_locale_file_io(tmp_path, encoding):
    source, native, returned = (
        tmp_path / "model.yaml",
        tmp_path / "native",
        tmp_path / "back.yaml",
    )
    model = _model()
    model["datasets"][0]["fields"].append(
        {
            "name": "next_id",
            "description": TEXT,
            "datatype": "Integer",
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "id + 1"}]
            },
        }
    )
    model["metrics"] = [
        {
            "name": "total_id",
            "description": TEXT,
            "expression": {
                "dialects": [{"dialect": "ANSI_SQL", "expression": "SUM(orders.id)"}]
            },
        }
    ]
    _write(source, model, encoding)
    _run("ossie_honeydew.converter", "ossie-to-honeydew", "-i", source, "-o", native)
    files = sorted(native.rglob("*.y*ml"))
    assert files
    for category in ("datasets", "attributes", "metrics"):
        assert any(category in path.parts for path in files)
    for path in files:
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        document = yaml.safe_load(data.decode("utf-8"))
        _write(path, document, encoding)
    _run("ossie_honeydew.converter", "honeydew-to-ossie", "-i", native, "-o", returned)
    data = returned.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert yaml.safe_load(data.decode("utf-8"))["datasets"][0]["description"] == TEXT
