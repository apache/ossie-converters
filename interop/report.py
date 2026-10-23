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

"""Portable JSON evidence and a Markdown matrix derived from the same results."""

from collections import Counter
import json
from pathlib import Path
from urllib.parse import quote


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _link(path, output):
    path = Path(path)
    try:
        relative = path.relative_to(output)
    except ValueError:
        return _cell(path)
    return f"[evidence]({quote(relative.as_posix(), safe='/')})"


def _portable(value, output, key=None):
    if isinstance(value, dict):
        return {name: _portable(child, output, name) for name, child in value.items()}
    if isinstance(value, list):
        return [_portable(child, output, key) for child in value]
    if (
        key
        in {
            "output",
            "input",
            "ossie",
            "stdout",
            "stderr",
            "files",
            "expectations",
            "native_output",
        }
        and isinstance(value, str)
        and value.startswith(str(output) + "/")
    ):
        return str(Path(value).relative_to(output))
    return value


def _bindings(items):
    return (
        "; ".join(
            f"{item['function']}({item['source']}.{item['column']}) at {item['grain']}"
            for item in items
        )
        or "unresolved / missing"
    )


def write_report(report, output):
    output.mkdir(parents=True, exist_ok=True)
    (output / "results.json").write_text(
        json.dumps(_portable(report, output), indent=2) + "\n", encoding="utf-8"
    )
    for name, preparation in report["preparations"].items():
        directory = output / "preparations" / name
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(_portable(preparation, output), indent=2) + "\n",
            encoding="utf-8",
        )
    for source in report["sources"]:
        directory = output / "sources" / source["id"]
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(_portable(source, output), indent=2) + "\n", encoding="utf-8"
        )
    for case in report["cases"]:
        directory = Path(case["output"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory / f"tier-{case['tier']}.json").write_text(
            json.dumps(_portable(case, output), indent=2) + "\n", encoding="utf-8"
        )
    for regression in report.get("regressions", []):
        directory = Path(regression["output"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(_portable(regression, output), indent=2) + "\n", encoding="utf-8"
        )
    for regression in report.get("document_regressions", []):
        directory = Path(regression["input"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(_portable(regression, output), indent=2) + "\n", encoding="utf-8"
        )
    counts = Counter(case["grade"] for case in report["cases"])
    lines = [
        "# Cross-converter interoperability",
        "",
        f"Commit: `{report['metadata']['repo_commit']}`; Ossie: `{report['metadata']['ossie_commit']}`.",
        "",
        f"Dirty checkout: repository={report['metadata'].get('repo_dirty', 'unknown')}, "
        f"Ossie={report['metadata'].get('ossie_dirty', 'unknown')}.",
        "",
        "Case counts: "
        + ", ".join(
            f"{grade}: {counts[grade]}"
            for grade in ("OK", "LOSSY", "EMPTY", "FAIL", "SKIP")
        ),
        "",
        "This run checks native content and named structural inventories. Registered regressions also check",
        "native aggregate bindings, dialect-only expressions, key declarations, and source qualification. General SQL equivalence, metadata, data types, and AI context",
        "are not compared. OK means the applicable checks passed, not complete semantic equivalence.",
        "",
        "## Preparation",
        "",
        "| Converter | Result | Reason |",
        "|---|---|---|",
    ]
    if report.get("capabilities"):
        # Keep unsupported directions visible even when there is no native fixture.
        insertion = lines.index("## Preparation")
        lines[insertion:insertion] = [
            "## Registered capabilities",
            "",
            "| Converter | Import | Export | Re-import exported artifact |",
            "|---|---|---|---|",
            *[
                f"| {name} | {_cell(value['import'])} | {_cell(value['export'])} | {_cell(value['roundtrip'])} |"
                for name, value in report["capabilities"].items()
            ],
            "",
        ]
    for name, result in report["preparations"].items():
        lines.append(
            f"| {name} | {result['grade']} | {_cell(result['reason'])} "
            f"{_link(output / 'preparations' / name / 'result.json', output)} |"
        )
    lines += [
        "",
        "## Source imports and validation",
        "",
        "| Source | Result | Reason |",
        "|---|---|---|",
    ]
    for source in report["sources"]:
        lines.append(
            f"| {source['id']} | {source['grade']} | {_cell(source['reason'])} "
            f"{_link(output / 'sources' / source['id'] / 'result.json', output)} |"
        )
    lines += ["", "## Tier B ordered matrix", ""]
    names = report["metadata"]["converters"]
    lines += [
        "| Source fixture | " + " | ".join(names) + " |",
        "|---|" + "---|" * len(names),
    ]
    for source in report["sources"]:
        if source["owner"] == "canonical":
            continue
        cells = []
        for name in names:
            case = next(
                (
                    case
                    for case in report["cases"]
                    if case["tier"] == "B"
                    and case["source"] == source["id"]
                    and case["target"] == name
                ),
                None,
            )
            cells.append(case["grade"] if case else "—")
        lines.append(f"| {source['id']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Case details",
        "",
        "| Tier | Source | Target | Result | Warns | Foreign | Native gate | Reason / evidence |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for case in report["cases"]:
        warnings = case["diagnostics"]
        reason = _cell(case["reason"])
        evidence = _link(
            Path(case["output"]).parent / f"tier-{case['tier']}.json", output
        )
        lines.append(
            f"| {case['tier']} | {case['source']} | {case['target']} | {case['grade']} | "
            f"{len(warnings)} | {sum(x['foreign_extension'] for x in warnings)} | "
            f"{case['native_gate']['grade']} | {reason} {evidence} |"
        )
    if report.get("regressions"):
        lines += [
            "",
            "## Native metric-binding regressions",
            "",
            "Binding grades cover only declared aggregate source/column/grain assertions. Conversion warnings remain in evidence.",
            "",
            "| Regression | Route | Result | Native gate | Reason / evidence |",
            "|---|---|---|---|---|",
        ]
        for regression in report["regressions"]:
            evidence = _link(Path(regression["output"]).parent / "result.json", output)
            lines.append(
                f"| {regression['id']} | {regression['source']} → {regression['via']} → {regression['source']} "
                f"| {regression['grade']} | {regression['native_gate']['grade']} | {_cell(regression['reason'])} {evidence} |"
            )
            for name, metric in (
                regression.get("returned_bindings", {}).get("metrics", {}).items()
            ):
                if metric["grade"] == "FAIL":
                    lines += ["", f"- `{name}`: {_cell(metric['error'])}"]
                    lines += [
                        "",
                        "| Expected bindings | Actual bindings |",
                        "|---|---|",
                        f"| {_cell(_bindings(metric['expected']))} | {_cell(_bindings(metric['actual']))} |",
                    ]
    if report.get("document_regressions"):
        lines += [
            "",
            "## Dialect, key, and source regressions",
            "",
            "These grades cover the declared assertions; conversion diagnostics remain in evidence.",
            "",
            "| Regression | Category | Target | Result | Native gate | Reason / evidence |",
            "|---|---|---|---|---|---|",
        ]
        for regression in report["document_regressions"]:
            evidence = _link(Path(regression["input"]).parent / "result.json", output)
            lines.append(
                f"| {regression['id']} | {regression['category']} | {regression['target']} | {regression['grade']} "
                f"| {regression['native_gate']['grade']} | {_cell(regression['reason'])} {evidence} |"
            )
        failed = [
            (regression["id"], stage, check)
            for regression in report["document_regressions"]
            for stage in ("input_assertions", "native_assertions", "assertions")
            for check in regression.get(stage, {}).get("checks", [])
            if check["grade"] == "FAIL"
        ]
        if failed:
            lines += [
                "",
                "### Failed preservation assertions",
                "",
                "| Regression | Stage / assertion | Expected | Actual |",
                "|---|---|---|---|",
            ]
            for name, stage, check in failed:
                lines.append(
                    f"| {name} | {stage}: {_cell(check['path'])} | {_cell(json.dumps(check['expected']))} "
                    f"| {_cell(json.dumps(check['actual']))} {_cell(check['error'] or '')} |"
                )
    for error in report["harness_errors"]:
        lines += ["", "## Harness error", "", _cell(error)]
    lines += [
        "",
        "See [results.json](results.json) for per-stage commands, logs, validation, inventories, and unchecked comparisons.",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
