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

"""Deterministic name derivation for CSN to Apache Ossie conversion.

Every rule here is a pure function of the CSN input, so the same model always
yields the same names. Where a business-facing model wants a shorter or more
idiomatic name than the mechanical rule produces, the curation overlay renames
it explicitly (see :mod:`ossie_sap_csn_ontology.curation`) rather than the emitter
guessing at abbreviations.
"""

from __future__ import annotations

import re

_NON_ALNUM = re.compile(r"[^0-9A-Za-z]+")
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")


def pascal_case(text: str) -> str:
    """``"Business Partner Number"`` -> ``"BusinessPartnerNumber"``.

    Words that are already PascalCase or camelCase keep their internal casing,
    so ``"BusinessPartner"`` survives unchanged.
    """
    words = [w for w in _NON_ALNUM.split(text or "") if w]
    return "".join(w if w[:1].isupper() else w[:1].upper() + w[1:] for w in words)


def snake_case(text: str) -> str:
    """``"BusinessPartnerCategory"`` -> ``"business_partner_category"``."""
    words: list[str] = []
    for chunk in (w for w in _NON_ALNUM.split(text or "") if w):
        words.extend(part.lower() for part in _CAMEL_BOUNDARY.split(chunk) if part)
    return "_".join(words)


def strip_leading_underscore(name: str) -> str:
    """CSN association names are conventionally underscore-prefixed."""
    return name.lstrip("_")


def strip_prefix(name: str, prefix: str) -> str:
    """Drop a redundant entity-name prefix from an element name.

    ``("BusinessPartnerCategory", "BusinessPartner")`` -> ``"Category"``. The
    prefix is only dropped when a non-empty remainder starting at a word
    boundary is left, so ``BusinessPartner`` on entity ``BusinessPartner`` is
    returned unchanged rather than becoming empty.
    """
    if prefix and name.startswith(prefix) and len(name) > len(prefix):
        remainder = name[len(prefix) :]
        if remainder[:1].isupper() or remainder[:1] == "_":
            return remainder.lstrip("_")
    return name


def value_type_name(label: str | None, element_name: str) -> str:
    """Name of the value type a scalar element's domain becomes.

    The ``@EndUserText.label`` is preferred because SAP labels carry the
    business name of the domain; the technical element name is the fallback.
    """
    return pascal_case(label) if label else pascal_case(element_name)


def relationship_name(element_name: str, entity_simple_name: str, *, to_many: bool = False) -> str:
    """Default relationship name for a CSN element on an entity.

    Scalar elements and to-one associations become ``snake_case`` of the element
    name with the entity prefix dropped; to-many associations take a ``has_``
    prefix, which reads correctly in both the collection sense and in the
    generated verbalizations.
    """
    base = snake_case(strip_prefix(strip_leading_underscore(element_name), entity_simple_name))
    return f"has_{base}" if to_many else base
