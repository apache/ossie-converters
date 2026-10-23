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

"""Shared pieces: the error type and the strict-form helpers.

An Ossie document in *strict form* carries every `custom_extensions[].data` as a JSON string,
which is what the 0.2.0 schema accepts and what every other converter can read. The converter
works on objects; `converter.export` writes the strict form and `load` here reads it back.
"""

from __future__ import annotations

import json


class ConversionError(Exception):
    """The input cannot be converted at all (a malformed file, an unknown metric name)."""


def load(doc: dict) -> dict:
    """Strict form -> working form: extension data from JSON strings back to objects."""
    out = json.loads(json.dumps(doc))
    holders = [out] + out["datasets"] + [f for d in out["datasets"] for f in d.get("fields", [])] \
        + out.get("relationships", []) + out["metrics"]
    for h in holders:
        for x in h.get("custom_extensions") or []:
            if isinstance(x["data"], str):
                x["data"] = json.loads(x["data"])
    return out
