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

"""Deterministic YAML output.

Keys are emitted in construction order rather than sorted, so the emitted
document reads in the order the spec documents it (``concept``, ``type``,
``description``, ...). Nothing time- or environment-dependent enters the output:
converting the same CSN and overlay twice produces byte-identical YAML.
"""

from __future__ import annotations

from typing import Any

import yaml


class _OssieDumper(yaml.SafeDumper):
    """SafeDumper with block-style collections and no anchors."""

    #: Repeated values are written out in full. Anchors and aliases are valid
    #: YAML, but a fixture that a reader diffs by eye should not contain them.
    ignore_aliases = staticmethod(lambda *_args: True)

    def increase_indent(self, flow: bool = False, indentless: bool = False) -> None:
        # Indent list items under their key, which is how the spec's own
        # examples and the other converters' fixtures are written.
        super().increase_indent(flow=flow, indentless=False)


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_OssieDumper.add_representer(str, _represent_str)


def _normalize_scalars(node: Any) -> Any:
    """Strip trailing whitespace from every string in the document.

    Descriptions curated as folded YAML scalars arrive with a trailing newline,
    which would otherwise force each of them into a literal block holding one
    very long line. The trailing whitespace carries no meaning, and dropping it
    lets the dumper wrap prose normally.
    """
    if isinstance(node, dict):
        return {k: _normalize_scalars(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_normalize_scalars(v) for v in node]
    if isinstance(node, str):
        return node.rstrip()
    return node


def dump_yaml(document: dict[str, Any]) -> str:
    """Serialize an ontology document to deterministic YAML."""
    return yaml.dump(
        _normalize_scalars(document),
        Dumper=_OssieDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=100,
    )


def load_yaml(text: str) -> Any:
    """Parse YAML with the safe loader (matches the dumper above)."""
    return yaml.safe_load(text)
