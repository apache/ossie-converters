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
from hypothesis import given, settings, strategies as st

from ossie_tableau import convert as C
from ossie_tableau.calc_catalog import CATALOG
from ossie_tableau.convert import convert_datasource

from .test_units import BASE, calc, ds

pytestmark = pytest.mark.unit

_AGG_NAMES = sorted(e.name for e in CATALOG if e.group == "aggregate")


def _sql(r, dialect="SNOWFLAKE"):
    ex = r.document.metrics[0].expression.dialects
    return next(d.expression for d in ex if str(getattr(d.dialect, "value", d.dialect)) == dialect)


# BASE is a Snowflake connection with lower-case remote names, so every column is quoted to keep its case.
@pytest.mark.parametrize("formula,sql", [
    ("STDEV([amt])", 'STDDEV(("amt"))'),
    ("STDEVP([amt])", 'STDDEV_POP(("amt"))'),
    ("VAR([amt])", 'VARIANCE(("amt"))'),
    ("VARP([amt])", 'VARIANCE_POP(("amt"))'),
    ("PERCENTILE([amt], 0.9)", 'PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY ("amt"))'),
    ("CORR([amt], [odd col])", 'CORR(("amt"), ("odd col"))'),
    ("COVAR([amt], [odd col])", 'COVAR_SAMP(("amt"), ("odd col"))'),
    ("COVARP([amt], [odd col])", 'COVAR_POP(("amt"), ("odd col"))'),
    ("ATTR([amt])", 'MIN(("amt"))'),
])
def test_every_aggregate_becomes_a_metric(formula, sql):
    r = convert_datasource(ds(BASE, calc(formula)))
    assert r.stats["metrics"] == 1 and not r.stats["unconverted_calcs"], formula
    assert "C" not in {f.name for d in r.document.datasets for f in d.fields}  # not a row-level field
    assert _sql(r) == sql


@pytest.mark.parametrize("formula", ["CORR([amt], [when])", "COVAR([when], [amt])", "PERCENTILE([when], 0.5)", "STDEV([name])"])
def test_non_numeric_reference_in_any_argument_is_caught(formula):
    r = convert_datasource(ds(BASE, calc(formula)))
    assert r.stats["metrics"] == 0
    assert "non_numeric_aggregate" in {x.kind for x in r.losses} | {
        u["reason"] for u in __import__("json").loads(r.document.custom_extensions[0].data)["unconverted_calculations"]}


@pytest.mark.parametrize("agg", ["STDEV", "STDEVP", "VAR", "VARP", "MEDIAN", "SUM", "COUNTD"])
def test_helper_split_covers_single_argument_aggregates(agg):
    from types import SimpleNamespace
    t = SimpleNamespace(name="t")
    assert C._helper_aggregate(f"{agg}([t].[x] * 2)", [t]) == (agg, "[t].[x] * 2", t)


@pytest.mark.parametrize("out", ["ATTR([t].[x] * 2)", "CORR([t].[x] * 2, [t].[y])", "PERCENTILE([t].[x] * 2, 0.5)"])
def test_helper_split_skips_attr_and_multi_argument_aggregates(out):
    from types import SimpleNamespace
    assert C._helper_aggregate(out, [SimpleNamespace(name="t")]) is None


def test_regexes_cover_every_catalogue_aggregate():
    for name in _AGG_NAMES:
        assert C.AGG_RE.search(f"{name}("), name
        assert C.BARE_AGG_RE.fullmatch(f"{name}([x])" if name not in ("CORR", "COVAR", "COVARP", "PERCENTILE") else
                                       f"{name}([x], {'0.5' if name == 'PERCENTILE' else '[y]'})"), name


@settings(max_examples=200, deadline=None)
@given(st.sampled_from(_AGG_NAMES), st.sampled_from(["", " ", "  "]), st.booleans(), st.sampled_from(["[a]", "[a] + 1", "IF [a] > 0 THEN [a] END"]))
def test_calc_aggregated_for_any_catalogue_aggregate(name, gap, lower, arg):
    fn = name.lower() if lower else name
    assert C._calc_aggregated(f"1 + {fn}{gap}({arg})")
    assert not C._calc_aggregated(f"{arg} + 1")
