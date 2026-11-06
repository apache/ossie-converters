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

"""Rewrite the golden outputs and loss counts for the feature fixtures.

Run from tableau/ after an intended behaviour change, then read the diff:

    uv run python tools/update_snapshots.py
"""

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.snapshots import ROOT, loss_counts, snapshot  # noqa: E402


def main() -> int:
    for case in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        exp_file = case / "expected.json"
        exp = json.loads(exp_file.read_text())
        if exp["fail"]:
            continue
        gold = case / "expected"
        shutil.rmtree(gold, ignore_errors=True)
        gold.mkdir()
        for rel, text in snapshot(case).items():
            (gold / rel).write_text(text)
        exp["loss_counts"] = loss_counts(case)
        exp_file.write_text(json.dumps(exp, indent=2) + "\n")
        print(f"updated {case.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
