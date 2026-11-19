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

import os
from pathlib import Path
from typing import Any

import yaml

from ossie_databricks import _ossie

FIXTURES = Path(__file__).parent / "fixtures"
_LICENSE = (Path(__file__).parent.parent / "pyproject.toml").read_text().split("\n\n")[0]


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def ossie_root() -> Path:
    return Path(_ossie._validator().__file__).resolve().parents[1]


def assert_golden(name: str, value: Any) -> None:
    """Compare `value` with `fixtures/expected/<name>.yaml`; UPDATE_GOLDEN=1 rewrites it."""
    path = FIXTURES / "expected" / f"{name}.yaml"
    if os.environ.get("UPDATE_GOLDEN"):
        path.parent.mkdir(exist_ok=True)
        path.write_text(f"{_LICENSE}\n\n{_ossie.dump(value)}", encoding="utf-8")
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == value


def warnings_of(result) -> list[str]:
    return [str(warning) for warning in result.warnings]
