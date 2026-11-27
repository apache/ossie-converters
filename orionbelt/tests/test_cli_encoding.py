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
@pytest.mark.parametrize("legacy", [False, True])
def test_both_clis_preserve_unicode_on_round_trip(tmp_path, encoding, legacy):
    source, native, returned = (
        tmp_path / name for name in ("model.yaml", "native.yaml", "back.yaml")
    )
    _write(source, _model(), encoding)
    module = "ossie_orionbelt.converter" if legacy else "ossie_orionbelt.cli"
    forward = ["ossie2obml", source] if legacy else ["ossie-to-obml", "-i", source]
    backward = ["obml2ossie", native] if legacy else ["obml-to-ossie", "-i", native]
    _run(module, *forward, "-o", native)
    document = yaml.safe_load(native.read_text(encoding="utf-8"))
    assert TEXT in yaml.safe_dump(document, allow_unicode=True)
    _write(native, document, encoding)
    _run(module, *backward, "-o", returned)
    data = returned.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert yaml.safe_load(data.decode("utf-8"))["datasets"][0]["description"] == TEXT
