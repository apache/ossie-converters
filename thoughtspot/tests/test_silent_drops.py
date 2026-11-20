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

"""Four things this converter dropped without saying so.

The converter's standing rule is that nothing is discarded in silence: a
construct ThoughtSpot cannot receive is reported, with the object it was on,
so an author can see what did not arrive. Four parts of the core
specification were being read past instead.

None of these is a translation this converter could have performed -- each
genuinely has no home in TML, and the fix here is the issue, not a mapping.
What makes them worth fixing is that an author converting a dbt, Databricks
or Snowflake document had no way to learn the information was gone.

* `Dataset.metrics` -- a dataset-scoped metric. Only `SemanticModel.metrics`
  is read, so every metric declared on a dataset vanished. This one is a
  measure disappearing from a model, which is why it is a WARNING rather
  than a note.
* `Field.dimension.is_time` -- the spec's temporal-role flag, independent of
  the field's datatype. ThoughtSpot derives the role from the column's own
  type and has no separate flag, so an author who set it on something other
  than a date column loses the distinction.
* `Relationship.ai_context` -- the sibling of the dataset, field, metric and
  model `ai_context`, each of which this converter already reports. A
  ThoughtSpot join carries no description, synonyms or instructions at all.
* Another vendor's `custom_extensions` entry. The forward leg deliberately
  preserves foreign entries alongside its own (`stash.write_stash`); the
  return leg has nowhere in TML to put them, so a document that came from
  another converter arrives shorn of whatever that converter stashed for
  itself.
"""
import json

import pytest

from ossie_thoughtspot.issues import IssueLog, Severity
from ossie_thoughtspot.ossie_to_thoughtspot import build_model, convert
from ossie_thoughtspot.tml import TmlDocument


def _table(name, columns):
    return TmlDocument(
        kind="table",
        body={
            "name": name, "db": "SALES", "schema": "PUBLIC", "db_table": name,
            "connection": {"name": "My Snowflake"},
            "columns": [{"name": c, "db_column_name": c,
                         "db_column_properties": {"data_type": "INT64"}} for c in columns],
        },
        guid=None,
    )


def _field(name, dataset, **extra):
    field = {"name": name, "expression": {"dialects": [
        {"dialect": "THOUGHTSPOT", "expression": f"[{dataset}::{name}]"},
        {"dialect": "ANSI_SQL", "expression": name},
    ]}}
    field.update(extra)
    return field


def _dataset(name, columns, **extra):
    dataset = {
        "name": name,
        "source": f"SALES.PUBLIC.{name}",
        "fields": [_field(c, name) for c in columns],
    }
    dataset.update(extra)
    return dataset


def _model(datasets=None, **extra):
    model = {"name": "sales", "datasets": datasets or [_dataset("ORDERS", ["AMOUNT"])]}
    model.update(extra)
    return model


def _build(model, tables=None):
    log = IssueLog()
    build_model(model, tables or [_table("ORDERS", ["AMOUNT"])], log)
    return log


def _issues(log, code):
    return [i for i in log.as_dicts() if i.get("code") == code]


class TestADatasetScopedMetricIsReported:
    """Only `SemanticModel.metrics` is read; a dataset's own metrics are not."""

    def _log(self):
        return _build(_model([_dataset(
            "ORDERS", ["AMOUNT"],
            metrics=[{"name": "total_amount", "expression": {"dialects": [
                {"dialect": "ANSI_SQL", "expression": "SUM(AMOUNT)"}]}}],
        )]))

    def test_the_drop_is_reported(self):
        [issue] = _issues(self._log(), "TS-DATASET-METRICS-DROPPED")
        assert issue["severity"] == Severity.WARNING.value

    def test_the_issue_names_the_metric_and_its_dataset(self):
        [issue] = _issues(self._log(), "TS-DATASET-METRICS-DROPPED")
        assert "total_amount" in issue["message"]
        assert issue["object_ref"] == "dataset:ORDERS"

    def test_a_dataset_with_no_metrics_is_quiet(self):
        assert _issues(_build(_model()), "TS-DATASET-METRICS-DROPPED") == []


class TestATemporalRoleFlagIsReported:
    """`is_time` is a ROLE, and the spec defaults it FROM the datatype.

    "When `is_time` is not set explicitly, it defaults to `true` if
    `datatype` is one of Date, Time, DateTime, DateTimeTz, and `false`
    otherwise. Explicit `is_time` always wins." ThoughtSpot derives exactly
    that default from the column's own type and has no flag to override it,
    so what is lost is not `is_time` -- it is any DISAGREEMENT between the
    resolved role and the one the datatype implies.

    That cuts both ways, and the first version of this reported only half
    of it:

    * `is_time: true` on a non-temporal datatype -- an integer year grain,
      a quarter-name string -- says "put this on the time axis" and
      ThoughtSpot cannot be told.
    * `is_time: false` on a temporal datatype -- the spec's own example is
      an audit `created_at` -- says "keep this OFF the time axis", and
      ThoughtSpot will put it there anyway. Reporting only the `true` case
      missed this one entirely.
    * Either flag AGREEING with its datatype is redundant, and reporting a
      loss there names one that did not happen.
    """

    def _log(self, is_time, datatype="Integer"):
        field = _field("ORDER_DATE", "ORDERS", dimension={"is_time": is_time})
        field["datatype"] = datatype
        return _build(_model([_dataset("ORDERS", [], fields=[field])]),
                      [_table("ORDERS", ["ORDER_DATE"])])

    def _reported(self, is_time, datatype="Integer"):
        return _issues(self._log(is_time, datatype), "TS-FIELD-IS-TIME-DROPPED")

    def test_a_time_role_on_a_non_temporal_type_is_reported(self):
        [issue] = self._reported(True, "Integer")
        assert issue["severity"] == Severity.INFO.value
        assert issue["object_ref"] == "field:ORDER_DATE"

    def test_opting_a_temporal_column_out_is_reported_too(self):
        [issue] = self._reported(False, "DateTime")
        assert "ORDER_DATE" in issue["message"]

    def test_a_time_role_agreeing_with_a_temporal_type_is_quiet(self):
        for datatype in ("Date", "Time", "DateTime", "DateTimeTz"):
            assert self._reported(True, datatype) == [], datatype

    def test_no_time_role_agreeing_with_a_non_temporal_type_is_quiet(self):
        assert self._reported(False, "String") == []

    def test_a_field_with_no_dimension_block_is_quiet(self):
        log = _build(_model([_dataset("ORDERS", ["AMOUNT"])]))
        assert _issues(log, "TS-FIELD-IS-TIME-DROPPED") == []

    def test_an_untyped_field_falls_back_to_non_temporal(self):
        # No datatype is no evidence of a temporal type, so `is_time: true`
        # is still a claim ThoughtSpot cannot carry.
        field = _field("GRAIN", "ORDERS", dimension={"is_time": True})
        log = _build(_model([_dataset("ORDERS", [], fields=[field])]),
                     [_table("ORDERS", ["GRAIN"])])
        assert len(_issues(log, "TS-FIELD-IS-TIME-DROPPED")) == 1


class TestARelationshipsAiContextIsReported:
    """The one `ai_context` scope this converter did not already report."""

    def _log(self, **rel_extra):
        rel = {"name": "orders_to_customers", "from": "ORDERS", "to": "CUSTOMERS",
               "from_columns": ["CUSTOMER_ID"], "to_columns": ["CUSTOMER_ID"]}
        rel.update(rel_extra)
        return _build(
            _model([_dataset("ORDERS", ["CUSTOMER_ID"]),
                    _dataset("CUSTOMERS", ["CUSTOMER_ID"])],
                   relationships=[rel]),
            [_table("ORDERS", ["CUSTOMER_ID"]), _table("CUSTOMERS", ["CUSTOMER_ID"])],
        )

    def test_the_drop_is_reported(self):
        [issue] = _issues(
            self._log(ai_context={"instructions": "join only for fulfilled orders"}),
            "TS-JOIN-AI-CONTEXT-UNSUPPORTED",
        )
        assert issue["severity"] == Severity.WARNING.value
        assert issue["object_ref"] == "relationship:orders_to_customers"

    def test_a_plain_string_ai_context_is_reported_too(self):
        assert len(_issues(self._log(ai_context="the fulfilment join"),
                           "TS-JOIN-AI-CONTEXT-UNSUPPORTED")) == 1

    def test_a_relationship_without_one_is_quiet(self):
        assert _issues(self._log(), "TS-JOIN-AI-CONTEXT-UNSUPPORTED") == []


class TestAnotherVendorsExtensionsAreReported:
    """The forward leg preserves them; the return leg has nowhere to put them."""

    def _convert(self, *extension_owners, on_field=False, field="custom_extensions"):
        # Our own entry has to be a well-formed payload: `stash.read_stash`
        # rejects an unrecognised shape version outright, which is a
        # different fact from the one under test here.
        extensions = [
            {"vendor_name": v,
             "data": json.dumps({"_v": 1} if v == "THOUGHTSPOT" else {"k": 1})}
            for v in extension_owners
        ]
        dataset = _dataset("ORDERS", ["AMOUNT"])
        if on_field:
            dataset["fields"][0][field] = extensions
        else:
            dataset[field] = extensions
        return convert(_model([dataset])).issues

    def test_the_drop_is_reported_once_naming_the_vendor(self):
        [issue] = _issues(self._convert("DATABRICKS"), "TS-VENDOR-EXTENSIONS-DROPPED")
        assert issue["severity"] == Severity.INFO.value
        assert "DATABRICKS" in issue["message"]

    def test_every_vendor_is_named_once_however_many_objects_carry_it(self):
        [issue] = _issues(
            self._convert("DATABRICKS", "SNOWFLAKE"), "TS-VENDOR-EXTENSIONS-DROPPED",
        )
        assert "DATABRICKS" in issue["message"]
        assert "SNOWFLAKE" in issue["message"]

    def test_a_field_scoped_extension_is_found_too(self):
        assert len(_issues(self._convert("DBT", on_field=True),
                           "TS-VENDOR-EXTENSIONS-DROPPED")) == 1

    @pytest.mark.parametrize("on_field", [False, True])
    def test_a_vendor_under_the_renamed_field_is_reported_too(self, on_field):
        # apache/ossie#522's name for the field. `stash.read_stash` reads
        # it, so a document written under it reaches this boundary too.
        [issue] = _issues(
            self._convert("DATABRICKS", on_field=on_field, field="nonportable_extensions"),
            "TS-VENDOR-EXTENSIONS-DROPPED",
        )
        assert "DATABRICKS" in issue["message"]

    def test_a_key_inside_another_vendors_payload_is_not_a_vendor(self):
        # With `data` an object, a foreign payload can hold a key spelled like
        # the field. What is under it is that vendor's own content.
        dataset = _dataset("ORDERS", ["AMOUNT"])
        dataset["nonportable_extensions"] = [{
            "vendor_name": "DATABRICKS",
            "data": {"custom_extensions": [{"vendor_name": "SNOWFLAKE", "data": "{}"}]},
        }]
        [issue] = _issues(convert(_model([dataset])).issues, "TS-VENDOR-EXTENSIONS-DROPPED")
        assert "DATABRICKS" in issue["message"]
        assert "SNOWFLAKE" not in issue["message"]

    def test_our_own_entry_is_not_reported_as_a_loss(self):
        assert _issues(self._convert("THOUGHTSPOT"), "TS-VENDOR-EXTENSIONS-DROPPED") == []

    def test_a_document_with_no_extensions_is_quiet(self):
        assert _issues(convert(_model()).issues, "TS-VENDOR-EXTENSIONS-DROPPED") == []


class TestTheExtensionWalkSurvivesTheDocumentItWalks:
    """A reporting pass must never be the thing that fails the conversion.

    The scan added for TS-VENDOR-EXTENSIONS-DROPPED runs over the whole
    document at the top of every `convert()`. Written as plain recursion it
    turned two documents that previously converted into a bare traceback:
    one carrying a cyclic structure, which a YAML alias can produce and the
    safe loader really does build, and one merely nested deeply. `cli.py`
    catches ConversionError, OSError and UnicodeDecodeError, so a
    RecursionError escapes as a traceback -- breaking the contract
    `stash.py` states and the rest of the module keeps.

    Walked iteratively with a visited set instead: no stack to exhaust, and
    a structure that points at itself is visited once.
    """

    def _cyclic(self):
        model = _model()
        model["datasets"][0]["self"] = model
        return model

    def test_a_cyclic_document_still_converts(self):
        assert convert(self._cyclic()).documents.model is not None

    def test_a_cyclic_document_does_not_hide_a_foreign_vendor(self):
        model = self._cyclic()
        model["datasets"][0]["custom_extensions"] = [
            {"vendor_name": "DATABRICKS", "data": json.dumps({"k": 1})},
        ]
        assert len(_issues(convert(model).issues, "TS-VENDOR-EXTENSIONS-DROPPED")) == 1

    def test_a_deeply_nested_document_still_converts(self):
        model = _model()
        node: dict = {}
        model["datasets"][0]["deep"] = node
        for _ in range(10_000):
            child: dict = {}
            node["next"] = child
            node = child
        assert convert(model).documents.model is not None
