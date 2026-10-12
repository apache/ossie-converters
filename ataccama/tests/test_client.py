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

"""Tests for the REST client's rate-limit handling (offline, with a fake session)."""

from __future__ import annotations

import warnings
from itertools import islice
from typing import Any

import pytest
import requests

from ataccama_ossie import client as client_module
from ataccama_ossie.client import MAX_RETRY_BACKOFF_S, AtaccamaClient
from ataccama_ossie.models import CatalogItem, CatalogItemBundle, CatalogLocation, TermAssignment

TOKEN_BODY = {"access_token": "token-1", "expires_in": 300}


class FakeResponse:
    def __init__(self, status_code: int, body: dict[str, Any] | None = None, headers: dict[str, str] | None = None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body or {}

    def json(self) -> dict[str, Any]:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)  # type: ignore[arg-type]


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self._responses = list(responses)
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append({"method": method, "url": url, **kwargs})
        return self._responses.pop(0)


def rate_limited(retry_after: str | None = None) -> FakeResponse:
    return FakeResponse(429, headers={"Retry-After": retry_after} if retry_after else {})


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(client_module.time, "sleep", recorded.append)
    return recorded


def make_client(responses: list[FakeResponse], **kwargs: Any) -> AtaccamaClient:
    return AtaccamaClient(
        base_url="https://example.test/api",
        token_url="https://example.test/token",
        client_id="id",
        client_secret="secret",
        session=FakeSession(responses),
        **kwargs,
    )


def test_retries_until_the_request_succeeds(sleeps: list[float]) -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            rate_limited(),
            rate_limited(),
            FakeResponse(200, {"urn": "item-1"}),
        ],
        retry_backoff_s=2.0,
    )

    assert client._get("/catalog/v1/catalog-items/item-1") == {"urn": "item-1"}
    assert sleeps == [2.0, 4.0]


def test_warns_about_every_retry(sleeps: list[float], capsys: pytest.CaptureFixture[str]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), rate_limited(), FakeResponse(200, {})],
        max_retries=3,
        retry_backoff_s=1.0,
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert capsys.readouterr().err == (
        "[WARNING] Rate limited by https://example.test/api/catalog/v1/catalog-items/item-1; "
        "retrying in 1.0s (retry 1 of 3)\n"
    )


def test_warns_when_giving_up(sleeps: list[float], capsys: pytest.CaptureFixture[str]) -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), rate_limited()], max_retries=0)

    with pytest.raises(requests.HTTPError):
        client._get("/catalog/v1/catalog-items/item-1")

    assert capsys.readouterr().err == (
        "[WARNING] Rate limited by https://example.test/api/catalog/v1/catalog-items/item-1; "
        "giving up after 0 retries\n"
    )


def test_honours_retry_after_header(sleeps: list[float]) -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), rate_limited("7"), FakeResponse(200, {})])

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [7.0]


def test_caps_retry_after_at_the_maximum_backoff(sleeps: list[float]) -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), rate_limited("3600"), FakeResponse(200, {})])

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [MAX_RETRY_BACKOFF_S]


def test_retry_after_date_in_the_past_retries_immediately(sleeps: list[float]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), rate_limited("Wed, 21 Oct 2015 07:28:00 GMT"), FakeResponse(200, {})]
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [0.0]


def test_retry_after_date_is_capped_at_the_maximum_backoff(sleeps: list[float]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), rate_limited("Fri, 31 Dec 9999 23:59:59 GMT"), FakeResponse(200, {})]
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [MAX_RETRY_BACKOFF_S]


@pytest.mark.parametrize("retry_after", ["soon", "nan", "inf", "-5", "1e3"])
def test_falls_back_to_backoff_for_an_invalid_retry_after(retry_after: str, sleeps: list[float]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), rate_limited(retry_after), FakeResponse(200, {})],
        retry_backoff_s=1.5,
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [1.5]


@pytest.mark.parametrize("status", [502, 503, 504])
def test_transient_gateway_errors_are_retried(status: int, sleeps: list[float]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), FakeResponse(status), FakeResponse(200, {"urn": "item-1"})],
        retry_backoff_s=2.0,
    )

    assert client._get("/catalog/v1/catalog-items/item-1") == {"urn": "item-1"}
    assert sleeps == [2.0]


def test_gateway_error_warning_names_the_status(sleeps: list[float], capsys: pytest.CaptureFixture[str]) -> None:
    # "Rate limited" would be a lie for a 503, so the warning reports what came back.
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), FakeResponse(503), FakeResponse(200, {})],
        max_retries=3,
        retry_backoff_s=1.0,
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert capsys.readouterr().err == (
        "[WARNING] HTTP 503 from https://example.test/api/catalog/v1/catalog-items/item-1; "
        "retrying in 1.0s (retry 1 of 3)\n"
    )


def test_gateway_error_honours_retry_after(sleeps: list[float]) -> None:
    # A 503 commonly carries Retry-After; it is respected as for a rate limit.
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), FakeResponse(503, headers={"Retry-After": "4"}), FakeResponse(200, {})]
    )

    client._get("/catalog/v1/catalog-items/item-1")

    assert sleeps == [4.0]


def test_gateway_error_is_raised_once_retries_run_out(sleeps: list[float]) -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(503)], max_retries=0)

    with pytest.raises(requests.HTTPError):
        client._get("/catalog/v1/catalog-items/item-1")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 500])
def test_other_errors_are_not_retried(status: int, sleeps: list[float]) -> None:
    # 500 is not transient the way a gateway error is; retrying it just repeats the fault.
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(status)])

    with pytest.raises(requests.HTTPError):
        client._get("/catalog/v1/catalog-items/item-1")
    assert sleeps == []


def test_a_gateway_error_does_not_look_like_absent_dq(sleeps: list[float]) -> None:
    # Only a 404 means "this item has no DQ"; an exhausted 503 must still raise.
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(503)], max_retries=0)

    with pytest.raises(requests.HTTPError):
        client.get_dq_results("item-1")


def test_a_gateway_error_mid_stream_is_retried(sleeps: list[float]) -> None:
    # The failure that aborted a real export: a page fetched part-way through paging.
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _items_page("customers", next_cursor="MQ==", total=2),
            FakeResponse(503),
            _items_page("orders", total=2),
        ],
        retry_backoff_s=1.0,
    )

    assert [i.name for i in client.find_catalog_items(source_urns=[SOURCE])] == ["customers", "orders"]
    assert sleeps == [1.0]


def test_raises_after_exhausting_retries(sleeps: list[float]) -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), rate_limited(), rate_limited(), rate_limited()],
        max_retries=2,
        retry_backoff_s=1.0,
    )

    with pytest.raises(requests.HTTPError):
        client._get("/catalog/v1/catalog-items/item-1")
    assert sleeps == [1.0, 2.0]


def test_rate_limited_dq_request_raises_rather_than_reporting_no_results(sleeps: list[float]) -> None:
    # Only a 404 means "this item has no DQ"; a rate-limited request is a real failure
    # and must not be mistaken for absent data.
    client = make_client([FakeResponse(200, TOKEN_BODY), rate_limited()], max_retries=0)

    with pytest.raises(requests.HTTPError):
        client.get_dq_results("item-1")


def test_token_request_is_retried(sleeps: list[float]) -> None:
    client = make_client(
        [rate_limited("5"), FakeResponse(200, TOKEN_BODY), FakeResponse(200, {"urn": "item-1"})],
        retry_backoff_s=1.0,
    )

    client._get("/catalog/v1/catalog-items/item-1")

    session: FakeSession = client._session  # type: ignore[assignment]
    assert [r["method"] for r in session.requests] == ["POST", "POST", "GET"]
    assert sleeps == [5.0]


def test_token_is_refreshed_between_retries(sleeps: list[float]) -> None:
    client = make_client(
        [
            FakeResponse(200, {"access_token": "token-1", "expires_in": 0}),
            rate_limited(),
            FakeResponse(200, {"access_token": "token-2", "expires_in": 300}),
            FakeResponse(200, {}),
        ],
    )

    client._get("/catalog/v1/catalog-items/item-1")

    session: FakeSession = client._session  # type: ignore[assignment]
    authorizations = [r["headers"]["Authorization"] for r in session.requests if r["method"] == "GET"]
    assert authorizations == ["Bearer token-1", "Bearer token-2"]


# --- primary keys ---


def _entity(urn: str, **properties: Any) -> dict[str, Any]:
    return {"urn": urn, "properties": properties}


def _page(*entities: dict[str, Any]) -> FakeResponse:
    return FakeResponse(200, {"data": list(entities)})


def test_primary_key_columns_are_ordered() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("pk-1", name="PK_ORDERS")),
            _page(_entity("c-2", name="line_no", order=2), _entity("c-1", name="order_id", order=1)),
        ]
    )

    assert client.get_primary_key("item-1") == {"name": "PK_ORDERS", "columns": ["order_id", "line_no"]}


def test_item_without_a_primary_key_reports_none() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _page()])

    assert client.get_primary_key("item-1") is None


def test_contradictory_primary_keys_keep_the_first_and_warn() -> None:
    # A table has at most one primary key, so more than one means the catalog metadata
    # contradicts itself -- the caller is told rather than left with a silent choice.
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("pk-1", name="PK_A"), _entity("pk-2", name="PK_B")),
            _page(_entity("c-1", name="id", order=1)),
            _page(_entity("c-2", name="alt_id", order=1)),
        ]
    )

    with pytest.warns(UserWarning, match=r"item-1 reports 2 primary keys \(PK_A, PK_B\); using 'PK_A'"):
        assert client.get_primary_key("item-1") == {"name": "PK_A", "columns": ["id"]}


# --- unique indexes ---


def test_unique_index_columns_are_ordered() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("ix-1", name="UX_TENANT_EMAIL", unique=True)),
            _page(
                _entity("c-2", name="email", unique=True, order=2),
                _entity("c-1", name="tenant_id", unique=True, order=1),
            ),
        ]
    )

    assert client.get_unique_indexes("item-1") == [{"name": "UX_TENANT_EMAIL", "columns": ["tenant_id", "email"]}]


def test_included_columns_are_left_out_of_the_key() -> None:
    # A unique index may also carry covering columns, which it does not enforce
    # uniqueness on; taking them would widen the key beyond the real constraint.
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("ix-1", name="UX_EMAIL", unique=True)),
            _page(
                _entity("c-1", name="email", unique=True, order=1),
                _entity("c-2", name="display_name", unique=False, order=2),
            ),
        ]
    )

    assert client.get_unique_indexes("item-1") == [{"name": "UX_EMAIL", "columns": ["email"]}]


def test_non_unique_indexes_are_skipped_without_fetching_columns() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _page(_entity("ix-1", name="IX_CREATED", unique=False))])

    assert client.get_unique_indexes("item-1") == []


def test_unique_indexes_are_sorted_by_name() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("ix-2", name="UX_B", unique=True), _entity("ix-1", name="UX_A", unique=True)),
            _page(_entity("c-1", name="b", unique=True, order=1)),
            _page(_entity("c-2", name="a", unique=True, order=1)),
        ]
    )

    assert [i["name"] for i in client.get_unique_indexes("item-1")] == ["UX_A", "UX_B"]


# --- foreign keys ---


def _foreign_key_column(urn: str, order: int, **properties: Any) -> dict[str, Any]:
    return _entity(urn, order=order, **properties)


def test_foreign_key_columns_are_ordered_and_paired() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_LINES_ORDERS")),
            _page(
                _foreign_key_column(
                    "c-2",
                    2,
                    name="line_no",
                    referencedSchema="sales",
                    referencedTableName="orders",
                    referencedColumnName="line",
                ),
                _foreign_key_column(
                    "c-1",
                    1,
                    name="order_id",
                    referencedSchema="sales",
                    referencedTableName="orders",
                    referencedColumnName="id",
                ),
            ),
        ]
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        foreign_keys = client.get_foreign_keys("item-1")

    assert foreign_keys == [
        {
            "name": "FK_LINES_ORDERS",
            "columns": ["order_id", "line_no"],
            "referenced_schema": "sales",
            "referenced_table": "orders",
            "referenced_columns": ["id", "line"],
        }
    ]


def test_foreign_key_rows_missing_either_side_are_dropped_without_misaligning_the_rest() -> None:
    # Filtering local and referenced names separately would pair c with y here and still
    # pass the equal-length check downstream; dropping whole rows keeps the pairs intact.
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_ABC")),
            _page(
                _foreign_key_column("c-1", 1, name="a", referencedTableName="target", referencedColumnName="x"),
                _foreign_key_column("c-2", 2, referencedTableName="target", referencedColumnName="y"),
                _foreign_key_column("c-3", 3, name="c", referencedTableName="target"),
            ),
        ]
    )

    with pytest.warns(UserWarning, match=r"Invalid reference pair for foreign key FK_ABC") as recorded:
        foreign_keys = client.get_foreign_keys("item-1")

    assert len(recorded) == 2
    assert foreign_keys[0]["columns"] == ["a"]
    assert foreign_keys[0]["referenced_columns"] == ["x"]


def test_foreign_key_referencing_several_tables_has_no_referenced_table() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_MIXED")),
            _page(
                _foreign_key_column("c-1", 1, name="a", referencedTableName="orders", referencedColumnName="id"),
                _foreign_key_column("c-2", 2, name="b", referencedTableName="invoices", referencedColumnName="id"),
            ),
        ]
    )

    with pytest.warns(
        UserWarning, match=r"Inconsistent referenced table for foreign key FK_MIXED of catalog item item-1"
    ):
        foreign_keys = client.get_foreign_keys("item-1")

    assert (foreign_keys[0]["referenced_schema"], foreign_keys[0]["referenced_table"]) == (None, None)


def test_foreign_key_without_a_referenced_table_has_none() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_DANGLING")),
            _page(_foreign_key_column("c-1", 1, name="a", referencedColumnName="id")),
        ]
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        foreign_keys = client.get_foreign_keys("item-1")

    assert foreign_keys[0]["referenced_table"] is None


def test_foreign_key_without_a_referenced_schema_has_none() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_NO_SCHEMA")),
            _page(_foreign_key_column("c-1", 1, name="a", referencedTableName="orders", referencedColumnName="id")),
        ]
    )

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        foreign_keys = client.get_foreign_keys("item-1")

    assert foreign_keys[0]["referenced_schema"] is None


@pytest.mark.parametrize("second_schema", ["archive", None], ids=["another schema", "no schema"])
def test_foreign_key_columns_disagreeing_on_the_schema_have_no_referenced_table(second_schema: str | None) -> None:
    second_column_schema = {"referencedSchema": second_schema} if second_schema else {}
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(_entity("fk-1", name="FK_MIXED_SCHEMAS")),
            _page(
                _foreign_key_column(
                    "c-1",
                    1,
                    name="a",
                    referencedSchema="sales",
                    referencedTableName="orders",
                    referencedColumnName="id",
                ),
                _foreign_key_column(
                    "c-2",
                    2,
                    name="b",
                    referencedTableName="orders",
                    referencedColumnName="line",
                    **second_column_schema,
                ),
            ),
        ]
    )

    with pytest.warns(UserWarning, match=r"Inconsistent referenced table for foreign key FK_MIXED_SCHEMAS"):
        foreign_keys = client.get_foreign_keys("item-1")

    assert (foreign_keys[0]["referenced_schema"], foreign_keys[0]["referenced_table"]) == (None, None)


# --- locations ---


def _location_entity(urn: str, concrete_type: str) -> FakeResponse:
    return FakeResponse(200, {"urn": urn, "rootSuperType": "location", "concreteType": concrete_type})


def _item_in(*locations: CatalogLocation) -> CatalogItem:
    return CatalogItem(urn="item-1", name="orders", locations=list(locations))


SALES_SCHEMA = CatalogLocation(name="sales", urn="urn:ata:t:catalog:location:sales")
SHOP_DATABASE = CatalogLocation(name="shop", urn="urn:ata:t:catalog:location:shop")


def test_schema_name_is_the_schema_location_among_the_item_locations() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _location_entity(SHOP_DATABASE.urn, "databaseLocation"),
            _location_entity(SALES_SCHEMA.urn, "schemaLocation"),
        ]
    )

    assert client.get_schema_name(_item_in(SHOP_DATABASE, SALES_SCHEMA)) == "sales"

    session: FakeSession = client._session  # type: ignore[assignment]
    assert [r["url"] for r in session.requests if r["method"] == "GET"] == [
        f"https://example.test/api/catalog/v1/entities/{SHOP_DATABASE.urn}",
        f"https://example.test/api/catalog/v1/entities/{SALES_SCHEMA.urn}",
    ]


def test_item_outside_any_schema_has_no_schema_name() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _location_entity(SHOP_DATABASE.urn, "databaseLocation")])

    assert client.get_schema_name(_item_in(SHOP_DATABASE)) is None


def test_each_location_type_is_looked_up_once() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _location_entity(SALES_SCHEMA.urn, "schemaLocation")])

    assert client.get_schema_name(_item_in(SALES_SCHEMA)) == "sales"
    assert client.get_schema_name(_item_in(SALES_SCHEMA)) == "sales"

    session: FakeSession = client._session  # type: ignore[assignment]
    assert len([r for r in session.requests if r["method"] == "GET"]) == 1


def test_bundle_without_relationships_skips_location_lookups() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _page(), FakeResponse(404)])  # token, attributes, dq

    bundle = client.fetch_bundle_for(_item_in(SALES_SCHEMA), with_relationships=False)

    assert bundle.schema_name is None
    session: FakeSession = client._session  # type: ignore[assignment]
    assert not any("/catalog/v1/entities/" in r["url"] for r in session.requests)


# --- filtering catalog items ---

CONNECTION = "urn:ata:t:processing:connection:c1"
SOURCE = "urn:ata:t:catalog:source:s1"
LOCATION = "urn:ata:t:catalog:location:l1"


def _items_page(*names: str, next_cursor: str | None = None, total: int | None = None) -> FakeResponse:
    data = [{"urn": f"urn:ata:t:catalog:catalog-item:{n}", "name": n} for n in names]
    meta: dict[str, Any] = {"total": len(names) if total is None else total}
    if next_cursor:
        meta["next"] = next_cursor
    return FakeResponse(200, {"data": data, "meta": meta})


def _filter_request(client: AtaccamaClient) -> dict[str, Any]:
    session: FakeSession = client._session  # type: ignore[assignment]
    return next(r for r in session.requests if r["method"] == "POST" and "filter" in r["url"])


def test_filtering_by_one_criterion_sends_a_bare_clause() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page("customers")])

    assert [i.name for i in client.find_catalog_items(source_urns=[SOURCE])] == ["customers"]
    assert _filter_request(client)["json"] == {"filter": {"source": {"urn": {"in": [SOURCE]}}}}


def test_several_criteria_are_combined_with_or() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page("customers")])

    list(
        client.find_catalog_items(
            catalog_item_urns=["urn:ata:t:catalog:catalog-item:a"],
            connection_urns=[CONNECTION],
            location_urns=[LOCATION],
        )
    )

    assert _filter_request(client)["json"] == {
        "filter": {
            "or": [
                {"urn": {"in": ["urn:ata:t:catalog:catalog-item:a"]}},
                {"connection": {"urn": {"in": [CONNECTION]}}},
                {"locations": {"any": {"urn": {"in": [LOCATION]}}}},
            ]
        }
    }


def test_several_urns_of_one_kind_become_a_single_in_list() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page("customers")])

    list(client.find_catalog_items(source_urns=[SOURCE, "urn:ata:t:catalog:source:s2"]))

    assert _filter_request(client)["json"] == {
        "filter": {"source": {"urn": {"in": [SOURCE, "urn:ata:t:catalog:source:s2"]}}}
    }


def test_filter_request_asks_for_the_largest_page_the_endpoint_allows() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page("customers")])

    list(client.find_catalog_items(source_urns=[SOURCE]))

    request = _filter_request(client)
    assert request["url"].endswith("/catalog/v1/catalog-items/filter")
    assert request["params"] == {"include": "effectiveStewardship", "size": 100}


def test_attribute_and_entity_listings_ask_for_the_largest_page_the_api_allows() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _page(), _page()])

    client.get_attributes("item-1")
    client.get_primary_key("item-1")

    session: FakeSession = client._session  # type: ignore[assignment]
    assert [r["params"]["size"] for r in session.requests if r["method"] == "GET"] == [100, 100]


def test_filtering_follows_the_cursor_across_pages() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _items_page("customers", next_cursor="MQ==", total=2),
            _items_page("orders", total=2),
        ]
    )

    assert [i.name for i in client.find_catalog_items(source_urns=[SOURCE])] == ["customers", "orders"]


def test_the_total_is_known_before_any_item_is_consumed() -> None:
    # The count comes from the server, so a caller can size the run up front rather than
    # gathering every item to find out how many there are.
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page("customers", "orders", total=57)])

    results = client.find_catalog_items(source_urns=[SOURCE])

    assert results.total == 57


def test_only_the_first_page_is_read_up_front() -> None:
    # Page one is what carries the total; the rest wait until they are consumed, so
    # enrichment can start without paging the whole catalog first.
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _items_page("customers", "orders", next_cursor="MQ==", total=3),
            _items_page("invoices", total=3),
        ]
    )

    results = client.find_catalog_items(source_urns=[SOURCE])

    session: FakeSession = client._session  # type: ignore[assignment]
    assert len([r for r in session.requests if "filter" in r["url"]]) == 1
    assert [i.name for i in islice(results, 2)] == ["customers", "orders"]
    assert len([r for r in session.requests if "filter" in r["url"]]) == 1


def test_an_empty_result_reports_a_total_of_zero() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), _items_page()])

    results = client.find_catalog_items(source_urns=[SOURCE])

    assert results.total == 0
    assert list(results) == []


def test_filtering_without_any_urn_is_refused_without_a_request() -> None:
    client = make_client([])

    with pytest.raises(ValueError, match="at least one"):
        client.find_catalog_items()

    session: FakeSession = client._session  # type: ignore[assignment]
    assert session.requests == []


def test_stewardship_is_requested_when_reading_one_catalog_item() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(200, {"urn": "item-1", "name": "customers"})])

    client.get_catalog_item("item-1")

    session: FakeSession = client._session  # type: ignore[assignment]
    request = next(r for r in session.requests if r["method"] == "GET")
    assert request["params"] == {"include": "effectiveStewardship"}


def test_enriching_an_already_fetched_item_skips_the_catalog_item_request() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            _page(),  # attributes
            FakeResponse(404),  # dq results
            _page(),  # primary keys
            _page(),  # indexes
            _page(),  # foreign keys
        ]
    )

    bundle = client.fetch_bundle_for(CatalogItem(urn="item-1", name="customers"))

    assert bundle.item.name == "customers"
    session: FakeSession = client._session  # type: ignore[assignment]
    assert not any(r["url"].endswith("/catalog-items/item-1") for r in session.requests)


# --- data quality ---


def test_dq_results_use_the_dq_evaluation_resource() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(200, {"workflowUrn": "urn:w"})])

    assert client.get_dq_results("item-1") == {"workflowUrn": "urn:w"}

    session: FakeSession = client._session  # type: ignore[assignment]
    url = next(r["url"] for r in session.requests if r["method"] == "GET")
    assert url.endswith("/dq-monitors/primary/processings/latest/results/dq-evaluation")


def test_item_without_dq_reports_none() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(404)])

    assert client.get_dq_results("item-1") is None


def test_dq_server_error_is_not_mistaken_for_absent_dq() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(500)])

    with pytest.raises(requests.HTTPError):
        client.get_dq_results("item-1")


def _threshold(value: int, severity: str = "WARNING") -> dict[str, Any]:
    return {"urn": f"urn:ata:t:data-quality:dq-threshold:{value}", "value": value, "severity": severity}


def test_overall_threshold_is_read_from_the_keyed_map() -> None:
    client = make_client(
        [FakeResponse(200, TOKEN_BODY), FakeResponse(200, {"overallDqThresholds": {"a-uuid": _threshold(80)}})]
    )

    assert client.get_dq_overall_threshold("item-1") == 80


def test_critical_threshold_wins_over_a_stricter_warning() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            FakeResponse(
                200,
                {"overallDqThresholds": {"a": _threshold(90), "b": _threshold(60, "CRITICAL")}},
            ),
        ]
    )

    assert client.get_dq_overall_threshold("item-1") == 60


def test_strictest_threshold_wins_when_no_critical_one_exists() -> None:
    client = make_client(
        [
            FakeResponse(200, TOKEN_BODY),
            FakeResponse(200, {"overallDqThresholds": {"a": _threshold(75), "b": _threshold(90)}}),
        ]
    )

    assert client.get_dq_overall_threshold("item-1") == 90


def test_monitor_without_thresholds_reports_none() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(200, {})])

    assert client.get_dq_overall_threshold("item-1") is None


def test_missing_monitor_reports_no_threshold() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(404)])

    assert client.get_dq_overall_threshold("item-1") is None


def test_threshold_server_error_is_not_mistaken_for_an_unset_threshold() -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), FakeResponse(500)])

    with pytest.raises(requests.HTTPError):
        client.get_dq_overall_threshold("item-1")


# --- terms ---


def _fetch_item_with_term(term_response: FakeResponse) -> CatalogItemBundle:
    client = make_client([FakeResponse(200, TOKEN_BODY), _page(), term_response])  # token, attributes, term
    item = CatalogItem(urn="item-1", name="customers", term_assignments=[TermAssignment(term_urn="term-1")])
    return client.fetch_bundle_for(item, with_dq=False, with_relationships=False)


@pytest.mark.parametrize("status", [403, 404])
def test_inaccessible_term_is_skipped_with_a_warning(status: int) -> None:
    with pytest.warns(UserWarning, match=rf"Term term-1 is inaccessible \(status code {status}\)"):
        bundle = _fetch_item_with_term(FakeResponse(status))

    assert bundle.terms == {}


def test_term_server_error_fails_the_fetch() -> None:
    with pytest.raises(requests.HTTPError):
        _fetch_item_with_term(FakeResponse(500))
