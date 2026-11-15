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

"""EXPORT direction: Apache Ossie semantic model -> Databricks Metric View v1.1 YAML.

This is the harder direction: it reassembles a relationship graph into a join tree. Shared helpers
come from :mod:`ossie_databricks.ossie_converter_common`; the public entry point is re-exported
through :mod:`ossie_databricks.ossie_converter`.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from ossie_databricks import expression_dialect, joined_integer_sum
from ossie_databricks.ossie_converter import (
    MV_VERSION,
    OSSIE_VERSION,
    ConversionError,
    Notices,
    Result,
)
from ossie_databricks.ossie_converter_common import (
    CARD_MANY_TO_ONE,
    CARD_ONE_TO_MANY,
    DIALECT_DATABRICKS,
    MAX_JOIN_NODES,
    STASH_SOURCE_KEY,
    SYNONYM_LIMIT,
    as_list,
    as_map,
    cube_integer_sum_metadata,
    find_outside_literals,
    foreign_vendor_extensions,
    is_simple_identifier,
    jlist,
    js,
    jstr,
    load_yaml,
    merge_description,
    pick_expression,
    qualifier_chain_pattern,
    read_stash,
    replace_outside_literals,
    require,
    require_str,
    rewrite_qualifiers,
    str_list,
    synonyms_of,
    truthy,
    validate_source,
    write_yaml,
)


class _Node:
    """A join tree node: one dataset reached by one join path."""

    __slots__ = ("dataset", "rel", "parent_is_from", "alias", "is_otm", "children")

    def __init__(self, dataset: str, rel: dict[Any, Any], parent_is_from: bool) -> None:
        self.dataset = dataset
        self.rel = rel  # empty for the fact/root
        self.parent_is_from = parent_is_from
        self.alias: str = ""
        self.is_otm = False
        self.children: list[_Node] = []


def _equals_ignore_case(a: str | None, b: str | None) -> bool:
    if a is None or b is None:
        return False
    return len(a) == len(b) and (a.upper() == b.upper() or a.lower() == b.lower())


# -- public entry ---------------------------------------------------------------
def convert_ossie_to_metric_view(ossie_yaml_str: str | None, source: str | None = None) -> Result:
    notices = Notices()
    if ossie_yaml_str is None or ossie_yaml_str.strip() == "":
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: expected a mapping at the root", "the input is empty"
        )
    try:
        parsed = load_yaml(ossie_yaml_str)
    except Exception as e:  # noqa: BLE001 - any parser failure is invalid input
        raise ConversionError.invalid_input(
            f"Invalid Apache Ossie YAML: {e}", f"failed to parse YAML: {e}"
        ) from e
    if not isinstance(parsed, dict):
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: expected a mapping at the root",
            "it is not a mapping at the root",
        )
    root = parsed
    version_value = root.get("version")
    version = jstr(version_value)
    if version_value is None or version.strip() == "":  # type: ignore[union-attr]
        raise ConversionError.invalid_input(
            "Invalid Apache Ossie YAML: missing required 'version' field",
            "it is missing the required 'version' field",
        )
    if version != OSSIE_VERSION:
        raise ConversionError.for_unsupported_version(
            f"Unsupported Apache Ossie version '{version}'. Supported: {OSSIE_VERSION}",
            version,  # type: ignore[arg-type]
        )
    if "semantic_model" in root:
        reason = (
            "Legacy 'semantic_model' wrappers are not supported; place the model "
            "properties directly at the document root"
        )
        raise ConversionError.invalid_input(reason, reason)
    if "dialects" in root or "vendors" in root:
        reason = "Root 'dialects' and 'vendors' are not supported by the Apache Ossie spec"
        raise ConversionError.invalid_input(reason, reason)
    if not isinstance(root.get("name"), str):
        reason = "Apache Ossie model requires a string 'name' at the document root"
        raise ConversionError.invalid_input(reason, reason)
    view = _convert_model(root, source, notices)
    try:
        text = write_yaml(view)
    except Exception as e:  # noqa: BLE001
        raise ConversionError.internal_error(
            f"failed to serialize Metric View YAML: {e}"
        ) from e
    return Result(text, notices.to_list())


def _convert_model(
    model: dict[Any, Any], explicit_source: str | None, notices: Notices
) -> dict[str, Any]:
    name = js(model.get("name")) if "name" in model else "<unnamed>"
    model_scope = f"Model '{name}'"
    dataset_list = _schema_map_list(model, "datasets", model_scope)
    if not dataset_list:
        raise ConversionError(f"Model '{name}' has no datasets")
    seen: set[str] = set()
    datasets: dict[str, dict[Any, Any]] = {}
    dataset_fields: dict[str, list[dict[Any, Any]]] = {}
    for d in dataset_list:
        ds_name = require_str(d, "name", f"Model '{name}': dataset")
        key = ds_name.strip().lower()
        if key in seen:
            raise ConversionError(f"Model '{name}': duplicate dataset name '{ds_name}'")
        seen.add(key)
        datasets[ds_name] = d
        dataset_fields[ds_name] = _schema_map_list(d, "fields", f"Dataset '{ds_name}'")
    relationships = _schema_map_list(model, "relationships", model_scope)
    metrics = _schema_map_list(model, "metrics", model_scope)

    model_stash = read_stash(model, notices, model_scope)
    fact_hint = explicit_source if explicit_source else jstr(model_stash.get(STASH_SOURCE_KEY))
    root, fact = _build_join_tree(name, datasets, relationships, fact_hint, notices)
    counts = _assign_aliases(root, fact)
    _mark_otm(name, root)

    fact_ds = datasets[fact]
    view: dict[str, Any] = {
        "version": MV_VERSION,
        "source": validate_source(fact_ds.get("source"), fact),
    }

    comment = jstr(model.get("description"))
    if truthy(comment):
        view["comment"] = comment
    if "filter" in model_stash:
        view["filter"] = model_stash["filter"]

    joins = [_build_join(child, "source", datasets, notices) for child in root.children]
    if joins:
        view["joins"] = joins

    # Insertion-ordered dicts stand in for the Java sets, keeping notice order deterministic.
    dropped_dims: dict[str, None] = {}
    dropped_measures: dict[str | None, None] = {}
    dimensions: list[dict[str, Any]] = []
    seen_dims: set[str] = set()
    # Dataset name -> the alias path that addresses its columns from the primary source, collected
    # from the same walk the dimensions use so measures qualify identically (see _qualify_measure).
    dataset_alias_path: dict[str, str] = {}
    integer_sum_keys: dict[str, str] = {}
    for node, join_path in _node_order(root):
        is_fact = node is root
        prefix = node.alias if counts[node.dataset] > 1 else None
        qualifier = ".".join(join_path)
        fields = dataset_fields[node.dataset]
        sum_key = (
            None
            if is_fact or node.is_otm or prefix is not None
            else _single_physical_primary_key(datasets[node.dataset], fields)
        )
        if not is_fact:
            # A dataset reachable by more than one path (diamond) appears once per path; the first
            # wins, matching the order dimensions are emitted in.
            dataset_alias_path.setdefault(node.dataset, qualifier)
        for field in fields:
            fname = require_str(field, "name", f"dataset '{node.dataset}': field")
            if node.is_otm:
                notices.warn(
                    f"field '{fname}'",
                    "column on a one-to-many-joined table cannot be a dimension "
                    "(must resolve to one value per source row); dropped",
                )
                dropped_dims[fname] = None
                continue
            dim = _convert_field(field, fname, qualifier, is_fact, prefix, notices)
            if dim is None:
                dropped_dims[fname] = None
                continue
            dn = dim["name"]
            if dn.lower() in seen_dims:
                raise ConversionError(
                    f"dataset '{node.dataset}': dimension name '{dn}' collides with another "
                    "dimension/measure; Metric Views require unique dimension/measure names -- "
                    "rename before use"
                )
            seen_dims.add(dn.lower())
            dimensions.append(dim)
            if (
                sum_key is not None
                and field.get("datatype") == "Integer"
                and f"{qualifier}.{fname}" == dim.get("expr")
            ):
                integer_sum_keys[f"{qualifier}.{fname}"] = f"{qualifier}.{sum_key}"

    measures: list[dict[str, Any]] = []
    # Depends only on the fact name, so compile it once rather than per metric.
    fact_qualifier = re.compile(r"\b" + re.escape(fact) + r"\.", re.ASCII)
    # Likewise fixed once the join tree is walked: one alternation over the joined dataset names.
    dataset_head = qualifier_chain_pattern(dataset_alias_path.keys())
    # Datasets reached by more than one join path (a diamond). A measure names a dataset, so a bare
    # reference to one of these cannot be unambiguously qualified (see _convert_metric).
    diamond_datasets = {j for j in dataset_alias_path if counts.get(j, 1) > 1}
    for metric in metrics:
        measure = _convert_metric(
            metric, fact_qualifier, dataset_head, dataset_alias_path, diamond_datasets,
            integer_sum_keys, seen_dims, notices,
        )  # fmt: skip
        if measure is None:
            dropped_measures[jstr(metric.get("name"))] = None
            continue
        measures.append(measure)

    _CascadeDropper(dimensions, measures, dropped_dims, dropped_measures, notices).run()

    # A Metric View must define at least one dimension or measure, so a view with neither is one
    # the engine refuses at CREATE. Fail here instead, naming the dropped columns: after the
    # cascade the emptiness is usually a consequence of earlier drops rather than an empty input.
    if not dimensions and not measures:
        msg = (
            f"Model '{name}' produced no dimensions or measures; "
            "a Metric View requires at least one."
        )
        if dropped_dims or dropped_measures:
            msg += " Dropped during conversion:"
            if dropped_dims:
                msg += " dimensions " + _sorted_names(dropped_dims)
            if dropped_measures:
                msg += " measures " + _sorted_names(dropped_measures)
            msg += " -- see the warnings for why each was dropped."
        raise ConversionError(msg)

    if dimensions:
        view["dimensions"] = dimensions
    if measures:
        view["measures"] = measures
    if "parameters" in model_stash:
        view["parameters"] = model_stash["parameters"]
    if "materialization" in model_stash:
        view["materialization"] = model_stash["materialization"]

    _warn_dropped_model(model, notices)
    return view


def _schema_map_list(parent: dict[Any, Any], key: str, scope: str) -> list[dict[Any, Any]]:
    result: list[dict[Any, Any]] = []
    for index, element in enumerate(_schema_list(parent, key, scope)):
        if not isinstance(element, dict):
            reason = f"{scope}: '{key}[{index}]' must be a mapping"
            raise ConversionError.invalid_input(reason, reason)
        result.append(element)
    return result


def _schema_list(parent: dict[Any, Any], key: str, scope: str) -> list[Any]:
    # A present-but-null value (the bare `key:` YAML idiom) is treated like an absent key. A scalar
    # or map value is still rejected. Required collections stay guarded by their own emptiness
    # checks.
    value = parent.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        reason = f"{scope}: '{key}' must be a list"
        raise ConversionError.invalid_input(reason, reason)
    return value


def _build_join_tree(
    model_name: str,
    datasets: dict[str, dict[Any, Any]],
    relationships0: list[dict[Any, Any]],
    fact_hint: str | None,
    notices: Notices,
) -> tuple[_Node, str]:
    for rel in relationships0:
        rel_name = js(rel.get("name")) if "name" in rel else "<unnamed>"
        scope = f"Model '{model_name}': relationship '{rel_name}'"
        f = require(rel, "from", scope)
        t = require(rel, "to", scope)
        if jstr(f) not in datasets or jstr(t) not in datasets:
            raise ConversionError(
                f"Model '{model_name}': relationship '{js(rel.get('name'))}' "
                "references an unknown dataset"
            )
        for key in ("from_columns", "to_columns"):
            if rel.get(key) is not None and not isinstance(rel.get(key), list):
                raise ConversionError(f"{scope}: from_columns and to_columns must be lists")
    relationships = [_orient_by_key(rel, datasets, notices) for rel in relationships0]
    if len(datasets) > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}' has {len(datasets)} datasets; "
            f"at most {MAX_JOIN_NODES} are supported."
        )
    fact = _pick_fact(model_name, datasets, relationships, fact_hint)
    _reject_directed_cycle(model_name, datasets, relationships)

    adj: dict[str, list[str]] = {n: [] for n in datasets}
    for rel in relationships:
        adj[jstr(rel.get("from"))].append(jstr(rel.get("to")))  # type: ignore[index,arg-type]
        adj[jstr(rel.get("to"))].append(jstr(rel.get("from")))  # type: ignore[index,arg-type]
    dist: dict[str, int] = {fact: 0}
    queue: deque[str] = deque([fact])
    while queue:
        cur = queue.popleft()
        for nb in adj[cur]:
            if nb not in dist:
                dist[nb] = dist[cur] + 1
                queue.append(nb)
    unreachable = sorted(n for n in datasets if n not in dist)
    if unreachable:
        raise ConversionError(
            f"Model '{model_name}': datasets {jlist(unreachable)} are not reachable from fact "
            f"'{fact}' via relationships."
        )
    children_of: dict[str, list[tuple[str, dict[Any, Any], bool]]] = {n: [] for n in datasets}
    for rel in relationships:
        a = jstr(rel.get("from"))
        b = jstr(rel.get("to"))
        if dist[a] == dist[b]:  # type: ignore[index]
            raise ConversionError(
                f"Model '{model_name}': relationship '{js(rel.get('name'))}' joins two datasets "
                "equidistant from the fact; the graph is not tree-shaped (it contains a cycle)."
            )
        parent, child = (a, b) if dist[a] < dist[b] else (b, a)  # type: ignore[index]
        children_of[parent].append((child, rel, parent == jstr(rel.get("from"))))  # type: ignore
    counter = [0]
    root = _build(model_name, fact, {}, False, children_of, counter)
    return root, fact


def _build(
    model_name: str,
    dataset: str,
    rel: dict[Any, Any],
    parent_is_from: bool,
    children_of: dict[str, list[tuple[str, dict[Any, Any], bool]]],
    counter: list[int],
) -> _Node:
    counter[0] += 1
    if counter[0] > MAX_JOIN_NODES:
        raise ConversionError(
            f"Model '{model_name}': join graph fans out to more than {MAX_JOIN_NODES} joins; "
            "check for an unintended diamond explosion."
        )
    node = _Node(dataset, rel, parent_is_from)
    for child, crel, child_parent_is_from in children_of[dataset]:
        node.children.append(
            _build(model_name, child, crel, child_parent_is_from, children_of, counter)
        )
    return node


def _assign_aliases(root: _Node, fact: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    _count_node(root, counts)
    used = {"source"}
    _assign(root, None, fact, counts, used)
    return counts


def _count_node(node: _Node, counts: dict[str, int]) -> None:
    counts[node.dataset] = counts.get(node.dataset, 0) + 1
    for c in node.children:
        _count_node(c, counts)


def _assign(
    node: _Node, parent_alias: str | None, fact: str, counts: dict[str, int], used: set[str]
) -> None:
    if node.dataset == fact:
        alias = "source"
    else:
        if counts[node.dataset] == 1:
            base = node.dataset
        elif parent_alias is not None and parent_alias != "source":
            base = f"{parent_alias}_{node.dataset}"
        else:
            base = node.dataset
        alias = base
        n = 2
        while alias in used:
            alias = f"{base}_{n}"
            n += 1
    node.alias = alias
    used.add(alias)
    for c in node.children:
        _assign(c, alias, fact, counts, used)


def _pick_fact(
    model_name: str,
    datasets: dict[str, dict[Any, Any]],
    relationships: list[dict[Any, Any]],
    fact_hint: str | None,
) -> str:
    if fact_hint is not None:
        if fact_hint not in datasets:
            raise ConversionError(
                f"Model '{model_name}': requested source '{fact_hint}' is not a dataset"
            )
        return fact_hint
    if len(datasets) > 1 and not relationships:
        raise ConversionError(
            f"Model '{model_name}': {len(datasets)} datasets but no relationships; "
            "cannot determine the fact table."
        )
    incoming: dict[str | None, int] = {n: 0 for n in datasets}
    for rel in relationships:
        to = jstr(rel.get("to"))
        incoming[to] = incoming.get(to, 0) + 1
    roots = [n for n, c in incoming.items() if c == 0]
    if not roots:
        raise ConversionError(
            f"Model '{model_name}': join graph contains a cycle (no root dataset). "
            "A Metric View requires an acyclic, tree-shaped graph."
        )
    if len(roots) > 1:
        raise ConversionError(
            f"Model '{model_name}': multiple candidate fact datasets {jlist(sorted(roots))}. "  # type: ignore[type-var]
            "Name the grain with --source."
        )
    return roots[0]  # type: ignore[return-value]


def _mark_otm(model_name: str, root: _Node) -> None:
    """Marks each joined node with its branch's cardinality and rejects a mixed branch.

    A Metric View requires every join within one top-level branch to share a single cardinality
    (an absent `cardinality` reads as `many_to_one`), so a mixed branch is rejected in either
    direction. Emitting one would produce a view the engine refuses at CREATE.
    """
    # Each top-level join starts a branch and sets that branch's expected cardinality.
    for top in root.children:
        branch_is_otm = not top.parent_is_from
        top.is_otm = branch_is_otm
        _mark_otm_visit(model_name, top, branch_is_otm)


def _mark_otm_visit(model_name: str, node: _Node, branch_is_otm: bool) -> None:
    for child in node.children:
        is_otm = not child.parent_is_from
        if is_otm != branch_is_otm:
            raise ConversionError(
                f"Model '{model_name}': join '{child.alias}' is {_cardinality_name(is_otm)} but "
                f"descends from a {_cardinality_name(branch_is_otm)} join; a Metric View requires "
                "every join within one top-level branch to share the same cardinality."
            )
        child.is_otm = branch_is_otm
        _mark_otm_visit(model_name, child, branch_is_otm)


def _cardinality_name(is_otm: bool) -> str:
    return CARD_ONE_TO_MANY if is_otm else CARD_MANY_TO_ONE


def _reject_directed_cycle(
    model_name: str, datasets: dict[str, dict[Any, Any]], relationships: list[dict[Any, Any]]
) -> None:
    """Rejects a directed cycle in the relationship graph.

    Checked on the DIRECTED graph, for an edge back to a dataset on the current DFS stack: a
    dataset reachable by two distinct paths (a diamond) is directed-acyclic and legitimately
    supported via the fan-out aliases. Only a genuine directed cycle is an error.
    """
    out: dict[str, list[str]] = {n: [] for n in datasets}
    for rel in relationships:
        out[jstr(rel.get("from"))].append(jstr(rel.get("to")))  # type: ignore[index,arg-type]
    done: set[str] = set()
    on_stack: dict[str, None] = {}
    for n in datasets:
        cycle = _find_cycle(n, out, done, on_stack)
        if cycle is not None:
            raise ConversionError(
                f"Model '{model_name}': relationships form a directed cycle "
                f"{' -> '.join(cycle)}; a Metric View join graph must be acyclic."
            )


def _find_cycle(
    node: str, out: dict[str, list[str]], done: set[str], on_stack: dict[str, None]
) -> list[str] | None:
    """The cycle path (closing dataset repeated at the end), or None if this subtree is clean."""
    if node in done:
        return None
    if node in on_stack:
        # Back-edge: report from the first occurrence of `node` so the message shows the cycle.
        stack = list(on_stack)
        cycle = stack[stack.index(node) :]
        cycle.append(node)
        return cycle
    on_stack[node] = None
    for nxt in out[node]:
        cycle = _find_cycle(nxt, out, done, on_stack)
        if cycle is not None:
            return cycle
    del on_stack[node]
    done.add(node)
    return None


def _qualify_measure(
    expr: str, dataset_head: re.Pattern[str] | None, dataset_alias_path: dict[str, str]
) -> str:
    """Rewrites the ``<dataset>.`` qualifier of a measure to the alias path from the source.

    A dataset joined directly to the primary already maps to its own alias, so those rewrites are
    no-ops; only a nested dataset actually changes. The whole qualifier run is matched at once and
    resolved from its leaf, so an expression that already carries a full path is re-emitted
    unchanged rather than qualified a second time.
    """
    return rewrite_qualifiers(expr, dataset_head, dataset_alias_path.get)


def _first_diamond_reference(
    expr: str, dataset_head: re.Pattern[str] | None, diamond_datasets: set[str]
) -> str | None:
    """The first dataset a diamond reference names in ``expr``, or None if none."""
    if dataset_head is None or not diamond_datasets:
        return None
    found: list[str | None] = [None]

    def detect(leaf: str) -> str | None:
        if found[0] is None and leaf in diamond_datasets:
            found[0] = leaf
        return None

    rewrite_qualifiers(expr, dataset_head, detect)
    return found[0]


def _sorted_names(names: dict[Any, None]) -> str:
    """Sorted so the error message is deterministic."""
    return jlist(sorted(n for n in names if n is not None))


def _node_order(root: _Node) -> list[tuple[_Node, list[str]]]:
    order: list[tuple[_Node, list[str]]] = []
    _node_order_visit(root, [], order)
    return order


def _node_order_visit(
    node: _Node, path: list[str], order: list[tuple[_Node, list[str]]]
) -> None:
    order.append((node, list(path)))
    for child in node.children:
        _node_order_visit(child, [*path, child.alias], order)


def _build_join(
    node: _Node,
    parent_alias: str,
    datasets: dict[str, dict[Any, Any]],
    notices: Notices,
) -> dict[str, Any]:
    rel = node.rel
    alias = node.alias
    join: dict[str, Any] = {
        "name": alias,
        "source": validate_source(datasets[node.dataset].get("source"), node.dataset),
    }

    stash = read_stash(rel, notices, f"join '{alias}'")
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if "on" in stash:
        # The import stashes the original `on` verbatim when rebuilding it from the equi-join's
        # from/to columns would not reproduce it. Restore it directly.
        join["on"] = stash["on"]
    else:
        _validate_join_columns(rel, from_cols, to_cols)
        parent_cols = from_cols if node.parent_is_from else to_cols
        child_cols = to_cols if node.parent_is_from else from_cols
        if parent_cols == child_cols:
            join["using"] = list(parent_cols)
        else:
            join["on"] = " AND ".join(
                f"{parent_alias}.{p} = {alias}.{c}"
                for p, c in zip(parent_cols, child_cols, strict=False)
            )
    if "rely" in stash:
        join["rely"] = stash["rely"]
    elif node.parent_is_from and _covers_unique_key(datasets[node.dataset], to_cols):
        join["rely"] = {"at_most_one_match": True}
    if "cardinality" in stash:
        join["cardinality"] = stash["cardinality"]
    elif not node.parent_is_from:
        join["cardinality"] = CARD_ONE_TO_MANY
    nested = [_build_join(c, alias, datasets, notices) for c in node.children]
    if nested:
        join["joins"] = nested
    return join


def _covers_unique_key(dataset: dict[Any, Any], join_cols: list[str]) -> bool:
    cols = set(join_cols)
    keys: list[list[str]] = []
    pk = str_list(dataset.get("primary_key"))
    if pk:
        keys.append(pk)
    for k in as_list(dataset.get("unique_keys")):
        keys.append(str_list(k))
    return any(key and cols.issuperset(key) for key in keys)


def _orient_by_key(
    rel: dict[Any, Any], datasets: dict[str, dict[Any, Any]], notices: Notices
) -> dict[Any, Any]:
    from_cols = str_list(rel.get("from_columns"))
    to_cols = str_list(rel.get("to_columns"))
    if not from_cols or not to_cols:
        return rel
    to_ds = datasets[jstr(rel.get("to"))]  # type: ignore[index]
    to_has_keys = bool(str_list(to_ds.get("primary_key"))) or bool(
        as_list(to_ds.get("unique_keys"))
    )
    from_covers = _covers_unique_key(datasets[jstr(rel.get("from"))], from_cols)  # type: ignore
    scope = f"relationship '{js(rel.get('name'))}'"
    if from_covers and to_has_keys and not _covers_unique_key(to_ds, to_cols):
        notices.warn(
            scope,
            "from/to looks mislabeled (the `from` columns are a declared key, the `to` columns "
            "are not); re-orienting so the key side is the `to`/one side",
        )
        swapped = dict(rel)
        swapped["from"] = rel.get("to")
        swapped["to"] = rel.get("from")
        swapped["from_columns"] = to_cols
        swapped["to_columns"] = from_cols
        return swapped
    if from_covers and not to_has_keys:
        notices.warn(
            scope,
            "the `from` columns are a declared key but the `to` side declares none, so from/to "
            "orientation can't be verified; using it as-is -- check the join direction if the "
            "resulting cardinality looks inverted",
        )
    return rel


def _validate_join_columns(
    rel: dict[Any, Any], from_cols: list[str], to_cols: list[str]
) -> None:
    name = js(rel.get("name"))
    if not from_cols or not to_cols:
        raise ConversionError(
            f"Relationship '{name}': from_columns and to_columns are required"
        )
    if len(from_cols) != len(to_cols):
        raise ConversionError(
            f"Relationship '{name}': from_columns ({len(from_cols)}) and to_columns "
            f"({len(to_cols)}) must have the same length"
        )


def _single_physical_primary_key(
    dataset: dict[Any, Any], fields: list[dict[Any, Any]]
) -> str | None:
    primary = str_list(dataset.get("primary_key"))
    if len(primary) != 1 or not is_simple_identifier(primary[0]):
        return None
    key = primary[0]
    for field in fields:
        if key == field.get("name") and key == pick_expression(
            field.get("expression"), f"field '{key}'"
        ):
            return key
    return None


def _convert_field(
    field: dict[Any, Any],
    name0: str,
    qualifier: str,
    is_fact: bool,
    prefix: str | None,
    notices: Notices,
) -> dict[str, Any] | None:
    scope = f"field '{name0}'"
    expr = pick_expression(field.get("expression"), scope)
    if expr is None:
        notices.warn(
            scope,
            "no usable expression.sql or DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 entry; dropping field",
        )
        return None
    if not is_fact:
        qualified = (
            f"{qualifier}.{expr}"
            if is_simple_identifier(expr)
            else expression_dialect.qualify_field_columns(expr, qualifier)
        )
        if qualified is not None:
            expr = qualified
        elif prefix is not None:
            notices.warn(
                scope,
                "complex expression on a fanned-out (diamond) join cannot be "
                "unambiguously qualified; dropped",
            )
            return None
        else:
            notices.warn(
                scope, "complex expression on a joined table; emitted as-is, verify qualification"
            )
    name = f"{prefix}_{name0}" if prefix is not None else name0
    dim: dict[str, Any] = {"name": name, "expr": expr}
    comment = merge_description(field.get("description"), field.get("ai_context"))
    if truthy(comment):
        dim["comment"] = comment
    label = jstr(field.get("label"))
    if truthy(label):
        dim["display_name"] = label
    syns = synonyms_of(field.get("ai_context"))
    if syns:
        dim["synonyms"] = _truncate_synonyms(syns, scope, notices)
    stash = read_stash(field, notices, scope)
    if "format" in stash:
        dim["format"] = stash["format"]
    _warn_dropped_field(field, scope, notices)
    return dim


def _convert_metric(
    metric: dict[Any, Any],
    fact_qualifier: re.Pattern[str],
    dataset_head: re.Pattern[str] | None,
    dataset_alias_path: dict[str, str],
    diamond_datasets: set[str],
    integer_sum_keys: dict[str, str],
    seen_names: set[str],
    notices: Notices,
) -> dict[str, Any] | None:
    name = require_str(metric, "name", "metric")
    scope = f"metric '{name}'"
    if name.lower() in seen_names:
        raise ConversionError(
            f"metric '{name}' collides with another dimension/measure; "
            "Metric Views require unique dimension/measure names -- rename before use"
        )
    seen_names.add(name.lower())
    expr = pick_expression(metric.get("expression"), scope)
    if expr is None:
        notices.warn(
            scope,
            "no usable expression.sql or DATABRICKS/ANSI_SQL/OSSIE_SQL_2026 entry; dropping metric",
        )
        return None
    diamond = _first_diamond_reference(expr, dataset_head, diamond_datasets)
    if diamond is not None:
        notices.warn(
            scope,
            f"expression references dataset '{diamond}', reached by more than one join path "
            "(diamond); cannot be unambiguously qualified; dropped",
        )
        return None
    # Re-qualify a joined dataset's columns with the alias path that addresses them from the
    # primary source. A Metric View addresses a nested join column by its full path, so a bare
    # nested alias at the head is read as struct access on a parameter rather than as a join column.
    expr = _qualify_measure(expr, dataset_head, dataset_alias_path)
    # Strip a `<fact>.` qualifier so fact columns are bare in measures (the Metric View idiom),
    # only outside string literals / comments.
    expr = replace_outside_literals(expr, fact_qualifier, "")
    if not _has_databricks_expression(metric):
        grain = cube_integer_sum_metadata(metric)
        grain_datasets = str_list(grain.get("integer_sum_grain"))
        if grain_datasets:
            grain_paths: dict[str, None] = {}
            for dataset in grain_datasets:
                path = dataset_alias_path.get(dataset)
                if path is not None:
                    grain_paths[path] = None
            keys = dict(integer_sum_keys)
            for value, key in as_map(grain.get("integer_sum_keys")).items():
                if isinstance(key, str):
                    keys[_qualify_measure(value, dataset_head, dataset_alias_path)] = (
                        _qualify_measure(key, dataset_head, dataset_alias_path)
                    )
            encoded = (
                joined_integer_sum.encode(expr, keys, grain_paths.keys())
                if len(grain_paths) == len(grain_datasets)
                else None
            )
            if encoded is None:
                notices.warn(
                    scope,
                    f"cannot preserve the entire Cube aggregate grain for {jlist(grain_datasets)}; "
                    "leaving the expression unchanged",
                )
            else:
                expr = encoded
                notices.warn(
                    scope,
                    f"preserving Cube aggregate grain for {jlist(grain_datasets)} collects every "
                    "distinct joined key per group; memory grows with the key count",
                )
    measure: dict[str, Any] = {"name": name, "expr": expr}
    comment = merge_description(metric.get("description"), metric.get("ai_context"))
    if truthy(comment):
        measure["comment"] = comment
    syns = synonyms_of(metric.get("ai_context"))
    if syns:
        measure["synonyms"] = _truncate_synonyms(syns, scope, notices)
    stash = read_stash(metric, notices, scope)
    # The Ossie Metric schema has no `label`, so a measure's display_name round-trips through the
    # DATABRICKS stash rather than a native field.
    for key in ("format", "window", "partition", "display_name"):
        if key in stash:
            measure[key] = stash[key]
    _warn_dropped_column(metric, scope, notices)
    return measure


def _has_databricks_expression(metric: dict[Any, Any]) -> bool:
    expression = as_map(metric.get("expression"))
    for item in as_list(expression.get("dialects")):
        variant = as_map(item)
        if variant.get("dialect") == DIALECT_DATABRICKS and truthy(variant.get("expression")):
            return True
    return False


class _CascadeDropper:
    """Propagates dropped field/metric references to the columns that use them.

    The worklists preserve the phase order: dimensions then measures, both in source order, with a
    dependency discovered after its source position deferred to the next round. This keeps both
    output and notice ordering stable.
    """

    SEED_PHASE = 0
    DIMENSION_PHASE = 1
    MEASURE_PHASE = 2

    def __init__(
        self,
        dimensions: list[dict[str, Any]],
        measures: list[dict[str, Any]],
        dropped_dims: dict[str, None],
        dropped_measures: dict[str | None, None],
        notices: Notices,
    ) -> None:
        self.dimensions = dimensions
        self.measures = measures
        self.dropped_dims = dropped_dims
        self.dropped_measures = dropped_measures
        self.notices = notices
        self.removed_dims: set[int] = set()
        self.removed_measures: set[int] = set()
        self.dimension_patterns: dict[str, re.Pattern[str]] = {}
        self.measure_patterns: dict[str, re.Pattern[str]] = {}
        self.dims_now: set[int] = set()
        self.dims_next: set[int] = set()
        self.measures_now: set[int] = set()
        self.measures_next: set[int] = set()

    def run(self) -> None:
        for name in list(self.dropped_measures):
            self._propagate(name, True, self.SEED_PHASE, -1)
        for name in list(self.dropped_dims):
            self._propagate(name, False, self.SEED_PHASE, -1)

        while self.dims_now or self.measures_now:
            self._process(False)
            self._process(True)
            self.dims_now, self.dims_next = self.dims_next, set()
            self.measures_now, self.measures_next = self.measures_next, set()

        self.dimensions[:] = [
            c for i, c in enumerate(self.dimensions) if i not in self.removed_dims
        ]
        self.measures[:] = [
            c for i, c in enumerate(self.measures) if i not in self.removed_measures
        ]

    def _process(self, measure: bool) -> None:
        # Ascending, like BitSet.nextSetBit: an index scheduled above the current one during this
        # pass is processed in the same pass.
        phase = self.MEASURE_PHASE if measure else self.DIMENSION_PHASE
        index = 0
        while True:
            now = self.measures_now if measure else self.dims_now
            pending = [i for i in now if i >= index]
            if not pending:
                return
            index = min(pending)
            now.discard(index)
            self._drop(index, measure, phase)
            index += 1

    def _drop(self, index: int, measure: bool, phase: int) -> None:
        removed = self.removed_measures if measure else self.removed_dims
        if index in removed:
            return
        column = (self.measures if measure else self.dimensions)[index]
        name = column["name"]
        reference = self._references_dropped(column["expr"], name, measure)
        if reference is None:
            return
        removed.add(index)
        kind = "measure" if measure else "dimension"
        self.notices.warn(
            f"{kind} '{name}'",
            f"references dropped '{reference}'; dropping (downstream of a dropped field/metric)",
        )
        dropped: dict[Any, None] = self.dropped_measures if measure else self.dropped_dims
        if name not in dropped:
            dropped[name] = None
            self._propagate(name, measure, phase, index)

    def _propagate(
        self, name: str | None, measure_reference: bool, phase: int, source_index: int
    ) -> None:
        if name is None:
            return
        pattern = self._reference_pattern(name, measure_reference)
        for index, column in enumerate(self.dimensions):
            if not self._is_pending(index, False) and _matches(
                column, name, measure_reference, False, pattern
            ):
                self._schedule(index, False, phase, source_index)
        for index, column in enumerate(self.measures):
            if not self._is_pending(index, True) and _matches(
                column, name, measure_reference, True, pattern
            ):
                self._schedule(index, True, phase, source_index)

    def _is_pending(self, index: int, measure: bool) -> bool:
        if measure:
            return (
                index in self.removed_measures
                or index in self.measures_now
                or index in self.measures_next
            )
        return index in self.removed_dims or index in self.dims_now or index in self.dims_next

    def _schedule(self, index: int, measure: bool, phase: int, source_index: int) -> None:
        if measure:
            if phase != self.MEASURE_PHASE or index > source_index:
                self.measures_now.add(index)
            else:
                self.measures_next.add(index)
        elif phase == self.SEED_PHASE or (
            phase == self.DIMENSION_PHASE and index > source_index
        ):
            self.dims_now.add(index)
        else:
            self.dims_next.add(index)

    def _references_dropped(
        self, expr: str, self_name: str, self_is_measure: bool
    ) -> str | None:
        # Databricks SQL identifiers are case-insensitive, so match a dropped name against the
        # expression case-insensitively: both the pre-filter here and the regex.
        lower_expr = expr.lower()
        for name in self.dropped_measures:
            if name is None or _is_self_reference(self_is_measure, True, self_name, name):
                continue
            if name.lower() in lower_expr and find_outside_literals(
                expr, self._reference_pattern(name, True)
            ):
                return name
        for name in self.dropped_dims:
            if name is None or _is_self_reference(self_is_measure, False, self_name, name):
                continue
            if name.lower() in lower_expr and find_outside_literals(
                expr, self._reference_pattern(name, False)
            ):
                return name
        return None

    def _reference_pattern(self, name: str, measure: bool) -> re.Pattern[str]:
        patterns = self.measure_patterns if measure else self.dimension_patterns
        pattern = patterns.get(name)
        if pattern is None:
            # A dimension is referenced as a bare, unqualified token. The trailing (?!\s*\()
            # excludes a same-spelled function/keyword call such as YEAR(...).
            source = (
                r"measure\(\s*" + re.escape(name) + r"\s*\)"
                if measure
                else r"(?<![\w.])" + re.escape(name) + r"(?![\w.])(?!\s*\()"
            )
            pattern = re.compile(source, re.IGNORECASE | re.ASCII)
            patterns[name] = pattern
        return pattern


def _is_self_reference(
    column_is_measure: bool, dropped_is_measure: bool, column_name: str, dropped_name: str
) -> bool:
    """A column is a self-reference to a dropped name only within its own kind.

    Compared case-insensitively (Databricks SQL identifiers are case-insensitive). A column that
    merely shares a dropped column's name across kinds is a different object, so it is still
    matched and cascade-dropped.
    """
    return column_is_measure == dropped_is_measure and _equals_ignore_case(
        dropped_name, column_name
    )


def _matches(
    column: dict[str, Any],
    reference: str,
    measure_reference: bool,
    column_is_measure: bool,
    pattern: re.Pattern[str],
) -> bool:
    name = column["name"]
    if _is_self_reference(column_is_measure, measure_reference, name, reference):
        return False
    expr = column["expr"]
    return reference.lower() in expr.lower() and find_outside_literals(expr, pattern)


def _truncate_synonyms(syns: list[str], scope: str, notices: Notices) -> list[str]:
    if len(syns) > SYNONYM_LIMIT:
        notices.warn(
            scope,
            f"{len(syns)} synonyms exceeds Metric View limit; keeping first {SYNONYM_LIMIT}",
        )
        return syns[:SYNONYM_LIMIT]
    return syns


def _warn_dropped_model(model: dict[Any, Any], notices: Notices) -> None:
    if foreign_vendor_extensions(model):
        notices.warn("model", "foreign-vendor custom_extensions dropped")
    if truthy(model.get("ai_context")):
        notices.warn(
            "model",
            "model-level ai_context dropped (only the description maps to the view comment)",
        )
    for ds_obj in as_list(model.get("datasets")):
        ds = as_map(ds_obj)
        scope = f"dataset '{js(ds.get('name'))}'"
        if str_list(ds.get("primary_key")) or as_list(ds.get("unique_keys")):
            notices.warn(
                scope,
                "primary_key/unique_keys not stored as columns; used to set "
                "rely.at_most_one_match on a matching many_to_one join where applicable",
            )
        if isinstance(ds.get("ai_context"), dict) and ds.get("ai_context"):
            notices.warn(scope, "dataset-level ai_context (object) dropped")
        if truthy(ds.get("description")):
            notices.warn(scope, "dataset-level description dropped (no per-source comment field)")
        if foreign_vendor_extensions(ds):
            notices.warn(scope, "foreign-vendor custom_extensions dropped")
    for rel_obj in as_list(model.get("relationships")):
        rel = as_map(rel_obj)
        rn = js(rel.get("name")) if "name" in rel else "<unnamed>"
        if truthy(rel.get("ai_context")):
            notices.warn(f"relationship '{rn}'", "relationship ai_context dropped")
        if foreign_vendor_extensions(rel):
            notices.warn(f"relationship '{rn}'", "foreign-vendor custom_extensions dropped")


def _warn_dropped_field(field: dict[Any, Any], scope: str, notices: Notices) -> None:
    dim = field.get("dimension")
    if isinstance(dim, dict) and "is_time" in dim:
        notices.warn(scope, "dimension.is_time has no Metric View counterpart; dropped")
    _warn_dropped_column(field, scope, notices)


def _warn_dropped_column(column: dict[Any, Any], scope: str, notices: Notices) -> None:
    """Notices shared by a field and a metric.

    `datatype`, the members of an `ai_context` OBJECT that have no Metric View slot, and
    foreign-vendor extensions. The string form of `ai_context` maps to the column comment and the
    object's `synonyms` maps to the column's synonyms, but every other object member --
    `instructions`, `examples` -- has nowhere to go.
    """
    if column.get("datatype") is not None:
        notices.warn(scope, "datatype has no Metric View counterpart; dropped")
    ai_context = column.get("ai_context")
    if isinstance(ai_context, dict):
        dropped = sorted(js(k) for k in ai_context if k != "synonyms")
        if dropped:
            notices.warn(
                scope,
                f"ai_context {jlist(dropped)} dropped (only 'synonyms' has a Metric View "
                "counterpart)",
            )
    if foreign_vendor_extensions(column):
        notices.warn(scope, "foreign-vendor custom_extensions dropped")


__all__ = ["OSSIE_VERSION", "convert_ossie_to_metric_view"]
