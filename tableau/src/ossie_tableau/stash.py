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

"""Versioned, witness-guarded stash for constructs Ossie cannot model.

A stash is a small JSON object kept inside a `TABLEAU` `custom_extensions` entry:

    {"_v": 1, "kind": "<what>", "witness": <live value when stashed>, "data": <payload>}

`witness` is the value in the Ossie model that the stashed data was derived from (for example the field name and label).
The writer restores the stash only if the live value still equals the witness, so editing the model never resurrects stale
Tableau data; in that case the live value wins and a `stash_stale` loss is recorded.
"""
from __future__ import annotations

import json
import re
from typing import Any

VERSION = 1


class StashError(ValueError):
    """The payload is not a stash this version understands."""


def _canon(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), default=str)


def pack(kind: str, witness: Any, data: Any) -> dict:
    """Build a stash payload. `witness` and `data` must be JSON-serialisable."""
    obj = {"_v": VERSION, "kind": kind, "witness": witness, "data": data}
    json.dumps(obj)  # fail early on unserialisable input
    return obj


def unpack(obj: Any, kind: str | None = None) -> dict:
    """Validate a stash payload (dict or JSON string) and return it. Raises StashError on anything unexpected."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise StashError(f"stash is not valid JSON: {e}") from e
    if not isinstance(obj, dict) or "_v" not in obj:
        raise StashError("not a stash payload (missing _v)")
    if obj["_v"] != VERSION:
        raise StashError(f"unsupported stash version {obj['_v']!r} (this build reads {VERSION})")
    for k in ("kind", "witness", "data"):
        if k not in obj:
            raise StashError(f"stash is missing {k!r}")
    if kind is not None and obj["kind"] != kind:
        raise StashError(f"stash kind {obj['kind']!r}, expected {kind!r}")
    return obj


def names_match(orig: str, sent: str) -> bool:
    """True when `sent` is `orig` as Snowflake READ returns it, allowing reserved-word (trailing or leading underscore) and collision (`_N`) renames."""
    o, n = orig.upper(), sent.upper()
    return n == o or n in (o + "_", "_" + o) or re.fullmatch(re.escape(o) + r"_\d+", n) is not None


def _witness_match(stashed: Any, live: Any, kind: str | None) -> bool:
    if _canon(stashed) == _canon(live):
        return True
    if kind == "field_names" and isinstance(stashed, dict) and isinstance(live, dict):
        sn, ln = stashed.get("name"), live.get("name")
        if isinstance(sn, str) and isinstance(ln, str) and names_match(sn, ln) and stashed.get("label") == live.get("label"):
            return True
    return False


def restore(obj: Any, live_witness: Any, kind: str | None = None) -> tuple[Any, bool]:
    """Return (data, True) when the stash is valid and its witness equals `live_witness`; otherwise (None, False).

    Structural problems raise StashError; a witness mismatch is not an error, it just means the stash is stale.
    `field_names` witnesses also match a reserved-word / collision rename of `name` (`100_NULL` vs `_100_NULL`).
    """
    s = unpack(obj, kind)
    if _witness_match(s["witness"], live_witness, kind or s.get("kind")):
        return s["data"], True
    return None, False
