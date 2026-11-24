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

import os
import subprocess
import sys

import pytest
import yaml

TEXT = "주문 매출 😀"


def _run(module, *arguments):
    # Disable Python's UTF-8 mode and locale coercion. Stdio stays UTF-8 so this
    # exercises model files, independently of the terminal's encoding.
    environment = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    program = """import codecs, locale, runpy, sys
if codecs.lookup(locale.getpreferredencoding(False)).name == "utf-8":
    sys.exit(77)
sys.argv = sys.argv[1:]
runpy.run_module(sys.argv[0], run_name="__main__")
"""
    result = subprocess.run(
        [sys.executable, "-c", program, module, *map(str, arguments)],
        env=environment,
        capture_output=True,
        encoding="utf-8",
        timeout=60,
    )
    if result.returncode == 77:
        pytest.skip("this platform cannot select a non-UTF-8 locale")
    assert result.returncode == 0, result.stderr


def _model():
    return {
        "version": "0.3.0.dev0",
        "name": "shop",
        "description": TEXT,
        "datasets": [
            {
                "name": "orders",
                "source": "db.sch.orders",
                "description": TEXT,
                "primary_key": ["id"],
                "fields": [
                    {
                        "name": "id",
                        "datatype": "Integer",
                        "description": TEXT,
                        "dimension": {"is_time": False},
                        "expression": {
                            "dialects": [{"dialect": "ANSI_SQL", "expression": "id"}]
                        },
                    }
                ],
            }
        ],
    }


def _write(path, document, encoding):
    path.write_text(
        yaml.safe_dump(document, allow_unicode=encoding != "escaped"),
        encoding="utf-8" if encoding == "escaped" else encoding,
    )


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "escaped"])
def test_cli_unicode_directory_round_trip_with_locale_file_io(tmp_path, encoding):
    source, native, returned = (
        tmp_path / "model.yaml",
        tmp_path / "native",
        tmp_path / "back.yaml",
    )
    model = _model()
    _write(source, model, encoding)
    _run("ossie_omni.cli", "export", "-i", source, "-o", native)
    files = sorted(native.rglob("*.y*ml"))
    assert files
    for path in files:
        data = path.read_bytes()
        assert not data.startswith(b"\xef\xbb\xbf")
        document = yaml.safe_load(data.decode("utf-8"))
        _write(path, document, encoding)
    _run("ossie_omni.cli", "import", "-i", native, "-o", returned)
    data = returned.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert yaml.safe_load(data.decode("utf-8"))["datasets"][0]["description"] == TEXT
