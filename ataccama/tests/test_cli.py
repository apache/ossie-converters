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

"""Tests for how the CLI scopes a run (offline, with a stub client)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ataccama_ossie import cli as cli_module
from ataccama_ossie.client import CatalogItemResults
from ataccama_ossie.models import CatalogItem, CatalogItemBundle

ITEM_URN = "urn:ata:example-tenant:catalog:catalog-item:{}"
SOURCE_URN = "urn:ata:example-tenant:catalog:source:s1"
CONNECTION_URN = "urn:ata:example-tenant:processing:connection:c1"
LOCATION_URN = "urn:ata:example-tenant:catalog:location:l1"


class StubClient:
    """Records what the CLI asks for, and answers with the items it was primed with."""

    instance: StubClient | None = None

    def __init__(self, **_: Any) -> None:
        self.find_calls: list[dict[str, Any]] = []
        self.enriched: list[str] = []
        StubClient.instance = self

    def find_catalog_items(self, **kwargs: Any) -> CatalogItemResults:
        self.find_calls.append(kwargs)
        return CatalogItemResults(total=len(self.available), items=iter(self.available))

    def fetch_bundle_for(self, item: CatalogItem, **_: Any) -> CatalogItemBundle:
        self.enriched.append(item.urn)
        return CatalogItemBundle(item=item)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> type[StubClient]:
    for var in cli_module.REQUIRED_VARS:
        monkeypatch.setenv(var, "x")
    StubClient.available = []
    StubClient.instance = None
    monkeypatch.setattr(cli_module, "AtaccamaClient", StubClient)
    return StubClient


def _items(*names: str) -> list[CatalogItem]:
    return [CatalogItem(urn=ITEM_URN.format(n), name=n) for n in names]


def _run(tmp_path: Path, *argv: str) -> int:
    return cli_module.main([*argv, "-o", str(tmp_path / "out.yaml")])


def test_every_scope_flag_reaches_a_single_lookup(client, tmp_path: Path) -> None:
    client.available = _items("customers")

    _run(
        tmp_path,
        "--urn",
        ITEM_URN.format("customers"),
        "--connection-urn",
        CONNECTION_URN,
        "--source-urn",
        SOURCE_URN,
        "--location-urn",
        LOCATION_URN,
    )

    (call,) = client.instance.find_calls
    assert call == {
        "catalog_item_urns": [ITEM_URN.format("customers")],
        "connection_urns": [CONNECTION_URN],
        "source_urns": [SOURCE_URN],
        "location_urns": [LOCATION_URN],
    }


def test_max_items_enriches_only_up_to_the_cap(client, tmp_path: Path) -> None:
    client.available = _items("a", "b", "c")

    _run(tmp_path, "--source-urn", SOURCE_URN, "--max-items", "2")

    assert client.instance.enriched == [ITEM_URN.format("a"), ITEM_URN.format("b")]


def test_truncation_warns_before_any_enrichment(client, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # The server's total is known up front, so the warning does not wait for the run.
    client.available = _items("a", "b", "c")

    _run(tmp_path, "--source-urn", SOURCE_URN, "--max-items", "2")

    err = capsys.readouterr().err
    assert "3 catalog items match; converting the first 2" in err
    assert "Relationships may be incomplete" in err


def test_progress_reports_against_the_number_being_converted(
    client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client.available = _items("a", "b", "c")

    _run(tmp_path, "--source-urn", SOURCE_URN, "--max-items", "2")

    err = capsys.readouterr().err
    assert "Fetching 1/2" in err and "Fetching 2/2" in err


def test_a_cap_the_result_fits_within_neither_warns_nor_truncates(
    client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client.available = _items("a", "b")

    _run(tmp_path, "--source-urn", SOURCE_URN, "--max-items", "2")

    assert len(client.instance.enriched) == 2
    assert "More than" not in capsys.readouterr().err


def test_a_named_urn_that_matches_nothing_warns_rather_than_failing(
    client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A catalog-item URN is a filter criterion like any other, and a source URN that
    # matches nothing is not an error either -- but a silent typo would quietly shrink
    # the model, so it is reported.
    client.available = _items("customers")

    assert _run(tmp_path, "--urn", ITEM_URN.format("customers"), "--urn", ITEM_URN.format("ghost")) == 0

    err = capsys.readouterr().err
    assert "No catalog item matched" in err and ITEM_URN.format("ghost") in err
    assert client.instance.enriched == [ITEM_URN.format("customers")]


def test_a_scope_that_matches_nothing_at_all_is_an_error(
    client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    client.available = []

    with pytest.raises(SystemExit):
        _run(tmp_path, "--urn", ITEM_URN.format("ghost"), "--urn", ITEM_URN.format("spectre"))

    assert "no catalog items match" in capsys.readouterr().err


def test_truncation_does_not_report_named_urns_as_missing(
    client, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Cut off by the cap, not absent from the catalog -- the truncation warning says so.
    client.available = _items("a", "b")

    assert _run(tmp_path, "--urn", ITEM_URN.format("a"), "--urn", ITEM_URN.format("b"), "--max-items", "1") == 0
    assert "No catalog item matched" not in capsys.readouterr().err


def test_a_run_without_any_scope_is_refused(client, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path)

    assert "provide a scope" in capsys.readouterr().err
    assert client.instance is None  # refused before a client was even built


def test_nothing_matching_the_filter_is_an_error(client, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    client.available = []

    with pytest.raises(SystemExit):
        _run(tmp_path, "--source-urn", SOURCE_URN)

    assert "no catalog items match" in capsys.readouterr().err


def test_tenant_comes_from_the_scope_urn(client, tmp_path: Path) -> None:
    # Read off the arguments rather than an item, since the bundles are still unconsumed
    # when the converter is called.
    client.available = _items("customers")
    out = tmp_path / "out.yaml"

    cli_module.main(["--source-urn", SOURCE_URN, "-o", str(out)])

    assert "example-tenant" in out.read_text()


def test_max_items_below_one_is_rejected(client, tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _run(tmp_path, "--source-urn", SOURCE_URN, "--max-items", "0")
