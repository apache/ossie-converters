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

"""
Converts LookML (Looker's semantic modeling language) to an Ossie YAML semantic
model. Pure offline conversion: no Looker or BigQuery connection required.

Input is a LookML project as a mapping of {relative filename: LookML string},
so views and explores can live in separate files, as they do in Looker.

Mapping summary:
    LookML view           -> Ossie dataset (sql_table_name or derived_table sql
                             becomes the dataset source)
"""

import warnings

import lkml
import yaml

from .ossie_to_lookml import SUPPORTED_VERSION, OssieConversionError


DEFAULT_MODEL_NAME = "lookml_model"


def _warn(scope, msg):
    warnings.warn(f"[{scope}] {msg}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def convert_lookml_to_ossie(files, model_name=DEFAULT_MODEL_NAME):
    """Top-level entry point. Parses LookML files, converts them, returns an
    Ossie document as a YAML string.

    Args:
        files: {relative filename: LookML string}.
        model_name: Name of the Ossie semantic model.

    Returns:
        Ossie YAML string.

    Raises:
        OssieConversionError: If the input cannot be converted.
    """
    if not isinstance(files, dict) or not files:
        raise OssieConversionError("Expected a non-empty mapping of {filename: LookML}")

    views = _load_views(files)
    datasets = [ds for ds in (_convert_view(v) for v in views) if ds is not None]

    model = {
        "version": SUPPORTED_VERSION,
        "name": model_name,
        "datasets": datasets,
    }
    return yaml.safe_dump(model, sort_keys=False)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _load_views(files):
    """Parses every file and returns its views, rejecting duplicate names."""
    views = {}
    for fname, text in files.items():
        try:
            parsed = lkml.load(text)
        except SyntaxError as e:
            raise OssieConversionError(f"File '{fname}': invalid LookML: {e}")
        for view in parsed.get("views", []):
            name = view["name"]
            if name.startswith("+"):
                _warn(f"view '{name}'", "refinements are not supported yet; skipping")
                continue
            if name in views:
                raise OssieConversionError(
                    f"View '{name}' is defined in both '{views[name][0]}' and '{fname}'"
                )
            views[name] = (fname, view)
    return [view for _, view in views.values()]


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def _convert_view(view):
    """Converts one LookML view to an Ossie dataset, or None if it has no
    SQL source Ossie can express."""
    name = view["name"]
    source = _convert_source(view)
    if source is None:
        return None
    return {"name": name, "source": source}


def _convert_source(view):
    """Returns the Ossie `source` for a view: its table name, or the SQL of its
    derived table.

    A view with neither uses its own name as the table, as Looker does.
    Native derived tables (`explore_source`) are built from an explore, not
    SQL, so they have no Ossie source; the view is skipped with a warning.
    """
    name = view["name"]
    derived = view.get("derived_table")
    if derived is not None:
        if "sql" in derived:
            return derived["sql"].strip()
        _warn(f"view '{name}'", "native derived tables (explore_source) have no "
              "Ossie source; skipping")
        return None

    table = view.get("sql_table_name", name).strip()
    # Backticks are BigQuery quoting, not part of the name.
    return table.replace("`", "")
