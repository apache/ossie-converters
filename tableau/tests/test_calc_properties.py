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
from hypothesis import HealthCheck, given, settings, strategies as st

from ossie_tableau.calc_parser import from_sql, translate

pytestmark = pytest.mark.unit

num_leaf = st.one_of(st.sampled_from(["[a]", "[b]", "[c]"]), st.integers(0, 99).map(str))
str_leaf = st.one_of(st.sampled_from(["[s]", "[t]"]), st.sampled_from(["'x'", "'yz'", "''"]))
date_leaf = st.sampled_from(["[d]", "[e]", "TODAY()"])

num = st.deferred(lambda: st.one_of(
    num_leaf,
    st.tuples(num, st.sampled_from(["+", "-", "*", "/", "%"]), num).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
    st.tuples(st.sampled_from(["ABS", "CEILING", "FLOOR", "SQRT", "ZN", "INT", "LN", "LOG"]), num).map(lambda t: f"{t[0]}({t[1]})"),
    st.tuples(num, st.integers(0, 3)).map(lambda t: f"ROUND({t[0]}, {t[1]})"),
    st.tuples(st.sampled_from(["IFNULL"]), num, num).map(lambda t: f"IFNULL({t[1]}, {t[2]})"),
    st.tuples(boolean, num, num).map(lambda t: f"IF {t[0]} THEN {t[1]} ELSE {t[2]} END"),
    st.tuples(boolean, num, num).map(lambda t: f"IIF({t[0]}, {t[1]}, {t[2]})"),
    st.tuples(st.sampled_from(["year", "month", "day"]), date_leaf).map(lambda t: f"DATEPART('{t[0]}', {t[1]})"),
    st.tuples(st.sampled_from(["day", "month"]), date_leaf, date_leaf).map(lambda t: f"DATEDIFF('{t[0]}', {t[1]}, {t[2]})"),
    st.sampled_from(["LEN([s])"]),
))
boolean = st.deferred(lambda: st.one_of(
    st.tuples(num, st.sampled_from(["=", "<>", "<", "<=", ">", ">="]), num).map(lambda t: f"{t[0]} {t[1]} {t[2]}"),
    st.tuples(boolean, st.sampled_from(["AND", "OR"]), boolean).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
    boolean.map(lambda b: f"NOT ({b})"),
    num.map(lambda n: f"ISNULL({n})"),
    st.tuples(str_leaf, str_leaf).map(lambda t: f"CONTAINS({t[0]}, {t[1]})"),
))
agg = st.tuples(st.sampled_from(["SUM", "AVG", "MIN", "MAX", "COUNT", "COUNTD", "MEDIAN", "STDEV", "VAR"]), num).map(lambda t: f"{t[0]}({t[1]})")
text = st.deferred(lambda: st.one_of(
    str_leaf,
    st.tuples(text, text).map(lambda t: f"({t[0]} + {t[1]})"),
    st.tuples(st.sampled_from(["UPPER", "LOWER", "TRIM", "STR"]), st.one_of(text, num)).map(lambda t: f"{t[0]}({t[1]})"),
    st.tuples(st.sampled_from(["LEFT", "RIGHT"]), text, st.integers(1, 5)).map(lambda t: f"{t[0]}({t[1]}, {t[2]})"),
    st.tuples(text, st.integers(1, 3), st.integers(1, 3)).map(lambda t: f"MID({t[0]}, {t[1]}, {t[2]})"),
))
anything = st.one_of(num, boolean, text, agg)


@settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large])
@given(anything)
def test_sql_is_a_fixed_point_through_the_reverse(formula):
    sql = translate(formula).sql
    back = from_sql(sql)
    assert translate(back).sql == sql, (formula, sql, back)


def _ev(n, env):
    import sqlglot.expressions as x
    t = type(n)
    if t is x.Paren:
        return _ev(n.this, env)
    if t is x.Column:
        return env[n.name.lower()]
    if t is x.Literal:
        return int(n.name)
    ops = {x.EQ: lambda a, b: a == b, x.GT: lambda a, b: a > b, x.LT: lambda a, b: a < b, x.GTE: lambda a, b: a >= b,
           x.LTE: lambda a, b: a <= b, x.And: lambda a, b: a and b, x.Or: lambda a, b: a or b}
    if t in ops:
        return ops[t](_ev(n.this, env), _ev(n.expression, env))
    if t is x.Not:
        return not _ev(n.this, env)
    if t is x.Between:
        return _ev(n.args["low"], env) <= _ev(n.this, env) <= _ev(n.args["high"], env)
    raise AssertionError(t)


_bleaf = st.one_of(
    st.tuples(st.sampled_from(["x", "y"]), st.integers(0, 3), st.integers(0, 3)).map(lambda t: f"{t[0]} BETWEEN {t[1]} AND {t[2]}"),
    st.tuples(st.sampled_from(["x", "y"]), st.integers(0, 3)).map(lambda t: f"{t[0]} = {t[1]}"),
)
_bool_sql = st.deferred(lambda: st.one_of(
    _bleaf,
    st.tuples(_bool_sql, st.sampled_from(["AND", "OR"]), _bool_sql).map(lambda t: f"({t[0]} {t[1]} {t[2]})"),
    _bool_sql.map(lambda b: f"NOT ({b})"),
    _bool_sql.map(lambda b: f"NOT {b}" if " " not in b.strip("()") or "BETWEEN" in b else b),
))


@settings(max_examples=150, suppress_health_check=[HealthCheck.too_slow], deadline=None)
@given(_bool_sql)
def test_between_roundtrip_is_semantically_equal(s):
    import sqlglot
    back = translate(from_sql(s)).sql
    for xv in range(4):
        for yv in range(4):
            env = {"x": xv, "y": yv}
            assert _ev(sqlglot.parse_one(s), env) == _ev(sqlglot.parse_one(back), env), (s, back)
