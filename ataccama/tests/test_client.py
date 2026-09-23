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


def test_rate_limited_requests_report_no_results(sleeps: list[float]) -> None:
    client = make_client([FakeResponse(200, TOKEN_BODY), rate_limited()], max_retries=0)

    assert client.get_dq_results("item-1") is None


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
