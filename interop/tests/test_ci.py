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

"""CI publication must preserve converter findings and infrastructure failures."""

import json
from pathlib import Path
import shlex

import pytest
import yaml

from interop.ci_summary import main, render
from interop.manifest import load_manifest
from interop.runner import exit_code

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def results():
    return {
        "schema_version": 1,
        "metadata": {"converters": ["alpha", "beta"]},
        "preparations": {"alpha": {"grade": "OK"}, "beta": {"grade": "FAIL"}},
        "sources": [{"grade": "SKIP"}],
        "cases": [
            {"tier": "A", "grade": "EMPTY"},
            {"tier": "A", "grade": "FAIL"},
            {"tier": "B", "grade": "LOSSY"},
        ],
        "regressions": [{"grade": "FAIL"}],
        "document_regressions": [{"grade": "OK"}],
        "harness_errors": [],
    }


def test_summary_retains_findings_without_enabling_strict_mode(tmp_path, results):
    (tmp_path / "results.json").write_text(json.dumps(results))
    summary = tmp_path / "summary.md"
    summary.write_text("Existing summary\n")
    url = "https://github.com/apache/ossie-converters/actions/runs/123/artifacts/456"
    assert main([str(tmp_path), "--summary", str(summary), "--artifact-url", url]) == 0
    text = summary.read_text()
    assert text.startswith("Existing summary\n")
    assert "report-only" in text and url in text
    assert "| Tier A exports | 0 | 0 | 1 | 1 | 0 |" in text
    assert "| Tier B returns | 0 | 1 | 0 | 0 | 0 |" in text
    assert "| Preparation | 1 | 0 | 0 | 1 | 0 |" in text
    assert "Harness errors: 0" in text
    assert exit_code(results) == 0
    assert exit_code(results, strict=True) == 1


def test_harness_failure_stays_blocking_even_when_summary_publishes(tmp_path, results):
    results["harness_errors"] = ["validator crashed | unexpected\nstate"]
    (tmp_path / "results.json").write_text(json.dumps(results))
    summary = tmp_path / "summary.md"
    assert main([str(tmp_path), "--summary", str(summary)]) == 0
    assert "Harness errors: 1" in summary.read_text()
    assert "validator crashed \\| unexpected state" in summary.read_text()
    assert exit_code(results) == 2


@pytest.mark.parametrize(
    "contents", [None, "{broken", "{}", "[]", '{"schema_version": 2}']
)
def test_missing_or_invalid_results_write_fallback_and_fail_publication(
    tmp_path, contents
):
    if contents is not None:
        (tmp_path / "results.json").write_text(contents)
    summary = tmp_path / "summary.md"
    assert main([str(tmp_path), "--summary", str(summary)]) == 2
    assert "report is missing or invalid" in summary.read_text()


def test_unknown_result_grade_is_not_silently_omitted(results):
    results["regressions"] = [{"grade": "SURPRISE"}]
    with pytest.raises(ValueError, match="unknown result grade"):
        render(results)


def test_summary_without_download_link_names_upload_step(results):
    text = render(results)
    assert "check the artifact upload step" in text
    assert (
        "](report.md)" not in text
    )  # Artifact-relative links do not resolve in Actions summaries.


def test_unwritable_summary_destination_is_publication_error(tmp_path, results):
    (tmp_path / "results.json").write_text(json.dumps(results))
    assert main([str(tmp_path), "--summary", str(tmp_path)]) == 2


def test_ci_policy_retains_exit_codes_and_publishes_after_failure():
    # BaseLoader keeps YAML's `on` key and boolean values as strings.
    workflow = yaml.load(
        (ROOT / ".github/workflows/interop-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in workflow["on"]
    assert "workflow_dispatch" in workflow["on"]
    projects = {
        converter.cwd.relative_to(ROOT).parts[0] + "/**"
        for converter in load_manifest(
            ROOT / "interop/manifest.yaml", ROOT
        ).converters.values()
    }
    for event in ("push", "pull_request"):
        assert projects <= set(workflow["on"][event]["paths"])
        assert "ossie" in workflow["on"][event]["paths"]
        assert "*/pyproject.toml" in workflow["on"][event]["paths"]
    job = workflow["jobs"]["interop"]
    assert "continue-on-error" not in job
    assert all("continue-on-error" not in step for step in job["steps"])
    runner = next(step for step in job["steps"] if step.get("id") == "matrix")
    command = shlex.split(runner["run"])
    assert "--strict" not in command and "--no-prepare" not in command
    assert not set(command) & {"||", "&&", ";", "true"}
    upload = next(step for step in job["steps"] if step.get("id") == "evidence")
    assert upload["if"] == "${{ always() }}"
    assert upload["with"]["path"] == "interop/.runs/ci/"
    assert upload["with"]["include-hidden-files"] == "true"
    assert upload["with"]["if-no-files-found"] == "error"
    summary = next(
        step for step in job["steps"] if "interop.ci_summary" in step.get("run", "")
    )
    assert summary["if"] == "${{ always() }}"
