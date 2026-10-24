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

"""Execute an ordered matrix without hiding failed setup or empty conversions."""

from datetime import datetime, timezone
import os
from pathlib import Path
import re
import shutil
import subprocess

from .bindings import check_bindings
from .contracts import check_document, check_native_sources
from .checks import (
    ContentError,
    Validator,
    compare_inventory,
    inventory,
    meaningful_ossie,
    native_content,
)
from .execution import diagnostics, has_degradation, run_command, succeeded
from .manifest import Manifest, expand


def _result(grade="OK", reason=""):
    return {"grade": grade, "reason": reason, "commands": [], "diagnostics": []}


def _import_unavailable(converter):
    if converter.import_skip:
        return converter.import_skip
    missing = [
        str(path) for path in converter.import_requires_files if not path.is_file()
    ]
    return (
        "missing import prerequisite file(s): " + ", ".join(missing)
        if missing
        else None
    )


def _revision(path):
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else None


def _dirty(path):
    result = subprocess.run(
        ["git", "-C", str(path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout) if result.returncode == 0 else None


class Runner:
    def __init__(
        self,
        root: Path,
        manifest: Manifest,
        output: Path,
        *,
        timeout=600,
        prepare=True,
        native=False,
    ):
        self.root, self.manifest, self.output = root, manifest, output
        self.timeout, self.prepare_enabled, self.native = timeout, prepare, native
        self.validator = Validator(root)
        self.report = {
            "schema_version": 1,
            "metadata": {
                "started_at": datetime.now(timezone.utc).isoformat(),
                "repo_commit": _revision(root),
                "ossie_commit": _revision(root / "ossie"),
                "repo_dirty": _dirty(root),
                "ossie_dirty": _dirty(root / "ossie"),
                "converters": list(manifest.converters),
                "timeout_seconds": timeout,
                "preparation_enabled": prepare,
                "native_gates_enabled": native,
            },
            "capabilities": {
                name: {
                    "import": converter.skip
                    or _import_unavailable(converter)
                    or "supported",
                    "export": converter.skip or "supported",
                    "roundtrip": converter.skip
                    or converter.roundtrip_skip
                    or _import_unavailable(converter)
                    or "supported",
                }
                for name, converter in manifest.converters.items()
            },
            "preparations": {},
            "sources": [],
            "cases": [],
            "regressions": [],
            "document_regressions": [],
            "harness_errors": [],
        }

    def command(self, result, command, cwd, evidence, converter):
        print(f"  {evidence.relative_to(self.output)}", flush=True)
        stage = run_command(command, cwd, evidence, self.timeout)
        result["commands"].append(stage)
        for diagnostic in diagnostics(stage, converter):
            result["diagnostics"].append(
                {**diagnostic, "stage": str(evidence.relative_to(self.output))}
            )
        return stage

    def prepare(self, converter):
        result = _result()
        if converter.skip:
            return _result("SKIP", converter.skip)
        needed = ["uv"] if converter.toolchain == "uv" else ["mvn", "java"]
        if not self.prepare_enabled and converter.toolchain == "maven":
            needed = ["java"]
        missing = [name for name in needed if not shutil.which(name)]
        if missing:
            result.update(
                grade="SKIP",
                reason=f"missing toolchain executable(s): {', '.join(missing)}",
            )
            return result
        if converter.toolchain == "maven":
            stage = self.command(
                result,
                ["java", "-version"],
                converter.cwd,
                self.output / "preparations" / converter.name / "java",
                converter.name,
            )
            text = "".join(Path(stage[key]).read_text() for key in ("stdout", "stderr"))
            version = re.search(r'version\s+"(\d+)', text)
            if not succeeded(stage) or not version or int(version.group(1)) < 21:
                result.update(
                    grade="SKIP", reason="requires a working Java 21+ runtime on PATH"
                )
                return result
        if self.prepare_enabled:
            stage = self.command(
                result,
                expand(converter.prepare, self.root),
                converter.cwd,
                self.output / "preparations" / converter.name / "prepare",
                converter.name,
            )
            if not succeeded(stage):
                result.update(
                    grade="FAIL",
                    reason=stage["error"] or "dependency preparation failed; see logs",
                )
        return result

    def validate(self, result, path):
        data, validation = self.validator.validate(path)
        result["validation"] = validation
        if validation["errors"]:
            result.update(grade="FAIL", reason="Ossie validation failed")
            return None
        result["inventory"] = inventory(data)
        if not meaningful_ossie(data):
            result.update(
                grade="EMPTY", reason="Ossie document contains no fields or metrics"
            )
            return None
        return data

    def source(self, owner, fixture, path):
        source_id = f"{owner}--{fixture}"
        result = {
            **_result(),
            "id": source_id,
            "owner": owner,
            "fixture": fixture,
            "input": str(path),
            "ossie": str(self.output / "sources" / source_id / "model.yaml"),
        }
        target = Path(result["ossie"])
        target.parent.mkdir(parents=True)
        if owner == "canonical":
            shutil.copyfile(path, target)
        else:
            preparation = self.report["preparations"][owner]
            if preparation["grade"] != "OK":
                result.update(
                    grade="SKIP",
                    reason=f"source preparation {preparation['grade']}: {preparation['reason']}",
                )
                return result, None
            converter = self.manifest.converters[owner]
            unavailable = _import_unavailable(converter)
            if unavailable:
                result.update(grade="SKIP", reason=unavailable)
                return result, None
            stage = self.command(
                result,
                expand(converter.import_command, self.root, path, target),
                converter.cwd,
                target.parent / "import",
                owner,
            )
            if not succeeded(stage):
                result.update(
                    grade="FAIL",
                    reason=stage["error"] or "source import failed; see logs",
                )
                return result, None
            if not target.is_file() or not target.stat().st_size:
                result.update(
                    grade="EMPTY", reason="source import produced no Ossie document"
                )
                return result, None
        data = self.validate(result, target)
        if data is not None and (
            has_degradation(result["diagnostics"]) or result["validation"]["warnings"]
        ):
            result.update(
                grade="LOSSY",
                reason="source import or validation reported degradation; see diagnostics",
            )
        return result, data

    def native_gate(self, converter, path, content, evidence):
        result = _result("SKIP", "native gates not requested (use --native)")
        if not self.native:
            return result
        gate = converter.native_gate
        if not gate:
            result["reason"] = "no native gate registered"
            return result
        if not os.environ.get(gate["requires_env"]):
            result["reason"] = (
                f"missing native-gate environment variable: {gate['requires_env']}"
            )
            return result
        command = expand(gate["command"], self.root, path, files=content["files"])
        if not shutil.which(command[0]):
            result["reason"] = f"missing native-gate executable: {command[0]}"
            return result
        stage = self.command(result, command, converter.cwd, evidence, converter.name)
        if stage["returncode"] == 2 and stage["error"] is None:
            result["reason"] = "native gate unavailable; see logs"
        else:
            result.update(
                grade="OK" if succeeded(stage) else "FAIL",
                reason=""
                if succeeded(stage)
                else stage["error"] or "native gate rejected output",
            )
        return result

    def acceptance(self, source, data, converter, *, directory=None):
        case_id = f"{source['id']}--to--{converter.name}"
        directory = directory or self.output / "cases" / case_id
        suffix = (
            ".json"
            if converter.native_format
            in ("dbt", "gooddata", "microsoft", "sigma", "wisdom", "salesforce")
            else ".yaml"
        )
        dest = directory / (
            "native" if converter.output == "directory" else f"native{suffix}"
        )
        result = {
            **_result(),
            "id": case_id,
            "tier": "A",
            "source": source["id"],
            "target": converter.name,
            "output": str(dest),
            "native_gate": _result("SKIP", "export has not succeeded"),
        }
        if data is None:
            result.update(
                grade="SKIP", reason=f"source {source['grade']}: {source['reason']}"
            )
            return result
        preparation = self.report["preparations"][converter.name]
        if preparation["grade"] != "OK":
            result.update(
                grade="SKIP",
                reason=f"target preparation {preparation['grade']}: {preparation['reason']}",
            )
            return result
        directory.mkdir(parents=True)
        result["inventory_before"] = inventory(data)
        stage = self.command(
            result,
            expand(converter.export_command, self.root, Path(source["ossie"]), dest),
            converter.cwd,
            directory / "export",
            converter.name,
        )
        if not succeeded(stage):
            result.update(
                grade="FAIL", reason=stage["error"] or "export failed; see logs"
            )
            return result
        try:
            content = native_content(dest, converter.native_format, converter.output)
        except ContentError as error:
            result.update(grade="FAIL", reason=str(error))
            return result
        result["content"] = content
        if not content["files"] or not content["members"]:
            result.update(
                grade="EMPTY", reason="export produced no meaningful native members"
            )
            return result
        if has_degradation(result["diagnostics"]):
            result.update(
                grade="LOSSY", reason="export reported degradation; see diagnostics"
            )
        result["native_gate"] = self.native_gate(
            converter, dest, content, directory / "native-gate"
        )
        return result

    def pair(self, source, before, converter, acceptance):
        directory = Path(acceptance["output"]).parent
        dest = directory / "returned.yaml"
        result = {
            **_result(),
            "id": acceptance["id"] + "--return",
            "tier": "B",
            "source": source["id"],
            "target": converter.name,
            "acceptance": acceptance["id"],
            "output": str(dest),
            "native_gate": acceptance["native_gate"],
        }
        if acceptance["grade"] not in ("OK", "LOSSY"):
            result.update(
                grade="SKIP",
                reason=f"depends on Tier A {acceptance['grade']}: {acceptance['reason']}",
            )
            return result
        unsupported = converter.roundtrip_skip or _import_unavailable(converter)
        if unsupported:
            result.update(grade="SKIP", reason=unsupported)
            return result
        result["diagnostics"] = list(acceptance["diagnostics"])
        result["inventory_before"] = inventory(before)
        stage = self.command(
            result,
            expand(
                converter.import_command, self.root, Path(acceptance["output"]), dest
            ),
            converter.cwd,
            directory / "reimport",
            converter.name,
        )
        if not succeeded(stage):
            result.update(
                grade="FAIL",
                reason=stage["error"] or "target re-import failed; see logs",
            )
            return result
        if not dest.is_file() or not dest.stat().st_size:
            result.update(
                grade="EMPTY", reason="target re-import produced no Ossie document"
            )
            return result
        after = self.validate(result, dest)
        if after is None:
            return result
        result["inventory_changes"] = compare_inventory(before, after)
        result["inventory_after"] = result.pop("inventory")
        changes = result["inventory_changes"]
        if (
            changes["removed"]
            or changes["added"]
            or has_degradation(result["diagnostics"])
            or result["validation"]["warnings"]
        ):
            result.update(
                grade="LOSSY",
                reason="structural changes or degradation detected; see evidence",
            )
        return result

    def run(self):
        for name, converter in self.manifest.converters.items():
            self.report["preparations"][name] = self.prepare(converter)
        fixtures = [
            ("canonical", key, value) for key, value in self.manifest.canonical.items()
        ]
        fixtures += [
            (name, key, value)
            for name, converter in self.manifest.converters.items()
            for key, value in converter.fixtures.items()
        ]
        for owner, fixture, path in fixtures:
            source, data = self.source(owner, fixture, path)
            self.report["sources"].append(source)
            for name, converter in self.manifest.converters.items():
                accepted = self.acceptance(source, data, converter)
                self.report["cases"].append(accepted)
                if owner != "canonical" and owner != name:
                    self.report["cases"].append(
                        self.pair(source, data, converter, accepted)
                    )
        for name, regression in self.manifest.regressions.items():
            self.report["regressions"].append(self.binding_regression(name, regression))
        for name, regression in self.manifest.document_regressions.items():
            for target in regression.targets:
                self.report["document_regressions"].append(
                    self.document_regression(name, regression, target)
                )
        return self.report

    def document_regression(self, name, regression, target):
        case_id = f"{name}--to--{target}"
        directory = self.output / "document-regressions" / case_id
        directory.mkdir(parents=True)
        result = {
            **_result(),
            "id": case_id,
            "category": regression.category,
            "target": target,
            "input": str(directory / "input.yaml"),
            "expectations": str(directory / "expectations.yaml"),
            "output": str(directory / "conversion" / "returned.yaml"),
            "native_gate": _result("SKIP", "export has not succeeded"),
        }
        shutil.copyfile(regression.input, result["input"])
        shutil.copyfile(regression.expectations, result["expectations"])
        data = self.validate(result, Path(result["input"]))
        result["input_validation"] = result.pop("validation")
        if "inventory" in result:
            result["inventory_before"] = result.pop("inventory")
        if data is None:
            result["reason"] = "regression input: " + result["reason"]
            return result
        result["input_assertions"] = check_document(
            data, regression.category, regression.assertions, input_stage=True
        )
        if result["input_assertions"]["grade"] != "OK":
            result.update(
                grade="FAIL",
                reason="regression input fails its independent assertion oracle",
            )
            return result
        source = {
            "id": "document--" + name,
            "ossie": result["input"],
            "grade": "OK",
            "reason": "",
        }
        converter = self.manifest.converters[target]
        accepted = self.acceptance(
            source, data, converter, directory=directory / "conversion"
        )
        result.update(
            commands=accepted["commands"],
            diagnostics=accepted["diagnostics"],
            native_gate=accepted["native_gate"],
            native_output=accepted["output"],
        )
        if accepted["grade"] not in ("OK", "LOSSY"):
            result.update(grade=accepted["grade"], reason=accepted["reason"])
            return result
        result["content"] = accepted["content"]
        if regression.category == "sources":
            result["native_assertions"] = check_native_sources(
                Path(accepted["output"]), converter.native_format, regression.assertions
            )
        returned = self.pair(source, data, converter, accepted)
        result["commands"] = accepted["commands"] + returned["commands"]
        result["diagnostics"] = returned["diagnostics"]
        for key in ("validation", "inventory_after", "inventory_changes"):
            if key in returned:
                result[key] = returned[key]
        if returned["grade"] not in ("OK", "LOSSY"):
            result.update(grade=returned["grade"], reason=returned["reason"])
            return result
        after = self.validator.module.load_yaml_named(
            Path(result["output"]).read_text(encoding="utf-8"), result["output"]
        )
        result["assertions"] = check_document(
            after, regression.category, regression.assertions
        )
        if (
            result["assertions"]["grade"] != "OK"
            or result.get("native_assertions", {}).get("grade") == "FAIL"
        ):
            result.update(
                grade="FAIL",
                reason=f"{regression.category} preservation assertions failed; see expected/actual evidence",
            )
        return result

    def binding_regression(self, name, regression):
        directory = self.output / "regressions" / name
        directory.mkdir(parents=True)
        converter = self.manifest.converters[regression.source]
        result = {
            **_result(),
            "id": name,
            "source": regression.source,
            "fixture": regression.fixture,
            "via": regression.via,
            "output": str(directory / "source-native"),
            "expectations": str(directory / "expectations.yaml"),
            "native_gate": _result("SKIP", "source-native export has not succeeded"),
        }
        shutil.copyfile(regression.expectations, result["expectations"])
        result["source_bindings"] = check_bindings(
            converter.fixtures[regression.fixture], regression.metrics
        )
        if result["source_bindings"]["grade"] != "OK":
            result.update(
                grade="FAIL",
                reason="source fixture fails its independent binding oracle",
            )
            return result
        source_id = f"{regression.source}--{regression.fixture}"
        pair = next(
            (
                case
                for case in self.report["cases"]
                if case["tier"] == "B"
                and case["source"] == source_id
                and case["target"] == regression.via
            ),
            None,
        )
        if pair is None or pair["grade"] not in ("OK", "LOSSY"):
            result.update(
                grade="SKIP",
                reason="pairwise return unavailable: "
                + (
                    f"{pair['grade']}: {pair['reason']}"
                    if pair
                    else "pair not selected"
                ),
            )
            return result
        result["pair"] = pair["id"]
        dest = Path(result["output"])
        stage = self.command(
            result,
            expand(converter.export_command, self.root, Path(pair["output"]), dest),
            converter.cwd,
            directory / "source-export",
            regression.source,
        )
        if not succeeded(stage):
            result.update(
                grade="FAIL",
                reason=stage["error"]
                or "return to source-native format failed; see logs",
            )
            return result
        try:
            result["content"] = native_content(
                dest, converter.native_format, converter.output
            )
        except ContentError as error:
            result.update(grade="FAIL", reason=str(error))
            return result
        if not result["content"]["members"]:
            result.update(
                grade="EMPTY",
                reason="return to source-native format produced no model members",
            )
            return result
        result["returned_bindings"] = check_bindings(dest, regression.metrics)
        if result["returned_bindings"]["grade"] != "OK":
            result.update(
                grade="FAIL",
                reason="returned native aggregate bindings differ or cannot be verified",
            )
        result["native_gate"] = self.native_gate(
            converter, dest, result["content"], directory / "native-gate"
        )
        return result


def exit_code(report, strict=False):
    if report["harness_errors"]:
        return 2
    if strict:
        results = (
            list(report["preparations"].values())
            + report["sources"]
            + report["cases"]
            + report.get("regressions", [])
            + report.get("document_regressions", [])
        )
        if any(result["grade"] in ("FAIL", "EMPTY", "SKIP") for result in results):
            return 1
        if any(
            case.get("native_gate", {}).get("grade") == "FAIL"
            for case in report["cases"]
            + report.get("regressions", [])
            + report.get("document_regressions", [])
        ):
            return 1
    return 0
