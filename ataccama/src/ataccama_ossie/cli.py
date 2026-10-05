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

"""CLI: import selected Ataccama catalog items into an Ossie semantic model.

Scope is defined by explicit catalog-item URNs (``--urn`` repeated, or ``--urns-file``)
and/or by connection, source, and location URNs. The groups are unioned, and a location
covers everything in its subtree. It's recommended to not combine catalog items from
multiple sources to avoid name clashes.

Configuration is read from environment variables (optionally loaded from an env file
via ``--env-file``):

  ATACCAMA_BASE_URL       API root, e.g. https://<host>/api
  ATACCAMA_TOKEN_URL      OAuth2 token endpoint (Keycloak openid-connect/token)
  ATACCAMA_CLIENT_ID      service-account client id
  ATACCAMA_CLIENT_SECRET  service-account client secret

Example:
  ataccama-to-ossie --env-file .ataccama.env \\
      --urn urn:ata:tenant:catalog:catalog-item:... \\
      --model-name my_model --output my_model.ossie.yaml
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator
from itertools import islice
from pathlib import Path

from dotenv import load_dotenv

from ataccama_ossie.ataccama_to_ossie import ataccama_to_ossie
from ataccama_ossie.client import AtaccamaClient
from ataccama_ossie.models import CatalogItem

REQUIRED_VARS = ("ATACCAMA_BASE_URL", "ATACCAMA_TOKEN_URL", "ATACCAMA_CLIENT_ID", "ATACCAMA_CLIENT_SECRET")


def _tenant_from_urn(urn: str) -> str | None:
    # urn:ata:<tenant>:<domain>:<type>:<id>
    parts = urn.split(":")
    return parts[2] if len(parts) > 2 else None


def _collect_urns(args: argparse.Namespace) -> list[str]:
    urns: list[str] = list(args.urn or [])
    if args.urns_file:
        for raw in Path(args.urns_file).read_text().splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                urns.append(line)
    # de-duplicate, preserve order
    seen: set[str] = set()
    return [u for u in urns if not (u in seen or seen.add(u))]


def _any_scope_urn(args: argparse.Namespace, requested_urns: list[str]) -> str:
    """Any URN the run was scoped by; they all carry the tenant in the same position."""
    return [*requested_urns, *(args.connection_urn or []), *(args.source_urn or []), *(args.location_urn or [])][0]


def _reporting_progress(items: Iterator[CatalogItem], total: int, seen: list[str]) -> Iterator[CatalogItem]:
    """Announce each item before it is enriched, and record what the filter returned."""
    for position, item in enumerate(items, start=1):
        seen.append(item.urn)
        print(f"Fetching {position}/{total} {item.urn} ...", file=sys.stderr)
        yield item


def _positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or more, got {number}")
    return number


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import Ataccama ONE catalog items into an Ossie semantic model.")
    parser.add_argument("--urn", action="append", help="Catalog-item URN to convert (repeatable).")
    parser.add_argument("--urns-file", help="File with one catalog-item URN per line.")
    parser.add_argument(
        "--connection-urn",
        action="append",
        help="Convert every catalog item on this connection (repeatable).",
    )
    parser.add_argument(
        "--source-urn",
        action="append",
        help="Convert every catalog item in this source (repeatable).",
    )
    parser.add_argument(
        "--location-urn",
        action="append",
        help="Convert every catalog item under this location, anywhere in its subtree (repeatable).",
    )
    parser.add_argument(
        "--max-items",
        type=_positive_int,
        help="Convert at most this many catalog items, for sampling a large source. "
        "Extra matches are left out with a warning.",
    )
    parser.add_argument("--model-name", default="ataccama_model", help="Ossie semantic model name.")
    parser.add_argument("--model-description", default=None, help="Ossie semantic model description.")
    parser.add_argument("--output", "-o", default="-", help="Output YAML path ('-' for stdout).")
    parser.add_argument("--env-file", help="Optional KEY=VALUE file with Ataccama connection config.")
    parser.add_argument(
        "--no-dq",
        action="store_true",
        help="Skip fetching data-quality results (DQ enrichment is included by default).",
    )
    parser.add_argument(
        "--no-relationships",
        action="store_true",
        help="Skip fetching primary/foreign keys (relationships and primary_key/unique_keys are included by default).",
    )
    parser.add_argument(
        "--dq-ai-warnings",
        action="store_true",
        help="Append a data-quality warning to ai_context.instructions for datasets that "
        "Ataccama flags as below quality — below its configured threshold, or with active "
        "findings (opt-in; requires DQ, so not with --no-dq).",
    )
    parser.add_argument(
        "--flat-ai-context",
        action="store_true",
        help="Emit ai_context as plain strings instead of objects. Both forms are valid "
        "Ossie, but some consumers (e.g. Snowflake) only accept strings.",
    )
    parser.add_argument(
        "--ossie-version",
        default=None,
        help="Stamp a different Ossie spec version on the document, for consumers pinned "
        "to a released spec (e.g. Snowflake, which rejects this repo's in-development "
        "version). The schema pins the version, so the result will not validate against it.",
    )
    args = parser.parse_args(argv)

    if args.env_file:
        load_dotenv(args.env_file, override=True)

    requested_urns = _collect_urns(args)
    if not (requested_urns or args.connection_urn or args.source_urn or args.location_urn):
        parser.error("provide a scope: --urn/--urns-file, or --connection-urn/--source-urn/--location-urn")

    missing = [v for v in REQUIRED_VARS if not os.environ.get(v)]
    if missing:
        parser.error(f"missing required configuration: {', '.join(missing)}")

    client = AtaccamaClient(
        base_url=os.environ["ATACCAMA_BASE_URL"],
        token_url=os.environ["ATACCAMA_TOKEN_URL"],
        client_id=os.environ["ATACCAMA_CLIENT_ID"],
        client_secret=os.environ["ATACCAMA_CLIENT_SECRET"],
    )

    print("Finding catalog items ...", file=sys.stderr)
    # Every scope URN is a filter criterion, so one that matches nothing simply contributes
    # nothing -- as true of a catalog item as of a source. Only an empty result is an error.
    results = client.find_catalog_items(
        catalog_item_urns=requested_urns,
        connection_urns=args.connection_urn or [],
        source_urns=args.source_urn or [],
        location_urns=args.location_urn or [],
    )
    if not results.total:
        parser.error("no catalog items match the given scope")

    converting = results.total if args.max_items is None else min(results.total, args.max_items)
    if converting < results.total:
        print(
            f"[WARNING] {results.total} catalog items match; converting the first {converting}. "
            f"Relationships may be incomplete, as foreign keys pointing outside the converted "
            f"set are dropped.",
            file=sys.stderr,
        )

    returned_urns: list[str] = []
    bundles = (
        client.fetch_bundle_for(item, with_dq=not args.no_dq, with_relationships=not args.no_relationships)
        for item in islice(_reporting_progress(results.items, converting, returned_urns), converting)
    )

    document = ataccama_to_ossie(
        bundles,
        model_name=args.model_name,
        model_description=args.model_description,
        tenant=_tenant_from_urn(_any_scope_urn(args, requested_urns)),
        dq_ai_warnings=args.dq_ai_warnings,
        flat_ai_context=args.flat_ai_context,
        ossie_version=args.ossie_version,
    )

    absent = [urn for urn in requested_urns if urn not in set(returned_urns)]
    if absent and converting == results.total:
        print(f"[WARNING] No catalog item matched: {', '.join(absent)}", file=sys.stderr)

    text = document.to_ossie_yaml()
    if args.output == "-":
        sys.stdout.write(text)
    else:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
