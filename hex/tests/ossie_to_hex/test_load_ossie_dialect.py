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
from inline_snapshot import snapshot
from ossie import OssieDialect

from ossie_hex.ossie_to_hex.context import ExportContext
from ossie_hex.ossie_to_hex.load_ossie_dialect import load_ossie_dialect
from tests.utils import problems_snapshot


@pytest.fixture
def ctx() -> ExportContext:
    return ExportContext()


@pytest.mark.parametrize("ossie_dialect", list(OssieDialect))
def test_load_enum(ctx: ExportContext, ossie_dialect: OssieDialect) -> None:
    result = load_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result == ossie_dialect.value
    assert type(result) is str
    assert not ctx.problems


@pytest.mark.parametrize("ossie_dialect", list(OssieDialect))
def test_load_wellknown_string(ctx: ExportContext, ossie_dialect: OssieDialect) -> None:
    result = load_ossie_dialect(ossie_dialect.value, ctx=ctx)

    assert result == ossie_dialect.value
    assert not ctx.problems


@pytest.mark.parametrize("ossie_dialect", list(OssieDialect))
def test_load_wellknown_lowercase_string(
    ctx: ExportContext, ossie_dialect: OssieDialect
) -> None:
    result = load_ossie_dialect(ossie_dialect.value.lower(), ctx=ctx)

    assert result == ossie_dialect.value.lower()
    assert not ctx.problems


def test_load_freeform_string_preserves_case(ctx: ExportContext) -> None:
    ossie_dialect = "Unknown_Dialect"
    result = load_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result == "Unknown_Dialect"
    assert problems_snapshot(ctx.problems) == snapshot(
        "[INFO] Unknown dialect: Unknown_Dialect."
    )


def test_load_freeform_string() -> None:
    ctx = ExportContext()
    ossie_dialect = "UNKNOWN_DIALECT"

    result = load_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result == ossie_dialect
    assert problems_snapshot(ctx.problems) == snapshot(
        "[INFO] Unknown dialect: UNKNOWN_DIALECT."
    )


def test_load_none(ctx: ExportContext) -> None:
    ossie_dialect = None

    result = load_ossie_dialect(ossie_dialect, ctx=ctx)

    assert result is None
    assert not ctx.problems
