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

"""Golden outputs for the feature fixtures, shared by the test and the updater.

`tests/test_feature_fixtures.py` compares these with the committed files, and
`tools/update_snapshots.py` rewrites them.
"""

import json
import re
from collections import Counter
from pathlib import Path

from ossie_tableau import convert as C

ROOT = Path(__file__).parent / "fixtures" / "features"


def source_of(case: Path) -> Path:
    return next(p for p in case.iterdir() if p.name.startswith("input."))


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def snapshot(case: Path) -> dict[str, str]:
    """Relative path under `expected/` -> content, for one passing fixture."""
    out: dict[str, str] = {}
    for r in C.convert_file(source_of(case)):
        stem = _safe(r.datasource)
        if r.document is not None:
            out[f"{stem}.ossie.yaml"] = r.document.to_ossie_yaml()
        losses = sorted(
            ({"kind": x.kind, "name": x.name, "detail": x.detail} for x in r.losses),
            key=lambda d: (d["kind"], d["name"] or "", d["detail"] or ""),
        )
        out[f"{stem}.loss.json"] = json.dumps(losses, indent=2, sort_keys=True) + "\n"
    return out


def loss_counts(case: Path) -> dict[str, int]:
    return dict(sorted(Counter(x.kind for r in C.convert_file(source_of(case)) for x in r.losses).items()))
