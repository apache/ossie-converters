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

"""Collect object outcomes during one CLI conversion."""

import json
from contextlib import contextmanager
from contextvars import ContextVar

from ._common import VENDOR

_entries: ContextVar[list[dict] | None] = ContextVar("conversion_entries", default=None)


@contextmanager
def collect_entries(entries):
    token = _entries.set(entries)
    try:
        yield
    finally:
        _entries.reset(token)


def record(kind, source_path, source, status, **details):
    entries = _entries.get()
    if entries is None:
        return
    entry = {"kind": kind, "source_path": source_path, "status": status}
    name = source.get("name") if isinstance(source, dict) else None
    if isinstance(name, str):
        entry["name"] = name
    entry.update({key: value for key, value in details.items() if value is not None})
    entries.append(entry)


def record_items(kind, source_path, items, status, **details):
    if _entries.get() is None or not isinstance(items, list):
        return
    for index, item in enumerate(items):
        record(kind, f"{source_path}/{index}", item, status, **details)


def record_dropped_extensions(source, source_path):
    if _entries.get() is None:
        return
    for index, extension in enumerate(source.get("custom_extensions") or []):
        if not isinstance(extension, dict) or extension.get("vendor_name") == VENDOR:
            continue
        vendor = extension.get("vendor_name", extension.get("vendor"))
        path = f"{source_path}/custom_extensions/{index}"
        payload = extension.get("data", extension.get("content"))
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (ValueError, RecursionError):
                payload = None
        properties = sorted(map(str, payload)) if isinstance(payload, dict) else None
        record(
            "extension", path, extension, "dropped",
            vendor=vendor if isinstance(vendor, str) else None,
            properties=properties, reason="unsupported_vendor",
        )
        if vendor == "SNOWFLAKE" and isinstance(payload, dict):
            metrics = payload.get("metrics")
            if isinstance(metrics, list):
                for metric_index, metric in enumerate(metrics):
                    record(
                        "metric", path, metric, "dropped", vendor=vendor,
                        payload_path=f"/metrics/{metric_index}", reason="unsupported_vendor",
                    )
