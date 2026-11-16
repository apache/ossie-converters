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

"""One test group per mapping rule in the CSN/Ossie semantics document.

Each group works from a minimal CSN document rather than the full slice, so a
failure names the rule that broke.
"""

from __future__ import annotations

import pytest

from ossie_sap_csn_ontology import ConversionError, CurationError, csn_to_ossie
from ossie_sap_csn_ontology.naming import pascal_case, relationship_name, snake_case, strip_prefix


def build(definitions: dict, curation: dict | None = None) -> dict:
    document = {"csnInteropEffective": "1.0", "definitions": definitions}
    return csn_to_ossie(document, curation, name="test_model")


def concept(document: dict, name: str) -> dict:
    for component in document["ontology"]:
        if component["concept"] == name:
            return component
    raise AssertionError(f"no concept {name!r} in {[c['concept'] for c in document['ontology']]}")


def relationship(document: dict, concept_name: str, name: str) -> dict:
    for entry in concept(document, concept_name).get("relationships", []):
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no relationship {name!r} on {concept_name}")


def entity(elements: dict, label: str | None = None) -> dict:
    definition = {"kind": "entity", "elements": elements}
    if label:
        definition["@EndUserText.label"] = label
    return definition


def string_element(length: int | None = None, key: bool = False, label: str | None = None) -> dict:
    element: dict = {"type": "cds.String"}
    if length is not None:
        element["length"] = length
    if key:
        element["key"] = True
    if label:
        element["@EndUserText.label"] = label
    return element


def association(target: str, on: list, to_many: bool = False, label: str | None = None) -> dict:
    element = {
        "type": "cds.Association",
        "target": target,
        "cardinality": {"max": "*" if to_many else 1},
        "on": on,
    }
    if label:
        element["@EndUserText.label"] = label
    return element


def join(association_name: str, remote: str, local: str) -> list:
    return [{"ref": [association_name, remote]}, "=", {"ref": [local]}]


# ---------------------------------------------------------------------------
# Rule 1: DDIC domain -> ValueType + requires
# ---------------------------------------------------------------------------


class TestDomainsBecomeValueTypes:
    def test_fixed_values_become_a_closed_world_constraint(self):
        document = build(
            {
                "s.Category": {
                    "kind": "type",
                    "type": "cds.String",
                    "length": 1,
                    "@EndUserText.label": "Business Partner Category",
                    "enum": {
                        "Person": {"val": "1"},
                        "Organization": {"val": "2"},
                        "Group": {"val": "3"},
                    },
                },
                "s.E": entity({"Id": string_element(4, key=True, label="Identifier")}),
            }
        )
        value_type = concept(document, "BusinessPartnerCategory")
        assert value_type["type"] == "ValueType"
        assert value_type["extends"] == ["String"]
        assert value_type["requires"] == [
            "BusinessPartnerCategory == '1' OR BusinessPartnerCategory == '2' OR BusinessPartnerCategory == '3'"
        ]

    def test_fixed_value_order_follows_csn_declaration_order(self):
        document = build(
            {
                "s.Flag": {
                    "kind": "type",
                    "type": "cds.String",
                    "enum": {"B": {"val": "b"}, "A": {"val": "a"}},
                },
                "s.E": entity({"Id": string_element(4, key=True, label="Identifier")}),
            }
        )
        assert concept(document, "Flag")["requires"] == ["Flag == 'b' OR Flag == 'a'"]

    def test_numeric_fixed_values_are_not_quoted(self):
        document = build(
            {
                "s.Level": {
                    "kind": "type",
                    "type": "cds.Integer",
                    "enum": {"Low": {"val": 1}, "High": {"val": 2}},
                },
                "s.E": entity({"Id": string_element(4, key=True, label="Identifier")}),
            }
        )
        level = concept(document, "Level")
        assert level["extends"] == ["Integer"]
        assert level["requires"] == ["Level == 1 OR Level == 2"]

    def test_length_is_recorded_not_modeled(self):
        """The ontology expression language has no functions, so no LENGTH constraint."""
        from ossie_sap_csn_ontology import convert, parse_csn

        conversion = convert(
            parse_csn({"definitions": {"s.E": entity({"Id": string_element(10, key=True, label="Customer Number")})}})
        )
        document = csn_to_ossie({"definitions": {"s.E": entity({"Id": string_element(10, key=True, label="Customer Number")})}}, name="t")
        value_type = concept(document, "CustomerNumber")
        assert "requires" not in value_type
        assert "CSN length 10." in value_type["description"]
        assert any("cannot express a length constraint" in w for w in conversion.warnings)

    def test_value_type_is_named_from_the_end_user_label(self):
        document = build(
            {"s.E": entity({"Kunnr": string_element(10, key=True, label="Customer Number")})}
        )
        assert concept(document, "CustomerNumber")["extends"] == ["String"]

    def test_value_type_falls_back_to_the_element_name_without_a_label(self):
        document = build({"s.E": entity({"CustomerNumber": string_element(10, key=True)})})
        assert concept(document, "CustomerNumber")["type"] == "ValueType"

    def test_elements_sharing_a_domain_name_produce_one_value_type(self):
        document = build(
            {
                "s.A": entity({"Id": string_element(10, key=True, label="Customer Number")}),
                "s.B": entity({"Other": string_element(10, key=True, label="Customer Number")}),
            }
        )
        assert [c["concept"] for c in document["ontology"]].count("CustomerNumber") == 1

    def test_disagreeing_domains_of_the_same_name_are_reported(self):
        from ossie_sap_csn_ontology import convert, parse_csn

        conversion = convert(
            parse_csn(
                {
                    "definitions": {
                        "s.A": entity({"Id": string_element(10, key=True, label="Customer Number")}),
                        "s.B": entity({"Id": {"type": "cds.Integer", "key": True, "@EndUserText.label": "Customer Number"}}),
                    }
                }
            )
        )
        assert any("disagree" in warning for warning in conversion.warnings)

    def test_non_string_types_map_to_their_ossie_builtin(self):
        document = build(
            {
                "s.E": entity(
                    {
                        "Id": string_element(4, key=True, label="Identifier"),
                        "Amount": {"type": "cds.Decimal", "@EndUserText.label": "Net Amount"},
                        "Created": {"type": "cds.Date", "@EndUserText.label": "Creation Date"},
                    }
                )
            }
        )
        assert concept(document, "NetAmount")["extends"] == ["Decimal"]
        assert concept(document, "CreationDate")["extends"] == ["Date"]


# ---------------------------------------------------------------------------
# Rule 2: check table -> a mapped population, not an enumeration
# ---------------------------------------------------------------------------


class TestCheckTables:
    def test_structural_constraints_are_kept_and_values_are_not_enumerated(self):
        """A check-table domain keeps its shape constraint; its rows stay data."""
        document = build(
            {
                "s.E": entity(
                    {
                        "Id": string_element(4, key=True, label="Identifier"),
                        "Country": {
                            "type": "cds.String",
                            "length": 3,
                            "@EndUserText.label": "Country Key",
                            "$comment": "Domain LAND1, check table T005",
                        },
                    }
                )
            }
        )
        country = concept(document, "CountryKey")
        assert "requires" not in country, "a check table's rows must not become an enumeration"
        assert "CSN length 3." in country["description"]

    def test_the_check_table_origin_survives_in_the_description(self):
        document = build(
            {
                "s.E": entity(
                    {
                        "Id": string_element(4, key=True, label="Identifier"),
                        "Country": {
                            "type": "cds.String",
                            "length": 3,
                            "@EndUserText.label": "Country Key",
                            "$comment": "Domain LAND1, check table T005",
                        },
                    }
                )
            }
        )
        assert "T005" in concept(document, "CountryKey")["description"]


# ---------------------------------------------------------------------------
# Rule 3: simple identity -> identify_by + an identifying relationship
# ---------------------------------------------------------------------------


class TestSimpleIdentity:
    def test_a_single_key_element_becomes_a_one_to_one_identifying_relationship(self):
        document = build(
            {"s.Partner": entity({"Partner": string_element(10, key=True, label="Partner Number")})}
        )
        partner = concept(document, "Partner")
        assert partner["identify_by"] == ["partner"]
        assert relationship(document, "Partner", "partner")["multiplicity"] == "OneToOne"

    def test_identify_by_is_always_populated_because_csn_always_knows_its_keys(self):
        document = build(
            {"s.Partner": entity({"Partner": string_element(10, key=True, label="Partner Number")})}
        )
        assert concept(document, "Partner")["identify_by"]

    def test_an_identifying_relationship_verbalizes_in_both_directions(self):
        document = build(
            {"s.Partner": entity({"Partner": string_element(10, key=True, label="Partner Number")})}
        )
        assert relationship(document, "Partner", "partner")["verbalizes"] == [
            "{Partner} is identified by {PartnerNumber}",
            "{PartnerNumber} identifies {Partner}",
        ]

    def test_a_non_key_element_is_many_to_one(self):
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "PartnerName": string_element(80, label="Partner Name"),
                    }
                )
            }
        )
        assert relationship(document, "Partner", "name")["multiplicity"] == "ManyToOne"


# ---------------------------------------------------------------------------
# Rule 4: associations, with and without functional multiplicity
# ---------------------------------------------------------------------------


class TestAssociations:
    def _model(self, to_many: bool) -> dict:
        return {
            "s.Order": entity(
                {
                    "Order": string_element(10, key=True, label="Order Number"),
                    "_Items": association(
                        "s.Item", join("_Items", "Order", "Order"), to_many=to_many
                    ),
                }
            ),
            "s.Item": entity(
                {
                    "Item": string_element(6, key=True, label="Item Number"),
                    "Order": string_element(10, label="Order Number"),
                }
            ),
        }

    def test_to_many_association_has_no_functional_multiplicity(self):
        document = build(self._model(to_many=True))
        items = relationship(document, "Order", "has_items")
        assert "multiplicity" not in items
        assert items["roles"] == [{"concept": "Item"}]

    def test_to_many_association_verbalizes_in_both_directions(self):
        document = build(self._model(to_many=True))
        assert relationship(document, "Order", "has_items")["verbalizes"] == [
            "{Order} has many {Item}",
            "{Item} belongs to {Order}",
        ]

    def test_a_foreign_key_association_folds_into_one_relationship(self):
        """An association joined on the *target's* key absorbs the local foreign key."""
        document = build(
            {
                "s.Item": entity(
                    {
                        "Item": string_element(6, key=True, label="Item Number"),
                        "Order": string_element(10, label="Order Number"),
                        "_Order": association("s.Order", join("_Order", "Order", "Order")),
                    }
                ),
                "s.Order": entity({"Order": string_element(10, key=True, label="Order Number")}),
            }
        )
        names = [r["name"] for r in concept(document, "Item")["relationships"]]
        assert names == ["item", "order"], "the foreign key and its association must not both appear"
        order = relationship(document, "Item", "order")
        assert order["roles"] == [{"concept": "Order"}]
        assert order["multiplicity"] == "ManyToOne"

    def test_an_association_joined_on_the_source_key_points_at_the_target_identifier(self):
        """The role-view direction: the target is referenced by its identifier."""
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "_Customer": association("s.Customer", join("_Customer", "Partner", "Partner")),
                    }
                ),
                "s.Customer": entity(
                    {
                        "Customer": string_element(10, key=True, label="Customer Number"),
                        "Partner": string_element(10, label="Partner Number"),
                    }
                ),
            }
        )
        customer = relationship(document, "Partner", "customer")
        assert customer["roles"] == [{"concept": "CustomerNumber"}]
        assert customer["multiplicity"] == "ManyToOne"

    def test_a_managed_association_references_the_target_entity(self):
        """CAP generates a managed association's foreign key from the target's key."""
        document = build(
            {
                "s.Item": entity(
                    {
                        "Item": string_element(6, key=True, label="Item Number"),
                        "_Order": {
                            "type": "cds.Association",
                            "target": "s.Order",
                            "cardinality": {"max": 1},
                        },
                    }
                ),
                "s.Order": entity({"Order": string_element(10, key=True, label="Order Number")}),
            }
        )
        order = relationship(document, "Item", "order")
        assert order["roles"] == [{"concept": "Order"}]
        assert order["multiplicity"] == "ManyToOne"

    def test_a_managed_key_association_is_identifying(self):
        document = build(
            {
                "s.Item": entity(
                    {
                        "_Order": {
                            "key": True,
                            "type": "cds.Association",
                            "target": "s.Order",
                            "cardinality": {"max": 1},
                        },
                        "Item": string_element(6, key=True, label="Item Number"),
                    }
                ),
                "s.Order": entity({"Order": string_element(10, key=True, label="Order Number")}),
            }
        )
        assert concept(document, "Item")["identify_by"] == ["order", "item"]

    def test_an_association_to_a_missing_target_is_reported_not_guessed(self):
        from ossie_sap_csn_ontology import convert, parse_csn

        conversion = convert(
            parse_csn(
                {
                    "definitions": {
                        "s.Order": entity(
                            {
                                "Order": string_element(10, key=True, label="Order Number"),
                                "_Ghost": association("s.Missing", join("_Ghost", "Order", "Order")),
                            }
                        )
                    }
                }
            )
        )
        assert any("not \nin this CSN".replace("\n", "") in w for w in conversion.warnings)
        assert [r.name for r in conversion.concepts[0].relationships] == ["order"]


# ---------------------------------------------------------------------------
# Rule 5: compound identity -> several identifying relationships
# ---------------------------------------------------------------------------


class TestCompoundIdentity:
    COMPOUND = {
        "s.SalesArea": entity(
            {
                "Customer": string_element(10, key=True, label="Customer Number"),
                "SalesOrganization": string_element(4, key=True, label="Sales Organization"),
                "Division": string_element(2, key=True, label="Division"),
            }
        )
    }

    def test_every_key_part_becomes_an_identifying_relationship(self):
        document = build(self.COMPOUND)
        assert concept(document, "SalesArea")["identify_by"] == [
            "customer",
            "sales_organization",
            "division",
        ]

    def test_compound_key_parts_are_many_to_one_not_one_to_one(self):
        document = build(self.COMPOUND)
        multiplicities = {
            r["name"]: r["multiplicity"] for r in concept(document, "SalesArea")["relationships"]
        }
        assert set(multiplicities.values()) == {"ManyToOne"}

    def test_every_key_part_is_required(self):
        document = build(self.COMPOUND)
        assert concept(document, "SalesArea")["requires"] == [
            "SalesArea.customer",
            "SalesArea.sales_organization",
            "SalesArea.division",
        ]

    def test_a_single_key_needs_no_mandatory_constraint(self):
        """`identify_by` plus OneToOne already says the identifier exists."""
        document = build(
            {"s.Partner": entity({"Partner": string_element(10, key=True, label="Partner Number")})}
        )
        assert "requires" not in concept(document, "Partner")

    def test_identity_through_an_entity_becomes_a_nested_referent_mapping(self):
        document = build(
            {
                "s.Order": entity({"Order": string_element(10, key=True, label="Order Number")}),
                "s.Item": entity(
                    {
                        "Order": string_element(10, key=True, label="Order Number"),
                        "Item": string_element(6, key=True, label="Item Number"),
                        "_Order": association("s.Order", join("_Order", "Order", "Order")),
                    }
                ),
            },
            {
                "ontology_mappings": [
                    {
                        "semantic_model": {
                            "name": "m",
                            "datasets": [
                                {"name": "ITEMS", "fields": [{"name": "ORDER_ID"}, {"name": "ITEM_NO"}]}
                            ],
                        },
                        "concept_bindings": [
                            {
                                "concept": "Item",
                                "dataset": "ITEMS",
                                "identity": {"Order": "ORDER_ID", "Item": "ITEM_NO"},
                            }
                        ],
                    }
                ]
            },
        )
        referents = document["ontology_mappings"][0]["concept_mappings"][0]["object_mappings"][0][
            "referent_mappings"
        ]
        assert referents == [
            {
                "relationship": "order",
                "referent_mappings": [{"relationship": "order", "expression": "ITEMS.ORDER_ID"}],
            },
            {"relationship": "item", "expression": "ITEMS.ITEM_NO"},
        ]

    def test_an_inherited_identity_is_qualified_by_its_declaring_concept(self):
        """A role view's identity is declared on the parent, so it is qualified."""
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "_Customer": association("s.Customer", join("_Customer", "Partner", "Partner")),
                    }
                ),
                "s.Customer": entity(
                    {
                        "Customer": string_element(10, key=True, label="Customer Number"),
                        "Partner": string_element(10, label="Partner Number"),
                    }
                ),
                "s.SalesArea": entity(
                    {
                        "Customer": string_element(10, key=True, label="Customer Number"),
                        "Division": string_element(2, key=True, label="Division"),
                        "_Customer": association("s.Customer", join("_Customer", "Customer", "Customer")),
                    }
                ),
            },
            {
                "ontology_mappings": [
                    {
                        "semantic_model": {
                            "name": "m",
                            "datasets": [{"name": "SA", "fields": [{"name": "CUST"}, {"name": "DIV"}]}],
                        },
                        "concept_bindings": [
                            {
                                "concept": "SalesArea",
                                "dataset": "SA",
                                "identity": {"Customer": "CUST", "Division": "DIV"},
                            }
                        ],
                    }
                ],
            },
        )
        referents = document["ontology_mappings"][0]["concept_mappings"][0]["object_mappings"][0][
            "referent_mappings"
        ]
        assert referents[0] == {
            "relationship": "customer",
            "referent_mappings": [{"relationship": "Partner.partner", "expression": "SA.CUST"}],
        }

    def test_an_unbound_identity_part_is_an_error_not_a_silent_gap(self):
        with pytest.raises(ConversionError, match="identity binding is missing"):
            build(
                self.COMPOUND,
                {
                    "ontology_mappings": [
                        {
                            "semantic_model": {"name": "m", "datasets": []},
                            "concept_bindings": [
                                {
                                    "concept": "SalesArea",
                                    "dataset": "D",
                                    "identity": {"Customer": "C"},
                                }
                            ],
                        }
                    ]
                },
            )


# ---------------------------------------------------------------------------
# Rule 6: role-based specialization -> extends + derived_by
# ---------------------------------------------------------------------------


class TestRoleSubtypes:
    ROLES = {
        "s.Partner": entity(
            {
                "Partner": string_element(10, key=True, label="Partner Number"),
                "_Customer": association("s.Customer", join("_Customer", "Partner", "Partner")),
            }
        ),
        "s.Customer": entity(
            {
                "Customer": string_element(10, key=True, label="Customer Number"),
                "Partner": string_element(10, label="Partner Number"),
            }
        ),
    }

    def test_a_role_view_extends_its_parent(self):
        assert concept(build(self.ROLES), "Customer")["extends"] == ["Partner"]

    def test_a_role_view_is_derived_by_the_parent_carrying_the_role_key(self):
        assert concept(build(self.ROLES), "Customer")["derived_by"] == [
            "EXISTS ( Partner.customer )"
        ]

    def test_a_role_view_restates_its_parents_identifier(self):
        """The spec is silent on inheriting identify_by, so it is declared explicitly."""
        customer = concept(build(self.ROLES), "Customer")
        assert customer["identify_by"] == ["partner"]
        assert customer.get("relationships", []) == [], "no relationship is redeclared"

    def test_an_inverse_to_many_association_folds_into_the_foreign_key_relationship(self):
        """Customer._SalesArea and CustomerSalesArea._Customer are one link, read both ways."""
        document = build(
            {
                "s.Customer": entity(
                    {
                        "Customer": string_element(10, key=True, label="Customer Number"),
                        "_SalesArea": association(
                            "s.SalesArea", join("_SalesArea", "Customer", "Customer"), to_many=True
                        ),
                    }
                ),
                "s.SalesArea": entity(
                    {
                        "Customer": string_element(10, key=True, label="Customer Number"),
                        "Division": string_element(2, key=True, label="Division"),
                        "_Customer": association("s.Customer", join("_Customer", "Customer", "Customer")),
                    }
                ),
            }
        )
        assert [r["name"] for r in concept(document, "Customer")["relationships"]] == ["customer"], (
            "only the identifier remains; no has_sales_area is declared on the one side"
        )
        customer = relationship(document, "SalesArea", "customer")
        assert customer["roles"] == [{"concept": "Customer"}]
        assert customer["multiplicity"] == "ManyToOne"
        assert "{Customer} has many {SalesArea}" in customer["verbalizes"]

    def test_a_to_many_association_without_an_inverse_is_emitted_on_its_own(self):
        document = build(
            {
                "s.Order": entity(
                    {
                        "Order": string_element(10, key=True, label="Order Number"),
                        "_Items": association("s.Item", join("_Items", "Order", "Order"), to_many=True),
                    }
                ),
                "s.Item": entity(
                    {
                        "Item": string_element(6, key=True, label="Item Number"),
                        "Order": string_element(10, label="Order Number"),
                    }
                ),
            }
        )
        assert relationship(document, "Order", "has_items")["roles"] == [{"concept": "Item"}]

    def test_derived_by_follows_a_curated_rename_of_the_parent_relationship(self):
        document = build(
            self.ROLES,
            {"concepts": {"Partner": {"relationships": {"_Customer": {"rename": "customer_nr"}}}}},
        )
        assert concept(document, "Customer")["derived_by"] == ["EXISTS ( Partner.customer_nr )"]

    def test_a_plain_foreign_key_is_not_mistaken_for_a_role_view(self):
        document = build(
            {
                "s.Item": entity(
                    {
                        "Item": string_element(6, key=True, label="Item Number"),
                        "Order": string_element(10, label="Order Number"),
                        "_Order": association("s.Order", join("_Order", "Order", "Order")),
                    }
                ),
                "s.Order": entity({"Order": string_element(10, key=True, label="Order Number")}),
            }
        )
        assert "extends" not in concept(document, "Order")
        assert "derived_by" not in concept(document, "Order")


# ---------------------------------------------------------------------------
# Rule 7: labels and texts -> descriptions and verbalizations
# ---------------------------------------------------------------------------


class TestLabelsAndVerbalizations:
    def test_labels_and_comments_reach_the_description(self):
        document = build(
            {
                "s.Partner": entity(
                    {"Partner": string_element(10, key=True, label="Partner Number")},
                    label="Business Partner",
                )
            }
        )
        description = concept(document, "Partner")["description"]
        assert "Business Partner" in description and "CSN source: entity Partner" in description

    def test_every_relationship_verbalizes_even_without_curation(self):
        """`verbalizes` is required by the schema, so a template always fills it."""
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "Name": string_element(80, label="Partner Name"),
                    }
                )
            }
        )
        assert all(
            r["verbalizes"] for r in concept(document, "Partner")["relationships"]
        )

    def test_curated_verbalizations_replace_the_templates(self):
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "Name": string_element(80, label="Partner Name"),
                    }
                )
            },
            {
                "concepts": {
                    "Partner": {
                        "relationships": {"Name": {"verbalizes": ["{Partner} is named {PartnerName}"]}}
                    }
                }
            },
        )
        assert relationship(document, "Partner", "name")["verbalizes"] == [
            "{Partner} is named {PartnerName}"
        ]

    def test_a_null_curated_description_suppresses_the_derived_one(self):
        document = build(
            {
                "s.Partner": entity(
                    {
                        "Partner": string_element(10, key=True, label="Partner Number"),
                        "Name": string_element(80, label="Partner Name"),
                    }
                )
            },
            {"concepts": {"Partner": {"relationships": {"Name": {"description": None}}}}},
        )
        assert "description" not in relationship(document, "Partner", "name")

    def test_a_boolean_element_becomes_a_unary_relationship(self):
        """A flag is a proposition about the entity, not a value it has."""
        document = build(
            {
                "s.SalesArea": entity(
                    {
                        "Id": string_element(4, key=True, label="Identifier"),
                        "DeliveryIsBlocked": {
                            "type": "cds.Boolean",
                            "@EndUserText.label": "Delivery Block",
                        },
                    }
                )
            }
        )
        flag = relationship(document, "SalesArea", "delivery_is_blocked")
        assert "roles" not in flag
        assert "multiplicity" not in flag
        assert flag["verbalizes"] == ["{SalesArea} is flagged Delivery Block"]


# ---------------------------------------------------------------------------
# Rule 8: the mapping target is a data product
# ---------------------------------------------------------------------------


class TestOntologyMappings:
    MODEL = {"s.Partner": entity({"Partner": string_element(10, key=True, label="Partner Number")})}

    def _curation(self, **binding) -> dict:
        return {
            "ontology_mappings": [
                {
                    "name": "from_data_product",
                    "semantic_model": {
                        "name": "dp",
                        "datasets": [
                            {
                                "name": "BP",
                                "source": "catalog.schema.bp",
                                "primary_key": ["BP_NUMBER"],
                                "fields": [
                                    {"name": "BP_NUMBER", "datatype": "String"},
                                    {"name": "BP_NAME", "datatype": "String"},
                                ],
                            }
                        ],
                    },
                    "concept_bindings": [
                        {
                            "concept": "Partner",
                            "dataset": "BP",
                            "identity": {"Partner": "BP_NUMBER"},
                            **binding,
                        }
                    ],
                }
            ]
        }

    def test_bare_field_names_become_ansi_sql_dialect_expressions(self):
        document = build(self.MODEL, self._curation())
        field = document["ontology_mappings"][0]["semantic_model"]["datasets"][0]["fields"][0]
        assert field == {
            "name": "BP_NUMBER",
            "expression": {"dialects": [{"dialect": "ANSI_SQL", "expression": "BP_NUMBER"}]},
            "datatype": "String",
        }

    def test_dataset_source_and_primary_key_pass_through(self):
        dataset = build(self.MODEL, self._curation())["ontology_mappings"][0]["semantic_model"][
            "datasets"
        ][0]
        assert dataset["source"] == "catalog.schema.bp"
        assert dataset["primary_key"] == ["BP_NUMBER"]

    def test_identity_becomes_an_object_mapping(self):
        mapping = build(self.MODEL, self._curation())["ontology_mappings"][0]["concept_mappings"][0]
        assert mapping["object_mappings"] == [
            {"referent_mappings": [{"relationship": "partner", "expression": "BP.BP_NUMBER"}]}
        ]

    def test_attributes_become_link_mappings_with_bare_relationship_names(self):
        model = {
            "s.Partner": entity(
                {
                    "Partner": string_element(10, key=True, label="Partner Number"),
                    "Name": string_element(80, label="Partner Name"),
                }
            )
        }
        document = build(model, self._curation(attributes={"Name": "BP_NAME"}))
        link_mappings = document["ontology_mappings"][0]["concept_mappings"][0]["link_mappings"]
        assert link_mappings[0]["children"] == [
            {
                "relationship": "name",
                "object_mapping": {"expression": "BP.BP_NAME"},
            }
        ]

    def test_a_concept_without_attributes_emits_no_link_mappings(self):
        mapping = build(self.MODEL, self._curation())["ontology_mappings"][0]["concept_mappings"][0]
        assert "link_mappings" not in mapping

    def test_binding_an_unknown_concept_is_an_error(self):
        curation = self._curation()
        curation["ontology_mappings"][0]["concept_bindings"][0]["concept"] = "Nope"
        with pytest.raises(ConversionError, match="unknown concept"):
            build(self.MODEL, curation)

    def test_binding_an_unknown_attribute_is_an_error(self):
        with pytest.raises(ConversionError, match="does not expose as a relationship"):
            build(self.MODEL, self._curation(attributes={"Nope": "BP_NAME"}))


# ---------------------------------------------------------------------------
# Naming and curation mechanics
# ---------------------------------------------------------------------------


class TestNaming:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("Business Partner Number", "BusinessPartnerNumber"),
            ("Sales Organization", "SalesOrganization"),
            ("Country Key", "CountryKey"),
            ("BusinessPartner", "BusinessPartner"),
        ],
    )
    def test_pascal_case(self, label, expected):
        assert pascal_case(label) == expected

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("BusinessPartnerCategory", "business_partner_category"),
            ("DeliveryIsBlocked", "delivery_is_blocked"),
            ("Division", "division"),
            ("ODMEntity", "odm_entity"),
        ],
    )
    def test_snake_case(self, name, expected):
        assert snake_case(name) == expected

    def test_a_redundant_entity_prefix_is_dropped(self):
        assert strip_prefix("BusinessPartnerCategory", "BusinessPartner") == "Category"

    def test_an_element_named_like_its_entity_keeps_its_name(self):
        assert strip_prefix("BusinessPartner", "BusinessPartner") == "BusinessPartner"

    def test_a_prefix_is_only_dropped_at_a_word_boundary(self):
        assert strip_prefix("Ordering", "Order") == "Ordering"

    def test_association_names_lose_their_leading_underscore(self):
        assert relationship_name("_SalesArea", "Customer") == "sales_area"

    def test_to_many_associations_take_a_has_prefix(self):
        assert relationship_name("_SalesArea", "Customer", to_many=True) == "has_sales_area"


class TestCurationIntegrity:
    def test_an_unknown_overlay_key_is_rejected(self):
        with pytest.raises(CurationError, match="unknown key"):
            build(
                {"s.E": entity({"Id": string_element(4, key=True, label="Identifier")})},
                {"concepts": {"E": {"relationships": {"Id": {"renmae": "id"}}}}},
            )

    def test_an_unknown_top_level_section_is_rejected(self):
        with pytest.raises(CurationError, match="unknown key"):
            build(
                {"s.E": entity({"Id": string_element(4, key=True, label="Identifier")})},
                {"concpets": {}},
            )

    def test_an_overlay_entry_matching_nothing_is_reported(self):
        from ossie_sap_csn_ontology import convert, parse_csn
        from ossie_sap_csn_ontology.curation import parse_curation

        curation = parse_curation({"value_types": {"NoSuchDomain": {"rename": "X"}}})
        convert(parse_csn({"definitions": {"s.E": entity({"Id": string_element(4, key=True)})}}), curation)
        assert curation.unused_keys() == ["value_types.NoSuchDomain"]

    def test_a_model_without_a_name_is_an_error(self):
        from ossie_sap_csn_ontology import convert, parse_csn, render

        conversion = convert(parse_csn({"definitions": {"s.E": entity({"Id": string_element(4, key=True)})}}))
        with pytest.raises(ConversionError, match="no ontology name"):
            render(conversion)


class TestKnownLosses:
    def test_annotations_other_than_the_label_are_reported_dropped(self):
        """Nothing in the ontology layer carries them, so they are named, not lost."""
        from ossie_sap_csn_ontology import convert, parse_csn

        conversion = convert(
            parse_csn(
                {
                    "definitions": {
                        "s.E": {
                            "kind": "entity",
                            "@EndUserText.label": "Thing",
                            "@ODM.entityName": "Thing",
                            "elements": {
                                "Id": {
                                    "key": True,
                                    "type": "cds.String",
                                    "@EndUserText.label": "Identifier",
                                    "@Semantics.businessPartner.id": True,
                                }
                            },
                        }
                    }
                }
            )
        )
        assert conversion.dropped_annotations == ["s.E: @ODM.entityName", "s.E.Id: @Semantics.businessPartner.id"]
        assert any("dropped" in w for w in conversion.warnings)

    def test_the_label_is_not_reported_dropped(self):
        from ossie_sap_csn_ontology import convert, parse_csn

        conversion = convert(
            parse_csn({"definitions": {"s.E": entity({"Id": string_element(4, key=True, label="Identifier")}, label="E")}})
        )
        assert conversion.dropped_annotations == []


@pytest.fixture(scope="module")
def cds_types_document():
    import json

    from conftest import FIXTURES

    with open(FIXTURES / "cds-types.csn.json", encoding="utf-8") as handle:
        return csn_to_ossie(json.load(handle), name="cds_types")


class TestTypeFidelity:
    """What each CDS scalar type becomes; see docs/csn-mapping.md and the README."""

    @pytest.mark.parametrize(
        ("value_type", "builtin"),
        [
            ("DocumentNumber", "String"),   # cds.String(10)
            ("CreatedAt", "DateTime"),      # cds.DateTime: second precision
            ("ChangedAt", "DateTimeTz"),    # cds.Timestamp: microsecond precision, UTC
            ("LongText", "String"),         # cds.LargeString: unbounded
            ("NetAmount", "Decimal"),       # cds.Decimal(15, 2)
            ("PostingDate", "Date"),        # cds.Date
            ("ItemCount", "Integer"),       # cds.Integer (Int32)
        ],
    )
    def test_builtin(self, cds_types_document, value_type, builtin):
        assert concept(cds_types_document, value_type)["extends"] == [builtin]

    def test_precision_and_scale_are_not_modeled(self, cds_types_document):
        """No portable constraint form exists for them; they are a known loss."""
        assert "requires" not in concept(cds_types_document, "NetAmount")

    def test_string_length_is_recorded_in_the_description(self, cds_types_document):
        assert "CSN length 10." in concept(cds_types_document, "DocumentNumber")["description"]
        assert "requires" not in concept(cds_types_document, "DocumentNumber")
