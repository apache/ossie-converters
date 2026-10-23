#!/usr/bin/env python3
#
# /// script
# requires-python = ">=3.10"
# ///

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

"""How much of a model survives Ossie -> vendor -> Ossie, for every converter?

Runs each portability fixture in the core repository's `compliance/portability/`
through every converter that can be driven in both directions from its command
line, grades each returned document against its input with the core repository's
grader, and prints one table. It turns the hand-driven round-trip table from the
portability measurement into one command.

    uv run dev/portability_matrix.py                    # every fixture, every converter
    uv run dev/portability_matrix.py --converters microsoft,dbt
    uv run dev/portability_matrix.py --json > matrix.json
    uv run dev/portability_matrix.py --keep              # leave the returned documents

Columns (counts over the metrics of every fixture, as graded by `grade.py`):

  portable    metrics returned in a portable dialect: PORTABLE, RELABELED (same text,
              different portable tag) and REWRITTEN judged cosmetic by the grader
  review      REWRITTEN where the normalized expressions differ
  vendor      SURVIVED_VENDOR_ONLY: returned only in the converter's own dialect
  placeholder PLACEHOLDER: a valid stand-in (e.g. BLANK()) replaced the content
  dropped     DROPPED_DIAGNOSED + DROPPED_SILENT
  rejected    fixtures the converter refused as a whole (non-zero exit, or REJECTED)

Each converter runs in its own `uv` environment, as in `cube/tools/interop_matrix.py`,
so the first run for a converter resolves its dependencies there. A converter whose
environment cannot be built is reported as SKIP, not as a failure. Converters that
cannot round-trip from the command line (export-only, API-only, or Java) are listed
with the reason rather than scored.

Stdlib only, so it needs no environment of its own.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

# How to drive one converter both ways. `export` and `import_` are argv templates;
# `{input}` and `{output}` are filled in per fixture. `vendor` is the name of the
# intermediate artifact (a file, or a directory when it ends in "/"). `import_glob`
# is for a CLI that takes the files inside an exported directory rather than the
# directory itself.


@dataclass(frozen=True)
class Converter:
    name: str
    directory: str
    script: str
    export: tuple
    import_: tuple
    vendor: str
    import_glob: str | None = None


CONVERTERS = [
    Converter("cube", "cube", "ossie-cube",
              ("export", "-i", "{input}", "-o", "{output}"),
              ("import", "-i", "{input}", "-o", "{output}"), "cube_model/"),
    Converter("dbt", "dbt", "ossie-dbt",
              ("ossie-to-msi", "-i", "{input}", "-o", "{output}"),
              ("msi-to-ossie", "-i", "{input}", "-o", "{output}"), "semantic_manifest.json"),
    Converter("honeydew", "honeydew", "ossie-honeydew",
              ("ossie-to-honeydew", "-i", "{input}", "-o", "{output}"),
              ("honeydew-to-ossie", "-i", "{input}", "-o", "{output}"), "honeydew/"),
    Converter("microsoft", "microsoft", "ossie-microsoft",
              ("export", "-q", "-i", "{input}", "-o", "{output}"),
              ("import", "-i", "{input}", "-o", "{output}"), "model.bim"),
    Converter("microsoft+translate-dax", "microsoft", "ossie-microsoft",
              ("export", "-q", "-i", "{input}", "-o", "{output}"),
              ("import", "--translate-dax", "-i", "{input}", "-o", "{output}"), "model.bim"),
    Converter("omni", "omni", "ossie-omni",
              ("export", "-i", "{input}", "-o", "{output}"),
              ("import", "-i", "{input}", "-o", "{output}"), "omni/"),
    Converter("orionbelt", "orionbelt", "ossie-orionbelt",
              ("ossie-to-obml", "-i", "{input}", "-o", "{output}"),
              ("obml-to-ossie", "-i", "{input}", "-o", "{output}"), "model.obml.yaml"),
    Converter("sigma", "sigma", "ossie-sigma",
              ("ossie-to-sigma", "-i", "{input}", "-o", "{output}"),
              ("sigma-to-ossie", "-i", "{input}", "-o", "{output}"), "sigma.json"),
    Converter("thoughtspot", "thoughtspot", "ossie-thoughtspot",
              ("to-tml", "{input}", "-o", "{output}"),
              ("to-ossie", "{input}", "-o", "{output}"), "tml/", import_glob="*.tml"),
    Converter("wisdom", "wisdom", "ossie-wisdom",
              ("ossie-to-wisdom", "-i", "{input}", "-o", "{output}"),
              ("wisdom-to-ossie", "-i", "{input}", "-o", "{output}"), "domain-export.json"),
]

# Listed, not scored: there is no command-line path back to Ossie to measure.
NOT_ROUND_TRIPPABLE = {
    "snowflake": "export only",
    "nvidia": "export only",
    "wisdom-semantic-view": "export only",
    "holistics": "import needs compiled AML (a Holistics compile step)",
    "gooddata": "no command line; API only",
    "hex": "export only",
    "databricks": "Java converter, needs Maven",
    "polaris": "Java converter, needs Maven",
    "salesforce": "Java converter, needs Maven",
}

PORTABLE_OUTCOMES = {"PORTABLE", "RELABELED"}

# A converter that has not finished by then is hung rather than working (resolving a
# cold dependency cache is the slow part).
_TIMEOUT_S = 600

# uv's wording when a converter's environment cannot be built, which is not the
# converter rejecting the model. Matching message text is unavoidable and may drift;
# a message that stops matching shows up as a rejection with its reason, not silently.
_ENV_FAILURE_MARKERS = ("No solution found", "Failed to spawn", "no such command",
                        "does not exist")


def run(argv, cwd=None):
    """Run `argv`, or return a synthetic failure rather than raising."""
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(argv, 1, "", f"timed out after {_TIMEOUT_S}s")
    except FileNotFoundError as error:
        return subprocess.CompletedProcess(argv, 1, "", f"{argv[0]}: {error.strerror}")


def repo_root():
    for parent in Path(__file__).resolve().parents:
        if (parent / "microsoft").is_dir() and (parent / "dev").is_dir():
            return parent
    sys.exit("cannot locate the repository root from this script's path")


def load_fixtures(fixtures_dir):
    """Return [(variant id, fixture path)] from the fixtures' manifest.yaml.

    The manifest is read with a tiny line parser so this script stays stdlib only: it
    only needs the `{id: ..., file: ...}` variant lines.
    """
    manifest = fixtures_dir / "manifest.yaml"
    if not manifest.exists():
        sys.exit(f"no manifest.yaml in {fixtures_dir}")
    fixtures = []
    for line in manifest.read_text().splitlines():
        line = line.strip()
        if line.startswith("- {id:") and "file:" in line:
            fields = {
                key.strip(): value.strip()
                for key, value in (
                    part.split(":", 1) for part in line[3:-1].split(",") if ":" in part
                )
            }
            variant = fields["id"].strip()
            path = fixtures_dir / fields["file"].strip()
            fixtures.append((variant, path))
    return fixtures


def converter_argv(converter, template, input_path, output_path, python, cwd):
    """The argv to run one direction of `converter`.

    By default the console script runs inside the converter's own uv environment. With
    `python`, the console script's entry point (read from the converter's
    pyproject.toml) is called from its src/ tree instead, for an interpreter that
    already has the converter's dependencies.
    """
    args = [part.format(input=input_path, output=output_path) for part in template]
    if not python:
        return ["uv", "run", "--quiet", converter.script, *args]
    module, function = entry_point(cwd, converter.script)
    source = (
        "import sys; sys.path.insert(0, 'src'); "
        f"from {module} import {function} as main; "
        f"sys.argv = [{converter.script!r}] + sys.argv[1:]; sys.exit(main())"
    )
    return [python, "-c", source, *args]


_SCRIPT_LINE = re.compile(r'^\s*"?([A-Za-z0-9_.-]+)"?\s*=\s*"([A-Za-z0-9_.]+):([A-Za-z0-9_]+)"')


def entry_point(cwd, script):
    """(module, function) for `script` from [project.scripts] in cwd/pyproject.toml.

    A line parser rather than tomllib, so the script also runs on Python 3.10.
    """
    in_scripts = False
    for line in (cwd / "pyproject.toml").read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_scripts = stripped == "[project.scripts]"
            continue
        match = _SCRIPT_LINE.match(line) if in_scripts else None
        if match and match.group(1) == script:
            return match.group(2), match.group(3)
    sys.exit(f"{script} is not a console script of {cwd.name}")


def round_trip(converter, fixture, work, root, python):
    """Ossie -> vendor -> Ossie for one fixture. Returns (returned path or None, log)."""
    cwd = root / converter.directory
    vendor = work / converter.vendor.rstrip("/")
    returned = work / "returned.ossie.yaml"
    log = []

    export = run(converter_argv(converter, converter.export, fixture, vendor, python, cwd), cwd)
    log.append(export.stderr)
    if export.returncode != 0:
        return None, "\n".join(log), _is_env_failure(export.stderr)

    import_inputs = [vendor]
    if converter.import_glob:
        import_inputs = sorted(vendor.glob(converter.import_glob))
        if not import_inputs:
            return None, "\n".join(log + ["export wrote no files to import"]), False
    template = list(converter.import_)
    position = template.index("{input}")
    expanded = template[:position] + [str(p) for p in import_inputs] + template[position + 1:]
    argv = converter_argv(converter, tuple(expanded), "", returned, python, cwd)
    imported = run(argv, cwd)
    log.append(imported.stderr)
    if imported.returncode != 0 or not returned.exists():
        return None, "\n".join(log), _is_env_failure(imported.stderr)
    return returned, "\n".join(log), False


def _is_env_failure(stderr):
    return any(marker in stderr for marker in _ENV_FAILURE_MARKERS)


def grade(grader, grader_python, fixture, returned, diagnostics, converter_name):
    """Run the core repository's grader; return its JSON report."""
    if grader_python:
        argv = [grader_python, str(grader)]
    else:
        argv = ["uv", "run", "--quiet", "--with", "pyyaml", "--with", "sqlglot",
                "python", str(grader)]
    argv += ["--input", str(fixture), "--returned", str(returned),
             "--diagnostics", str(diagnostics), "--converter", converter_name, "--json"]
    result = run(argv)
    if result.returncode not in (0, 1) or not result.stdout.strip():
        return {"error": result.stderr.strip() or "grader produced no output"}
    return json.loads(result.stdout)


def tally(reports):
    """Fold per-fixture grader reports into the matrix columns (metrics only)."""
    counts = Counter()
    for report in reports:
        if report.get("rejected") or report.get("error"):
            counts["rejected"] += 1
            continue
        for finding in report.get("findings", []):
            if not finding.get("kind", "").startswith("metric"):
                continue
            outcome = finding.get("outcome")
            detail = finding.get("detail", "")
            counts["metrics"] += 1
            if outcome in PORTABLE_OUTCOMES:
                counts["portable"] += 1
            elif outcome == "REWRITTEN":
                counts["portable" if detail.startswith("cosmetic") else "review"] += 1
            elif outcome == "SURVIVED_VENDOR_ONLY":
                counts["vendor"] += 1
            elif outcome == "PLACEHOLDER":
                counts["placeholder"] += 1
            elif outcome in ("DROPPED_DIAGNOSED", "DROPPED_SILENT"):
                counts["dropped"] += 1
            else:
                counts["other"] += 1
    return counts


def default_fixtures(root):
    return root / "ossie" / "compliance" / "portability"


def main(argv=None):
    root = repo_root()
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--fixtures", default=str(default_fixtures(root)),
                    help="portability fixtures directory with manifest.yaml and grader/ "
                         "(default: the ossie submodule's compliance/portability)")
    ap.add_argument("--converters", help="comma-separated subset to run")
    ap.add_argument("--python",
                    help="run converters from their src/ trees with this interpreter "
                         "instead of uv (it must already have their dependencies)")
    ap.add_argument("--grader-python",
                    help="run the grader with this interpreter instead of uv "
                         "(it needs pyyaml; sqlglot is optional)")
    ap.add_argument("--json", action="store_true", help="emit the matrix as JSON")
    ap.add_argument("--keep", action="store_true",
                    help="keep the returned documents and print where they are")
    args = ap.parse_args(argv)

    fixtures_dir = Path(args.fixtures).expanduser().resolve()
    grader = fixtures_dir / "grader" / "grade.py"
    if not grader.exists():
        sys.exit(f"no grader at {grader}: the ossie submodule may predate the portability "
                 "fixtures (apache/ossie#596). Update it (git submodule update --remote ossie) "
                 "or pass --fixtures <apache/ossie checkout>/compliance/portability")
    fixtures = load_fixtures(fixtures_dir)

    selected = CONVERTERS
    if args.converters:
        wanted = {name.strip() for name in args.converters.split(",") if name.strip()}
        unknown = wanted - {c.name for c in CONVERTERS}
        if unknown:
            sys.exit(f"unknown or not round-trippable converter(s): {', '.join(sorted(unknown))}")
        selected = [c for c in CONVERTERS if c.name in wanted]

    out = Path(tempfile.mkdtemp(prefix="ossie-portability-"))
    matrix = {}
    try:
        for converter in selected:
            reports, skipped = [], False
            for variant, fixture in fixtures:
                work = out / converter.name / variant
                work.mkdir(parents=True)
                returned, log, env_failure = round_trip(
                    converter, fixture, work, root, args.python)
                if env_failure:
                    skipped = True
                    break
                if returned is None:
                    reports.append({"variant": variant, "rejected": True,
                                    "detail": (log.strip().splitlines() or [""])[-1]})
                    continue
                diagnostics = work / "diagnostics.txt"
                diagnostics.write_text(log)
                report = grade(grader, args.grader_python, fixture, returned,
                               diagnostics, converter.name)
                report["variant"] = variant
                reports.append(report)
            matrix[converter.name] = {"skipped": skipped, "reports": reports,
                                      "counts": dict(tally(reports))}

        if args.json:
            print(json.dumps({"fixtures": [v for v, _ in fixtures], "matrix": matrix,
                              "not_round_trippable": NOT_ROUND_TRIPPABLE}, indent=2))
        else:
            print_table(fixtures, matrix)
        if args.keep:
            print(f"\noutputs: {out}", file=sys.stderr)
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(out, ignore_errors=True)


def print_table(fixtures, matrix):
    columns = ("portable", "review", "vendor", "placeholder", "dropped", "rejected")
    print(f"fixtures: {len(fixtures)} variants")
    print()
    header = f"{'converter':<24}" + "".join(f"{c:>12}" for c in columns) + f"{'metrics':>9}"
    print(header)
    print("-" * len(header))
    for name, entry in matrix.items():
        if entry["skipped"]:
            print(f"{name:<24}  SKIP (converter environment could not be built)")
            continue
        counts = entry["counts"]
        print(f"{name:<24}" + "".join(f"{counts.get(c, 0):>12}" for c in columns)
              + f"{counts.get('metrics', 0):>9}")
    for name, reason in NOT_ROUND_TRIPPABLE.items():
        print(f"{name:<24}  --  {reason}")


if __name__ == "__main__":
    sys.exit(main())
