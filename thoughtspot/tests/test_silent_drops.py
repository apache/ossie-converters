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
    """`is_time` is a role, deliberately independent of `datatype`."""

    def _log(self, is_time):
        return _build(_model([_dataset(
            "ORDERS", [],
            fields=[_field("ORDER_DATE", "ORDERS", dimension={"is_time": is_time})],
        )]), [_table("ORDERS", ["ORDER_DATE"])])

    def test_the_drop_is_reported(self):
        [issue] = _issues(self._log(True), "TS-FIELD-IS-TIME-DROPPED")
        assert issue["severity"] == Severity.INFO.value
        assert issue["object_ref"] == "field:ORDER_DATE"

    def test_is_time_false_asserts_nothing_so_nothing_is_lost(self):
        assert _issues(self._log(False), "TS-FIELD-IS-TIME-DROPPED") == []

    def test_a_field_with_no_dimension_block_is_quiet(self):
        log = _build(_model([_dataset("ORDERS", ["AMOUNT"])]))
        assert _issues(log, "TS-FIELD-IS-TIME-DROPPED") == []


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

    def _convert(self, *extension_owners, on_field=False):
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
            dataset["fields"][0]["custom_extensions"] = extensions
        else:
            dataset["custom_extensions"] = extensions
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

    def test_our_own_entry_is_not_reported_as_a_loss(self):
        assert _issues(self._convert("THOUGHTSPOT"), "TS-VENDOR-EXTENSIONS-DROPPED") == []

    def test_a_document_with_no_extensions_is_quiet(self):
        assert _issues(convert(_model()).issues, "TS-VENDOR-EXTENSIONS-DROPPED") == []
