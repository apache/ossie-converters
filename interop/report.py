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

from .portability import COLUMNS, KINDS


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def _link(path, output, label="evidence"):
    path = Path(path)
    try:
        relative = path.relative_to(output)
    except ValueError:
        return _cell(path)
    escaped_label = str(label).replace("\\", "\\\\").replace("[", r"\[").replace("]", r"\]")
    return f"[{_cell(escaped_label)}]({quote(relative.as_posix(), safe='/')})"


def _case_evidence(case):
    return Path(case["output"]).parent / f"tier-{case['tier']}.json"


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
        evidence = _case_evidence(case)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        evidence.write_text(
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
    for case in report.get("portability", []):
        directory = Path(case["input"]).parent
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "result.json").write_text(
            json.dumps(_portable(case, output), indent=2) + "\n", encoding="utf-8"
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
    pair_links = {
        (case["source"], case["target"]): _link(
            _case_evidence(case), output, label=case["grade"]
        )
        for case in report["cases"]
        if case["tier"] == "B"
    }
    for source in report["sources"]:
        if source["owner"] == "canonical":
            continue
        cells = [pair_links.get((source["id"], name), "—") for name in names]
        lines.append(f"| {source['id']} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "## Case details",
        "",
        "| Tier | Source | Target | Result | Issues | Foreign | Native gate | Reason / evidence |",
        "|---|---|---|---|---:|---:|---|---|",
    ]
    for case in report["cases"]:
        issues = case["diagnostics"]
        reason = _cell(case["reason"])
        evidence = _link(_case_evidence(case), output)
        lines.append(
            f"| {case['tier']} | {case['source']} | {case['target']} | {case['grade']} | "
            f"{len(issues)} | {sum(x['foreign_extension'] for x in issues)} | "
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
    if live := report.get("live_databricks"):
        lines += [
            "",
            "## Live Databricks TPC-DS measure parity",
            "",
            f"{live['grade']} {_cell(live['reason'])}".rstrip(),
        ]
        if live.get("cleanup_error") and live["cleanup_error"] != live["reason"]:
            lines.append(_cell(live["cleanup_error"]))
        if live["grains"]:
            measures = next(iter(live["grains"].values()))["measure_mismatches"]
            lines += [
                "",
                "Counts of differing groups per measure.",
                "",
                "| Grain | Cube rows | Metric View rows | Missing groups | "
                + " | ".join(measures)
                + " |",
                "|---|" + "---:|" * (3 + len(measures)),
            ]
            for grain, summary in live["grains"].items():
                counts = " | ".join(
                    str(value) for value in summary["measure_mismatches"].values()
                )
                lines.append(
                    f"| {grain} | {summary['cube_rows']} | {summary['metric_view_rows']} | "
                    f"{summary['missing_groups']} | {counts} |"
                )
    if report.get("portability"):
        lines += _portability_section(report["portability"], output)
    for error in report["harness_errors"]:
        lines += ["", "## Harness error", "", _cell(error)]
    lines += [
        "",
        "See [results.json](results.json) for per-stage commands, logs, validation, inventories, and unchecked comparisons.",
        "",
    ]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")


def _portability_section(cases, output):
    """Metric and field outcomes per converter, then per case. Report-only."""
    lines = [
        "",
        "## Portability (report-only)",
        "",
        "Each fixture in `ossie/compliance/portability/` is exported and re-imported by every "
        "converter that can round-trip, then graded against its input by the core grader. "
        "Metrics and fields are totalled separately. `portable` includes rewrites the grader "
        "proves cosmetic and relabels to another portable tag, `review` the rewrites it cannot "
        "prove cosmetic, and `vendor` what came back only under a vendor tag. These scores "
        "never change the exit status, with or without `--strict`.",
    ]
    totals = {}
    for case in cases:
        label = case["mode"] or case["target"]
        entry = totals.setdefault(
            label, {"grades": Counter(), **{kind: Counter() for kind in KINDS}}
        )
        entry["grades"][case["grade"]] += 1
        if case["grading"]:
            for kind in KINDS:
                entry[kind].update(case["grading"].get(kind + "s", {}))
    for kind in KINDS:
        lines += [
            "",
            f"### {kind.capitalize()}s",
            "",
            "| Converter | Round trips | " + " | ".join(COLUMNS) + " | authored |",
            "|---|---|" + "---:|" * (len(COLUMNS) + 1),
        ]
        for label, entry in totals.items():
            grades = ", ".join(
                f"{grade} {count}" for grade, count in sorted(entry["grades"].items())
            )
            lines.append(
                f"| {label} | {grades} | "
                + " | ".join(str(entry[kind][column]) for column in COLUMNS)
                + f" | {entry[kind]['authored']} |"
            )
    errors = sum(1 for case in cases if case.get("grading_error"))
    if errors:
        lines += [
            "",
            f"{errors} round trip(s) could not be graded; the grader error is on each case below.",
        ]
    lines += [
        "",
        "| Fixture | Converter | Round trip | Grader outcomes | Reason / evidence |",
        "|---|---|---|---|---|",
    ]
    for case in cases:
        grading = case["grading"]
        outcomes = (
            ", ".join(
                f"{name}={count}" for name, count in sorted(grading["summary"].items())
            )
            if grading
            else (
                f"grader error: {case['grading_error']}"
                if case.get("grading_error")
                else "not graded"
            )
        )
        evidence = _link(Path(case["input"]).parent / "result.json", output)
        lines.append(
            f"| {case['variant']} | {case['mode'] or case['target']} | {case['grade']} "
            f"| {_cell(outcomes)} | {_cell(case['reason'])} {evidence} |"
        )
    return lines
