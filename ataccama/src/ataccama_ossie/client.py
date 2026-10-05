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

"""Live REST client for the Ataccama ONE public API.

Handles OAuth2 client-credentials auth (tokens are short-lived, ~5 min, so we mint
and refresh them automatically), cursor-based pagination (``after``/``size``), and
retries while the API rate-limits us or answers with a transient gateway error.
"""

from __future__ import annotations

import sys
import time
import warnings
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from itertools import chain
from typing import Any

import requests
from urllib3.exceptions import InvalidHeader
from urllib3.util.retry import Retry

from ataccama_ossie.models import CatalogAttribute, CatalogItem, CatalogItemBundle, Term

# The API caps every listing endpoint at this many records per page.
MAX_PAGE_SIZE = 100
# Stewardship is resolved only on request, and the converter emits it when present.
CATALOG_ITEM_INCLUDES = "effectiveStewardship"
# Refresh the token this many seconds before it actually expires.
TOKEN_EXPIRY_SKEW_S = 30
NOT_FOUND = 404
TOO_MANY_REQUESTS = 429
RETRYABLE_STATUSES = frozenset({TOO_MANY_REQUESTS, 502, 503, 504})
DEFAULT_MAX_RETRIES = 5
DEFAULT_RETRY_BACKOFF_S = 1.0
MAX_RETRY_BACKOFF_S = 60.0


def _requested_retry_delay(retry_after: str | None) -> float | None:
    """The delay a Retry-After header asks for, or None if it is absent or invalid."""
    if retry_after is None:
        return None
    try:
        return Retry().parse_retry_after(retry_after)
    except InvalidHeader:
        return None


@dataclass
class CatalogItemResults:
    """A filter's matches: how many there are, and the items themselves as they arrive.

    ``total`` is the server's own count across every page, so a caller knows the size of
    the run before enriching anything, without having to gather the items first.
    """

    total: int
    items: Iterator[CatalogItem]

    def __iter__(self) -> Iterator[CatalogItem]:
        return self.items


def _retry_reason(status_code: int) -> str:
    """How to describe a retryable response, so the warning says what actually happened."""
    return "Rate limited by" if status_code == TOO_MANY_REQUESTS else f"HTTP {status_code} from"


def _catalog_items(pages: Iterator[dict[str, Any]]) -> Iterator[CatalogItem]:
    for page in pages:
        for record in page.get("data", []):
            yield CatalogItem.from_dict(record)


def _catalog_item_filter_criteria(
    *,
    catalog_item_urns: Sequence[str],
    connection_urns: Sequence[str],
    source_urns: Sequence[str],
    location_urns: Sequence[str],
) -> list[dict[str, Any]]:
    """One ``CatalogItemFilter`` clause per populated group, ready to be OR-ed.

    ``locations.any`` matches anywhere in the hierarchy, so a location clause covers the
    whole subtree rather than only the items directly under it.
    """
    groups = [
        ({"urn": {"in": list(catalog_item_urns)}}, catalog_item_urns),
        ({"connection": {"urn": {"in": list(connection_urns)}}}, connection_urns),
        ({"source": {"urn": {"in": list(source_urns)}}}, source_urns),
        ({"locations": {"any": {"urn": {"in": list(location_urns)}}}}, location_urns),
    ]
    return [clause for clause, urns in groups if urns]


class AtaccamaClient:
    """Minimal client for the endpoints the Ossie importer needs."""

    def __init__(
        self,
        base_url: str,
        token_url: str,
        client_id: str,
        client_secret: str,
        *,
        timeout: float = 30.0,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
        session: requests.Session | None = None,
    ) -> None:
        # base_url is the API root, e.g. https://<host>/api
        self.base_url = base_url.rstrip("/")
        self.token_url = token_url
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_s = retry_backoff_s
        self._session = session or requests.Session()
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # --- auth ---

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - TOKEN_EXPIRY_SKEW_S:
            return self._token
        resp = self._request(
            "POST",
            self.token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            authenticate=False,
        )
        payload = resp.json()
        token = payload["access_token"]
        self._token = token
        self._token_expiry = time.time() + float(payload.get("expires_in", 300))
        return token

    # --- low-level HTTP ---

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        json_body: Any | None = None,
        headers: dict[str, str] | None = None,
        authenticate: bool = True,
    ) -> requests.Response:
        """Send a request, retrying while the API rate-limits us (HTTP 429)."""
        attempt = 0
        while True:
            request_headers = dict(headers or {})
            if authenticate:
                # Minted per attempt: a token can expire while we back off.
                request_headers["Authorization"] = f"Bearer {self._access_token()}"
            resp = self._session.request(
                method,
                url,
                params=params,
                data=data,
                json=json_body,
                headers=request_headers,
                timeout=self.timeout,
            )
            if resp.status_code not in RETRYABLE_STATUSES or attempt >= self.max_retries:
                if resp.status_code in RETRYABLE_STATUSES:
                    print(
                        f"[WARNING] {_retry_reason(resp.status_code)} {url}; "
                        f"giving up after {self.max_retries} retries",
                        file=sys.stderr,
                    )
                resp.raise_for_status()
                return resp
            delay = self._retry_delay(resp, attempt=attempt)
            attempt += 1
            print(
                f"[WARNING] {_retry_reason(resp.status_code)} {url}; "
                f"retrying in {delay:.1f}s (retry {attempt} of {self.max_retries})",
                file=sys.stderr,
            )
            time.sleep(delay)

    def _retry_delay(self, resp: requests.Response, *, attempt: int) -> float:
        """Seconds to wait before retrying: the server's Retry-After, else exponential backoff."""
        requested_delay = _requested_retry_delay(resp.headers.get("Retry-After"))
        if requested_delay is None:
            requested_delay = self.retry_backoff_s * 2**attempt
        return min(requested_delay, MAX_RETRY_BACKOFF_S)

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("GET", f"{self.base_url}{path}", params=params).json()

    def _post(self, path: str, json_body: Any, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self._request("POST", f"{self.base_url}{path}", params=params, json_body=json_body).json()

    def _pages(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        json_body: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield each raw page, following the ``meta.next`` cursor.

        Pages carry ``meta.total`` alongside their records, which callers that need the
        size of the whole result read off the first one.

        A ``json_body`` switches the pages from GET to POST, as the filter endpoints
        expect; the cursor stays in the query string either way.
        """
        params = dict(params or {})
        params.setdefault("size", MAX_PAGE_SIZE)
        after: str | None = None
        while True:
            if after is not None:
                params["after"] = after
            page = self._post(path, json_body, params) if json_body is not None else self._get(path, params)
            yield page
            after = (page.get("meta") or {}).get("next")
            if not after:
                break

    def _paged(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        json_body: Any | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Iterate every record across pages."""
        for page in self._pages(path, params, json_body=json_body):
            yield from page.get("data", [])

    # --- catalog endpoints ---

    def get_catalog_item(self, urn: str) -> CatalogItem:
        return CatalogItem.from_dict(self._get(f"/catalog/v1/catalog-items/{urn}", {"include": CATALOG_ITEM_INCLUDES}))

    def find_catalog_items(
        self,
        *,
        catalog_item_urns: Sequence[str] = (),
        connection_urns: Sequence[str] = (),
        source_urns: Sequence[str] = (),
        location_urns: Sequence[str] = (),
    ) -> CatalogItemResults:
        """The catalog items matching any of the given URNs, connections, sources or locations.

        Every group is a filter criterion and they are OR-ed, so each one widens the result
        and a URN that matches nothing simply contributes nothing. Location matching covers
        the whole subtree, meaning one schema or database URN brings in every item beneath it.

        Items come back whole, exactly as ``get_catalog_item`` returns them, so callers can
        hand them straight to ``fetch_bundle_for`` without a second request per item.

        Only the first page is read here -- enough to learn ``total`` -- and the rest are
        fetched as they are consumed, so a caller that stops early, or that enriches each
        item as it arrives, never pays for the rest of the catalog.
        """
        criteria = _catalog_item_filter_criteria(
            catalog_item_urns=catalog_item_urns,
            connection_urns=connection_urns,
            source_urns=source_urns,
            location_urns=location_urns,
        )
        if not criteria:
            raise ValueError("at least one catalog-item, connection, source, or location URN is required")
        pages = self._pages(
            "/catalog/v1/catalog-items/filter",
            {"include": CATALOG_ITEM_INCLUDES},
            json_body={"filter": criteria[0] if len(criteria) == 1 else {"or": criteria}},
        )
        first = next(pages, None) or {}
        total = (first.get("meta") or {}).get("total") or 0
        return CatalogItemResults(total=total, items=_catalog_items(chain([first], pages)))

    def get_attributes(self, catalog_item_urn: str) -> list[CatalogAttribute]:
        return [
            CatalogAttribute.from_dict(a)
            for a in self._paged("/catalog/v1/attributes", {"catalogItemUrn": catalog_item_urn})
        ]

    def get_term(self, urn: str) -> Term:
        return Term.from_dict(self._get(f"/catalog/v1/terms/{urn}"))

    # --- data-quality endpoint ---

    def get_dq_results(
        self, catalog_item_urn: str, monitor: str = "primary", processing: str = "latest"
    ) -> dict | None:
        """Latest DQ results for a catalog item's primary monitor.

        Returns the raw ``DqEvaluationResults`` payload (overall + per-dimension +
        per-attribute quality), or ``None`` if the item has no monitor / published results.
        """
        path = (
            f"/data-quality/v1/catalog-items/{catalog_item_urn}"
            f"/dq-monitors/{monitor}/processings/{processing}/results/dq-evaluation"
        )
        try:
            return self._get(path)
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == NOT_FOUND:
                return None
            raise

    def get_dq_overall_threshold(self, catalog_item_urn: str, monitor: str = "primary") -> float | None:
        """The monitor's configured overall DQ threshold (0-100), or None if unset.

        This is the pass/fail bar shown in Ataccama, and is only available from the
        monitor-config endpoint (not the DQ results payload).

        A monitor can carry several thresholds at different severities, keyed by
        threshold id. The DQ block reports one, so CRITICAL wins and otherwise the
        strictest bar does, leaving the reported verdict unable to understate.
        """
        try:
            mon = self._get(f"/data-quality/v1/catalog-items/{catalog_item_urn}/dq-monitors/{monitor}")
        except requests.HTTPError:
            return None
        thresholds = [
            (threshold.get("severity") == "CRITICAL", threshold["value"])
            for threshold in (mon.get("overallDqThresholds") or {}).values()
            if isinstance(threshold, dict) and threshold.get("value") is not None
        ]
        return max(thresholds)[1] if thresholds else None

    # --- generic metadata entities (keys & relationships) ---

    def _entities_by_parent(self, entity_type: str, parent_urn: str, properties: str) -> list[dict]:
        """List entities of a type filtered to a parent, via the Generic Metadata Entities API."""
        return list(
            self._paged(
                "/catalog/v1/entities",
                {"entityType": entity_type, "parentUrn": parent_urn, "properties": properties},
            )
        )

    def get_primary_key(self, catalog_item_urn: str) -> dict | None:
        """The primary key of a catalog item: {"name", "columns": [ordered column names]}."""
        keys: list[dict] = []
        for pk in self._entities_by_parent("primaryKey", catalog_item_urn, "name"):
            cols = self._entities_by_parent("primaryKeyColumn", pk["urn"], "name,order")
            cols.sort(key=lambda c: c.get("properties", {}).get("order") or 0)
            names = [c["properties"].get("name") for c in cols if c.get("properties", {}).get("name")]
            if names:
                keys.append({"name": pk.get("properties", {}).get("name"), "columns": names})
        if len(keys) > 1:
            warnings.warn(
                f"{catalog_item_urn} reports {len(keys)} primary keys "
                f"({', '.join(str(k['name']) for k in keys)}); using {keys[0]['name']!r}",
                stacklevel=2,
            )
        return keys[0] if keys else None

    def get_unique_indexes(self, catalog_item_urn: str) -> list[dict]:
        """Unique indexes of a catalog item: [{"name", "columns": [ordered column names]}].

        Only the columns marked unique are taken: a unique index may also carry included
        (covering) columns, which are stored in the index but excluded from what it
        enforces uniqueness on.
        """
        result: list[dict] = []
        for index in self._entities_by_parent("index", catalog_item_urn, "name,unique"):
            if not index.get("properties", {}).get("unique"):
                continue
            cols = self._entities_by_parent("indexColumn", index["urn"], "name,unique,order")
            cols.sort(key=lambda c: c.get("properties", {}).get("order") or 0)
            names = [
                c["properties"].get("name")
                for c in cols
                if c.get("properties", {}).get("unique") and c.get("properties", {}).get("name")
            ]
            if names:
                result.append({"name": index.get("properties", {}).get("name"), "columns": names})
        result.sort(key=lambda i: str(i["name"] or ""))
        return result

    def get_foreign_keys(self, catalog_item_urn: str) -> list[dict]:
        """Foreign keys for a catalog item, with the referenced table and columns."""
        result: list[dict] = []
        for fk in self._entities_by_parent("foreignKey", catalog_item_urn, "name"):
            cols = self._entities_by_parent(
                "foreignKeyColumn", fk["urn"], "name,referencedTableName,referencedColumnName,order"
            )
            cols.sort(key=lambda c: c.get("properties", {}).get("order") or 0)
            local = [c["properties"].get("name") for c in cols]
            ref_cols = [c["properties"].get("referencedColumnName") for c in cols]
            ref_tables = {
                c["properties"].get("referencedTableName") for c in cols if c["properties"].get("referencedTableName")
            }
            result.append(
                {
                    "name": fk.get("properties", {}).get("name"),
                    "columns": [c for c in local if c],
                    # a foreign key targets a single table; None if inconsistent/missing
                    "referenced_table": next(iter(ref_tables)) if len(ref_tables) == 1 else None,
                    "referenced_columns": [c for c in ref_cols if c],
                }
            )
        return result

    # --- composite ---

    def fetch_bundle(
        self, catalog_item_urn: str, *, with_dq: bool = True, with_relationships: bool = True
    ) -> CatalogItemBundle:
        """Fetch a catalog item, its attributes, referenced terms, and (by default) DQ results."""
        return self.fetch_bundle_for(
            self.get_catalog_item(catalog_item_urn),
            with_dq=with_dq,
            with_relationships=with_relationships,
        )

    def fetch_bundle_for(
        self, item: CatalogItem, *, with_dq: bool = True, with_relationships: bool = True
    ) -> CatalogItemBundle:
        """Everything an already-fetched catalog item needs to become a dataset.

        ``find_catalog_items`` returns whole items, so enriching them this way skips the
        per-item catalog-item request entirely.
        """
        catalog_item_urn = item.urn
        attributes = self.get_attributes(catalog_item_urn)

        term_urns: set[str] = {ta.term_urn for ta in item.term_assignments}
        for attr in attributes:
            term_urns.update(ta.term_urn for ta in attr.term_assignments)

        terms: dict[str, Term] = {}
        for urn in sorted(term_urns):
            if not urn:
                continue
            try:
                terms[urn] = self.get_term(urn)
            except requests.HTTPError:
                # A referenced term may be inaccessible; skip it rather than fail the whole run.
                continue

        dq_results = None
        dq_threshold_pct = None
        if with_dq:
            dq_results = self.get_dq_results(catalog_item_urn)
            if dq_results is not None:
                dq_threshold_pct = self.get_dq_overall_threshold(catalog_item_urn)

        primary_key: dict | None = None
        unique_indexes: list[dict] = []
        foreign_keys: list[dict] = []
        if with_relationships:
            primary_key = self.get_primary_key(catalog_item_urn)
            unique_indexes = self.get_unique_indexes(catalog_item_urn)
            foreign_keys = self.get_foreign_keys(catalog_item_urn)

        return CatalogItemBundle(
            item=item,
            attributes=attributes,
            terms=terms,
            dq_results=dq_results,
            dq_threshold_pct=dq_threshold_pct,
            primary_key=primary_key,
            unique_indexes=unique_indexes,
            foreign_keys=foreign_keys,
        )
