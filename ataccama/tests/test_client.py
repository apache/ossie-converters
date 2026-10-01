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

from typing import Any

import pytest
import requests

from ataccama_ossie import client as client_module
from ataccama_ossie.client import MAX_RETRY_BACKOFF_S, AtaccamaClient

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
