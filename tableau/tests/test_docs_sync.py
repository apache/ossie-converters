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

"""The README loss tables and docs/loss-catalog.md must agree with the loss catalogue."""
import re
import subprocess
import sys
from pathlib import Path

import pytest

from ossie_tableau.losses import KNOWN_KINDS

pytestmark = pytest.mark.unit
CONV = Path(__file__).resolve().parents[1]


def _gen_docs() -> Path:
    return CONV / "tools" / "gen_docs.py"


HEADING = "## Known limitations and lossiness"


def _limitations() -> str:
    readme = CONV / "README.md"
    if not readme.exists():
        pytest.skip("README.md not present")
    text = readme.read_text()
    if HEADING not in text:
        pytest.skip(f"README.md has no '{HEADING}' section")
    return text.split(HEADING, 1)[1].split("\n## ", 1)[0]


def test_readme_kinds_are_in_catalogue():
    sec = _limitations()
    tokens: set[str] = set()
    for line in sec.splitlines():
        if line.startswith("| `"):
            tokens |= set(re.findall(r"`([^`]+)`", line.split("|")[1]))
        m = re.match(r"- `([a-z_]+)`", line)
        if m:
            tokens.add(m.group(1))
    tokens = {t for t in tokens if re.fullmatch(r"[a-z_]+", t)}
    assert tokens, "no loss kinds found in README"
    assert tokens <= KNOWN_KINDS, sorted(tokens - KNOWN_KINDS)


def test_loss_catalog_doc_is_current():
    r = subprocess.run([sys.executable, str(_gen_docs()), "--check"])
    assert r.returncode == 0, "run `python3 tools/gen_docs.py` to regenerate docs/loss-catalog.md"
