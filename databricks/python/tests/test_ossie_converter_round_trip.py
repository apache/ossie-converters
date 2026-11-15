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

"""Port of OssieConverterRoundTripSuite.java.

Property-based round-trip tests. For any generated model in the round-trippable subset,
converting one direction and back preserves content:

  MV -> Ossie -> MV : source, every dimension/measure name+expr+metadata, every join
    (name/source/condition/cardinality/rely) and nesting, and model filter/comment/
    materialization.
  Ossie -> MV -> Ossie : dataset names+sources+fields, relationship from/to/columns,
    metric name+expr, and model description.

Uses a seeded java.util.Random-compatible generator (JavaRandom below) so no
property-testing library is needed and every seed generates exactly the model the Java
suite generates; each of NUM_SEEDS seeds is one generated model.
"""

import re

from ossie_databricks.ossie_converter import (
    MV_VERSION,
    OSSIE_VERSION,
    convert_metric_view_to_ossie,
    convert_ossie_to_metric_view,
    dump_yaml as _dump_yaml,
    parse_yaml,
)

NUM_SEEDS = 300
AGGS = ["SUM", "COUNT", "AVG", "MIN", "MAX"]


# --- JavaRandom: a bit-for-bit port of java.util.Random ---------------------
class JavaRandom:
    _MULTIPLIER = 0x5DEECE66D
    _ADDEND = 0xB
    _MASK = (1 << 48) - 1

    def __init__(self, seed):
        self._seed = (seed ^ self._MULTIPLIER) & self._MASK

    def _next(self, bits):
        self._seed = (self._seed * self._MULTIPLIER + self._ADDEND) & self._MASK
        result = self._seed >> (48 - bits)
        # Java narrows to a signed 32-bit int.
        result &= 0xFFFFFFFF
        if result >= 1 << 31:
            result -= 1 << 32
        return result

    def next_int(self, bound=None):
        if bound is None:
            return self._next(32)
        if bound <= 0:
            raise ValueError("bound must be positive")
        r = self._next(31)
        m = bound - 1
        if (bound & m) == 0:
            # Power of two: (int) ((bound * (long) r) >> 31).
            return (bound * r) >> 31
        u = r
        while True:
            r = u % bound
            # Java rejects when u - r + m overflows a signed 32-bit int.
            if u - r + m < (1 << 31):
                return r
            u = self._next(31)

    def next_boolean(self):
        return self._next(1) != 0

    def next_double(self):
        return ((self._next(26) << 27) + self._next(27)) * (1.0 / (1 << 53))


# --- Java toString emulation, for the string-normalized comparisons ---------
def java_str(x):
    """Renders a value the way Java's String.valueOf / collection toString would."""
    if x is None:
        return "null"
    if isinstance(x, bool):
        return "true" if x else "false"
    if isinstance(x, dict):
        return "{" + ", ".join(java_str(k) + "=" + java_str(v) for k, v in x.items()) + "}"
    if isinstance(x, (list, tuple)):
        return "[" + ", ".join(java_str(v) for v in x) + "]"
    return str(x)


# --- Rnd: the small interface the builders depend on ------------------------
class Rnd:
    def __init__(self, seed):
        self.r = JavaRandom(seed)

    def chance(self, p):
        return self.r.next_double() < p

    def count(self, lo, hi):
        return lo + self.r.next_int(hi - lo + 1)

    def pick(self, seq):
        return seq[self.r.next_int(len(seq))]

    def text(self):
        alnum = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
        n = self.r.next_int(11)
        b = [alnum[self.r.next_int(len(alnum))]]
        alnum_space = alnum + " "
        for _ in range(n):
            b.append(alnum_space[self.r.next_int(len(alnum_space))])
        # Java String.trim(); only spaces can occur here.
        s = "".join(b).strip(" ")
        return "x" if s == "" else s

    def colname(self):
        lower = "abcdefghijklmnopqrstuvwxyz_"
        rest = lower + "0123456789"
        b = [lower[self.r.next_int(len(lower))]]
        n = self.r.next_int(8)
        for _ in range(n):
            b.append(rest[self.r.next_int(len(rest))])
        return "".join(b)


class Names:
    def __init__(self):
        self.n = {}

    def next(self, prefix):
        i = self.n.get(prefix, 0)
        self.n[prefix] = i + 1
        return prefix + str(i)


def three_part(rnd):
    return rnd.colname() + "." + rnd.colname() + "." + rnd.colname()


def maybe_meta(rnd, target):
    if rnd.chance(0.4):
        target["comment"] = rnd.text()
    if rnd.chance(0.3):
        target["display_name"] = rnd.text()
    if rnd.chance(0.3):
        syns = []
        k = rnd.count(1, 3)
        for _ in range(k):
            syns.append(rnd.text())
        target["synonyms"] = syns
    if rnd.chance(0.25):
        fmt = {}
        type_ = rnd.pick(["number", "currency", "date"])
        fmt["type"] = type_
        if type_ == "currency":
            fmt["currency_code"] = "USD"
        target["format"] = fmt


# --- Metric View builder (for MV -> Ossie -> MV) -----------------------------
def build_join(rnd, names, parent_alias, depth, ancestor_path, qual_paths, otm):
    """Builds one join subtree. ``qual_paths`` collects the alias path of every join produced
    (`j0`, `j0.j1`, ...) so a measure can be generated over a joined column.

    ``otm`` marks the whole branch one-to-many: a Metric View requires one cardinality per
    top-level branch, and a column on a one-to-many-joined table cannot be a dimension (the
    converter drops it, as Databricks would reject it), so an otm branch contributes measures but
    no dimensions. An otm join is also what makes the import stash the fact hint, so these seeds
    are the ones that exercise that path.
    """
    name = names.next("j")
    path = list(ancestor_path)
    path.append(name)
    qual = ".".join(path)
    qual_paths.append(qual)
    join = {}
    join["name"] = name
    join["source"] = three_part(rnd)
    if rnd.chance(0.5):
        ncols = rnd.count(1, 2)
        using = []
        for i in range(ncols):
            using.append("u" + str(i) + "_" + rnd.colname())
        join["using"] = using
    else:
        ncols = rnd.count(1, 2)
        clauses = []
        for i in range(ncols):
            pc = "fk" + str(i) + "_" + rnd.colname()
            cc = "pk" + str(i) + "_" + rnd.colname()
            clauses.append(parent_alias + "." + pc + " = " + name + "." + cc)
        join["on"] = " AND ".join(clauses)
    if otm:
        join["cardinality"] = "one_to_many"
    elif rnd.chance(0.4):
        join["cardinality"] = "many_to_one"
    if rnd.chance(0.3):
        rely = {}
        rely["at_most_one_match"] = True
        join["rely"] = rely
    dims = []
    nd = 0 if otm else rnd.count(0, 2)
    for _ in range(nd):
        col = rnd.colname()
        if rnd.chance(0.7):
            expr = qual + "." + col
        else:
            expr = qual + "." + col + " + " + qual + "." + rnd.colname()
        dim = {}
        dim["name"] = names.next("c")
        dim["expr"] = expr
        maybe_meta(rnd, dim)
        dims.append(dim)
    if depth < 2 and rnd.chance(0.35):
        # Cardinality is inherited: a Metric View rejects a branch that mixes the two.
        child_join, child_dims = build_join(
            rnd, names, name, depth + 1, path, qual_paths, otm)
        join["joins"] = [child_join]
        dims.extend(child_dims)
    return join, dims


def build_metric_view(rnd):
    names = Names()
    mv = {}
    mv["version"] = MV_VERSION
    mv["source"] = three_part(rnd)
    if rnd.chance(0.4):
        mv["comment"] = rnd.text()
    if rnd.chance(0.3):
        mv["filter"] = rnd.colname() + " > 0"
    fields = []
    joins = []
    nsrc = rnd.count(0, 3)
    for _ in range(nsrc):
        col = rnd.colname()
        expr = col if rnd.chance(0.7) else "UPPER(" + col + ")"
        dim = {}
        dim["name"] = names.next("c")
        dim["expr"] = expr
        maybe_meta(rnd, dim)
        fields.append(dim)
    njoins = rnd.count(0, 2)
    qual_paths = []
    for _ in range(njoins):
        # Java evaluates the chance() argument before build_join runs; so does Python.
        jr_join, jr_dims = build_join(rnd, names, "source", 0, [], qual_paths, rnd.chance(0.25))
        joins.append(jr_join)
        fields.extend(jr_dims)
    measures = []
    nmeas = rnd.count(0, 2)
    for _ in range(nmeas):
        m = {}
        m["name"] = names.next("c")
        # A measure over a JOINED column -- `j0.col`, and `j0.j1.col` for a nested join -- is the
        # shape the qualifier rewrite exists to handle: the import de-aliases the path down to the
        # dataset name and the export expands it back. Generating only `AGG(bare_col)` left both
        # sides of that rewrite untested. A bare column is left bare deliberately: the export
        # strips the fact qualifier (`SUM(source.x)` -> `SUM(x)`, the Metric View idiom), so a
        # `source.`-qualified measure is a normalization rather than a round trip.
        if qual_paths and rnd.chance(0.4):
            m["expr"] = (rnd.pick(AGGS) + "(" + rnd.pick(qual_paths) + "." + rnd.colname()
                         + ")")
        else:
            m["expr"] = rnd.pick(AGGS) + "(" + rnd.colname() + ")"
        if rnd.chance(0.4):
            m["comment"] = rnd.text()
        if rnd.chance(0.3):
            syns = []
            k = rnd.count(1, 3)
            for _ in range(k):
                syns.append(rnd.text())
            m["synonyms"] = syns
        if rnd.chance(0.3):
            w = {}
            w["order"] = rnd.colname()
            w["range"] = "trailing 7 day"
            m["window"] = [w]
        measures.append(m)
    if joins:
        mv["joins"] = joins
    # A Metric View requires at least one dimension or measure, so a model with neither is outside
    # the round-trippable subset (the converter rejects it, as Databricks would). Both counts can
    # independently come out zero, so add one dimension when that happens.
    if not fields and not measures:
        dim = {}
        dim["name"] = names.next("c")
        dim["expr"] = rnd.colname()
        fields.append(dim)
    if fields:
        mv["fields"] = fields
    if measures:
        mv["measures"] = measures
    if rnd.chance(0.2):
        mat = {}
        mat["schedule"] = "every 6 hours"
        mat["mode"] = rnd.pick(["relaxed", "strict"])
        mv["materialization"] = mat
    return mv


# --- Ossie builder (for Ossie -> MV -> Ossie) --------------------------------
def ossie_field(name, expr):
    dialect = {}
    dialect["dialect"] = "DATABRICKS"
    dialect["expression"] = expr
    expression = {}
    expression["dialects"] = [dialect]
    field = {}
    field["name"] = name
    field["expression"] = expression
    return field


def build_ossie(rnd):
    names = Names()
    fact = "fact"
    datasets = []
    fact_ds = {}
    fact_ds["name"] = fact
    fact_ds["source"] = "c.s." + fact
    datasets.append(fact_ds)
    relationships = []

    n_dims = rnd.count(0, 3)
    reachable = [fact]
    for i in range(n_dims):
        dname = names.next("dim")
        parent = rnd.pick(reachable)
        ds = {}
        ds["name"] = dname
        ds["source"] = "c.s." + rnd.colname() + str(i)
        datasets.append(ds)
        reachable.append(dname)
        rel = {}
        rel["name"] = names.next("r")
        rel["from"] = parent
        rel["to"] = dname
        if rnd.chance(0.5):
            cols = []
            k = rnd.count(1, 2)
            for _ in range(k):
                cols.append(rnd.colname())
            rel["from_columns"] = list(cols)
            rel["to_columns"] = list(cols)
        else:
            n = rnd.count(1, 2)
            fcols = []
            tcols = []
            for j in range(n):
                fcols.append("fk" + str(j) + "_" + rnd.colname())
                tcols.append("pk" + str(j) + "_" + rnd.colname())
            rel["from_columns"] = fcols
            rel["to_columns"] = tcols
        relationships.append(rel)
    for ds in datasets:
        flds = []
        nf = rnd.count(0, 3)
        for _ in range(nf):
            flds.append(ossie_field(names.next("c"), rnd.colname()))
        if flds:
            ds["fields"] = flds
    metrics = []
    nm = rnd.count(0, 2)
    # Datasets other than the fact, whose columns a metric addresses as `<dataset>.col`. The
    # export rewrites that to the join path the Metric View needs (`parent.child.col` when the
    # dataset is nested) and the import maps it back, so a qualified metric exercises both halves.
    joined = list(reachable[1:])
    for _ in range(nm):
        if joined and rnd.chance(0.4):
            expr = rnd.pick(AGGS) + "(" + rnd.pick(joined) + "." + rnd.colname() + ")"
        else:
            expr = rnd.pick(AGGS) + "(" + rnd.colname() + ")"
        metrics.append(ossie_field(names.next("c"), expr))
    # The converted Metric View needs at least one dimension or measure, so a model whose datasets
    # have no fields and which declares no metrics is outside the round-trippable subset. Give the
    # first dataset a field when nothing else would produce a column.
    any_field = False
    for ds in datasets:
        if as_list(ds.get("fields")):
            any_field = True
            break
    if not any_field and not metrics:
        datasets[0]["fields"] = [ossie_field(names.next("c"), rnd.colname())]
    model = {}
    model["name"] = names.next("m")
    if rnd.chance(0.4):
        model["description"] = rnd.text()
    model["datasets"] = datasets
    if relationships:
        model["relationships"] = relationships
    if metrics:
        model["metrics"] = metrics
    out = {}
    out["version"] = OSSIE_VERSION
    out.update(model)
    return out


# --- Round-trip assertions ---------------------------------------------------
def as_map(x):
    return x if isinstance(x, dict) else {}


def as_list(x):
    return x if isinstance(x, list) else []


def dump_yaml(obj):
    return _dump_yaml(obj)


_AND_SPLIT = re.compile(r"\s+AND\s+", re.IGNORECASE)


def cond_canon(join):
    using = as_list(join.get("using"))
    if using:
        return "using:" + java_str(sorted({java_str(u) for u in using}))
    on = join.get("on")
    if on is None:
        return "none"
    pairs = set()
    for clause in _AND_SPLIT.split(java_str(on)):
        lr = clause.split("=", 1)
        pairs.add(lr[0].strip() + "=" + lr[1].strip())
    return "on:" + java_str(sorted(pairs))


def flatten_joins(joins, parent, acc, edges):
    for j_obj in joins:
        j = as_map(j_obj)
        name = j.get("name")
        acc[name] = (java_str(j.get("source")) + "|" + cond_canon(j) + "|"
                     + java_str(j.get("cardinality")) + "|" + java_str(j.get("rely")))
        edges.add(java_str(parent) + "->" + java_str(name))
        flatten_joins(as_list(j.get("joins")), name, acc, edges)


def dims(mv):
    d = as_list(mv.get("dimensions"))
    return d if d else as_list(mv.get("fields"))


def dim_norm(d):
    return "|".join(java_str(d.get(k))
                    for k in ("expr", "comment", "display_name", "synonyms", "format"))


def meas_norm(m):
    return "|".join(java_str(m.get(k))
                    for k in ("expr", "comment", "synonyms", "format", "window"))


def by_name(items, measure):
    out = {}
    for o in items:
        m = as_map(o)
        out[m.get("name")] = meas_norm(m) if measure else dim_norm(m)
    return out


def assert_mv_round_trip(mv, seed):
    ossie_yaml = convert_metric_view_to_ossie(dump_yaml(mv), None).yaml
    mv2 = as_map(parse_yaml(convert_ossie_to_metric_view(ossie_yaml, None).yaml))

    ctx = " (seed " + str(seed) + ")"
    assert mv.get("source") == mv2.get("source"), "source" + ctx
    assert mv.get("comment") == mv2.get("comment"), "comment" + ctx
    assert mv.get("filter") == mv2.get("filter"), "filter" + ctx
    assert mv.get("materialization") == mv2.get("materialization"), "materialization" + ctx
    assert by_name(dims(mv), False) == by_name(dims(mv2), False), "fields" + ctx
    assert by_name(as_list(mv.get("measures")), True) == \
        by_name(as_list(mv2.get("measures")), True), "measures" + ctx

    a1 = {}
    e1 = set()
    flatten_joins(as_list(mv.get("joins")), "source", a1, e1)
    a2 = {}
    e2 = set()
    flatten_joins(as_list(mv2.get("joins")), "source", a2, e2)
    assert a1 == a2, "joins" + ctx
    assert e1 == e2, "join nesting" + ctx


# Import labels each expression with the most portable dialect it fits, so a DATABRICKS
# expression can come back as OSSIE_SQL_2026 or ANSI_SQL; the round trip preserves its text.
CONVERTER_DIALECTS = {"DATABRICKS", "ANSI_SQL", "OSSIE_SQL_2026"}


def expr_of(obj):
    for d_obj in as_list(as_map(obj.get("expression")).get("dialects")):
        d = as_map(d_obj)
        if d.get("dialect") in CONVERTER_DIALECTS:
            return d.get("expression")
    return None


def fields_map(ds):
    out = {}
    for f_obj in as_list(ds.get("fields")):
        f = as_map(f_obj)
        out[f.get("name")] = expr_of(f)
    return out


def rel_set(model):
    out = set()
    for r_obj in as_list(model.get("relationships")):
        r = as_map(r_obj)
        out.add(java_str(r.get("from")) + "->" + java_str(r.get("to")) + "|"
                + java_str(as_list(r.get("from_columns"))) + "|"
                + java_str(as_list(r.get("to_columns"))))
    return out


def assert_ossie_round_trip(ossie, seed):
    mv_yaml = convert_ossie_to_metric_view(dump_yaml(ossie), None).yaml
    ossie2 = as_map(parse_yaml(convert_metric_view_to_ossie(mv_yaml, None).yaml))

    ctx = " (seed " + str(seed) + ")"
    m1 = ossie
    m2 = ossie2

    d1 = {}
    for ds_obj in as_list(m1.get("datasets")):
        ds = as_map(ds_obj)
        d1[ds.get("name")] = java_str(ds.get("source")) + "|" + java_str(fields_map(ds))
    d2 = {}
    for ds_obj in as_list(m2.get("datasets")):
        ds = as_map(ds_obj)
        d2[ds.get("name")] = java_str(ds.get("source")) + "|" + java_str(fields_map(ds))
    assert d1 == d2, "datasets" + ctx
    assert rel_set(m1) == rel_set(m2), "relationships" + ctx

    met1 = {}
    for x in as_list(m1.get("metrics")):
        met1[as_map(x).get("name")] = expr_of(as_map(x))
    met2 = {}
    for x in as_list(m2.get("metrics")):
        met2[as_map(x).get("name")] = expr_of(as_map(x))
    assert met1 == met2, "metrics" + ctx
    assert m1.get("description") == m2.get("description"), "description" + ctx


def test_metric_view_round_trip_across_seeds():
    for seed in range(NUM_SEEDS):
        assert_mv_round_trip(build_metric_view(Rnd(seed)), seed)


def test_ossie_round_trip_across_seeds():
    for seed in range(NUM_SEEDS):
        assert_ossie_round_trip(build_ossie(Rnd(seed)), seed)
