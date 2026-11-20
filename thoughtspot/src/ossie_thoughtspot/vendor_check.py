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

"""The THOUGHTSPOT vendor validator for apache/ossie#697's hook.

`validate.py --vendor THOUGHTSPOT` hands this module the `data` object of each
THOUGHTSPOT `nonportable_extensions` entry, one at a time, with the site it
sits at. Nothing is decoded on the way: apache/ossie#522 made `data` an object
in the schema, and the hook requires one. This module answers one question
only -- *is our own payload
well-formed* -- which is the scope the dev@ thread settled on ("own payload
plus own dialect"), narrowed further to the payload for this first checker.

Three deliberate boundaries:

* **It reports, it does not raise.** `stash.read_stash` is the other reader of
  the same payload and it raises, correctly: it runs at the start of a
  conversion that cannot proceed on a payload it cannot trust. A validator is
  the opposite job -- name everything wrong with the document in one pass -- so
  the two readers answer the same questions and end differently. The duplication
  is one `_v` comparison and one scan; sharing the *code* would mean sharing the
  control flow, which is the part that has to differ.
* **It does not name the object.** The hook prefixes every message with the
  vendor and the site (`[Vendor THOUGHTSPOT] Field 'orders.amount' in model
  'sales': ...`), so a message that repeated the name would print it twice.
* **No witness check yet.** The stale-stash check this converter most wants
  needs two things the hook does not have today: the Ossie object the entry
  sits on (every witness is a comparison *against* that object), and a severity
  that does not fail the file (a stale stash is a warning -- the document is
  valid Ossie, our copy of part of it is out of date). Both are promised as a
  follow-up to #697; this checker stays inside what the hook can express.
"""
from typing import Any

from .constants import STASH_VERSION
from .stash import find_forbidden_key


def check(payload: Any, site: Any = None) -> list[str]:
    """Report what is wrong with one decoded THOUGHTSPOT payload.

    `site` carries the hook's location information (kind, name, owning
    dataset). It is accepted and deliberately unused: the hook prefixes each
    returned message with that location itself. It stays in the signature
    because it is the hook's contract, and because the checks that will use it
    -- anything that differs by object kind -- are the natural next ones.
    """
    if not isinstance(payload, dict):
        # Unreachable through the hook, and kept deliberately. Core reports
        # `data is not an object` itself and then skips the call, so this
        # branch answers only a direct call -- another converter's test, or a
        # reader still holding a payload decoded from the pre-#522 JSON string
        # form. It costs one isinstance and keeps `check` total over the
        # parameter type it declares.
        shape = type(payload).__name__
        return [f"payload is a JSON {shape}, not an object; this converter writes an object"]

    findings: list[str] = []

    version = payload.get("_v")
    if isinstance(version, bool) or version != STASH_VERSION:
        # Exact equality, and the same answer stash.read_stash gives. A shape
        # version this converter has never seen must not be read as the
        # current one, and which versions are acceptable is the checker's own
        # contract -- core hands over the decoded value without reading `_v`.
        #
        # The bool exclusion mirrors the reader's, and follows it deliberately:
        # Python reads `True == 1`, so `_v: true` would otherwise pass as
        # version 1 here while `read_stash` refuses it. The two must give one
        # answer about one payload -- a checker that passed what our own reader
        # aborts on would clear a document we cannot convert.
        findings.append(
            f"payload has shape version {version!r}, which this converter does "
            f"not recognise (expected {STASH_VERSION!r})"
        )

    forbidden = find_forbidden_key(payload)
    if forbidden is not None:
        # Scanned at any depth, by the same function `write_stash` refuses on,
        # so a finding here means the payload did not come through this
        # converter's writer -- or that the writer has a hole. Either is worth
        # a reader knowing before they trust the entry.
        findings.append(
            f"payload carries instance-local identity key {forbidden!r}, which "
            f"does not travel in a portable document"
        )

    return findings
