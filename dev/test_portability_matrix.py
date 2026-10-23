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

"""Tests for dev/portability_matrix.py (run with: python -m pytest dev/)."""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "portability_matrix", Path(__file__).with_name("portability_matrix.py"))
assert _SPEC is not None and _SPEC.loader is not None
pm = importlib.util.module_from_spec(_SPEC)
sys.modules["portability_matrix"] = pm  # dataclasses resolve their module through sys.modules
_SPEC.loader.exec_module(pm)

REPO = Path(__file__).resolve().parents[1]


def test_manifest_variants_are_read(tmp_path):
    (tmp_path / "manifest.yaml").write_text(
        "rungs:\n"
        "  - id: M0\n"
        "    variants:\n"
        "      - {id: M0.ansi,  file: m0/a.ossie.yaml, tag: ANSI_SQL, role: primary}\n"
        "      - {id: M0.bare,  file: m0/b.ossie.yaml, tag: ANSI_SQL, role: variant}\n")
    assert pm.load_fixtures(tmp_path) == [
        ("M0.ansi", tmp_path / "m0/a.ossie.yaml"),
        ("M0.bare", tmp_path / "m0/b.ossie.yaml"),
    ]


@pytest.mark.parametrize("converter", pm.CONVERTERS, ids=lambda c: c.name)
def test_every_converter_script_is_a_real_console_script(converter):
    module, function = pm.entry_point(REPO / converter.directory, converter.script)
    assert module and function


def test_round_trippable_and_listed_converters_do_not_overlap():
    names = {c.directory for c in pm.CONVERTERS}
    assert not names & set(pm.NOT_ROUND_TRIPPABLE)


def test_tally_counts_metrics_only_and_splits_rewritten():
    reports = [
        {"findings": [
            {"kind": "metric", "outcome": "PORTABLE"},
            {"kind": "metric", "outcome": "RELABELED"},
            {"kind": "metric", "outcome": "REWRITTEN",
             "detail": "cosmetic (normalized trees equal)"},
            {"kind": "metric", "outcome": "REWRITTEN", "detail": "review: normalized trees differ"},
            {"kind": "metric", "outcome": "SURVIVED_VENDOR_ONLY"},
            {"kind": "metric", "outcome": "PLACEHOLDER"},
            {"kind": "metric", "outcome": "DROPPED_SILENT"},
            {"kind": "field", "outcome": "SURVIVED_VENDOR_ONLY"},
        ]},
        {"rejected": True},
    ]
    counts = pm.tally(reports)
    assert counts == {"metrics": 7, "portable": 3, "review": 1, "vendor": 1,
                      "placeholder": 1, "dropped": 1, "rejected": 1}


def test_missing_fixtures_explain_the_submodule(tmp_path):
    with pytest.raises(SystemExit, match="git submodule update --remote ossie"):
        pm.main(["--fixtures", str(tmp_path)])
