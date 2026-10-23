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

"""Shared helpers for the Apache Ossie <-> Solid semantic model converters.

Both directions are pure offline YAML transforms. The cross-cutting concerns live
here: version constants, the `custom_extensions` stash protocol, YAML I/O, the
Solid asset-link markup, and small identifier helpers.
"""

import datetime
import json
import re
import warnings

import yaml

# Apache Ossie semantic model spec version this converter writes (see core-spec).
#
# NOTE: this spoke has no `apache-ossie` runtime dependency, so nothing updates the
# constant automatically -- it MUST be bumped in lockstep with the `version` in
# `core-spec/` whenever the spec version moves.
OSSIE_VERSION = "0.2.0.dev0"

# Spec versions this converter knows. A document declaring another is still read, as
# OSSIE_VERSION, with a warning (see convert_ossie_to_solid). Output is always written as
# OSSIE_VERSION.
#
# 0.1.1 is the only *released* spec version, so it is what models in the wild declare.
# It differs from 0.2.0.dev0 in one structural way and a few additive ones. The
# structural one is the document shape: 0.1.1 (and early 0.2.0.dev0 snapshots) wrap the
# model in a `semantic_model` list, where 0.2.0.dev0 puts one model's properties at the
# document root. convert_ossie_to_solid reads both shapes. The additive ones are
# `datatype` on Field and Metric, `BIGQUERY` and `OSSIE_SQL_2026` in the dialect enum,
# and a free-form `vendor_name`; of those only the absence of `datatype` is visible to
# this converter, and it is handled the same way a 0.2.0.dev0 model that simply omits
# it is handled.
READABLE_OSSIE_VERSIONS = ("0.2.0.dev0", "0.1.1")

# Read-only spec versions whose omissions are worth naming when one is encountered.
OSSIE_VERSION_NOTES = {
    "0.1.1": "it predates the `datatype` field, so a column with no stashed raw type "
             "gets an empty Solid 'type'",
}

# Vendor id used for the `custom_extensions` stash.
VENDOR = "SOLID"

# Note import writes to a one-to-one relationship's `ai_context.instructions`, where
# Apache Ossie's directional from/to carries no meaning (see solid_to_ossie._orient).
# Export recognizes it as this converter's own marker rather than a user annotation, so
# it is dropped silently on the way back while real annotations are reported.
ONE_TO_ONE_NOTE = ("One-to-one relationship: both sides are unique on the join "
                   "columns, so the from/to direction is arbitrary.")

# Bump when the shape of a stashed `data` blob changes.
STASH_VERSION = 1

# Apache Ossie dialects this converter can resolve a Solid model to. Solid itself
# records the warehouse on the asset row, not in the exported YAML, so the dialect is
# either supplied explicitly or inferred from the column type vocabulary
# (see datatypes.infer_dialect).
DIALECT_ANSI = "ANSI_SQL"
DIALECT_SNOWFLAKE = "SNOWFLAKE"
DIALECT_DATABRICKS = "DATABRICKS"
DIALECT_BIGQUERY = "BIGQUERY"

SUPPORTED_DIALECTS = (
    DIALECT_ANSI,
    DIALECT_SNOWFLAKE,
    DIALECT_DATABRICKS,
    DIALECT_BIGQUERY,
)

# The spec's portable dialect, defined on ANSI SQL:2003 Core. It is read as an
# ANSI_SQL-equivalent fallback, never resolved to or written.
DIALECT_OSSIE_SQL = "OSSIE_SQL_2026"

# The dialect-neutral forms an expression may carry, in the order they are preferred
# when the requested dialect is absent.
PORTABLE_DIALECTS = (DIALECT_ANSI, DIALECT_OSSIE_SQL)

# Apache Ossie dialect -> sqlglot dialect name. The portable dialects map to sqlglot's
# default ("" / None) parser rather than a vendor grammar.
SQLGLOT_DIALECTS = {
    DIALECT_ANSI: None,
    DIALECT_OSSIE_SQL: None,
    DIALECT_SNOWFLAKE: "snowflake",
    DIALECT_DATABRICKS: "databricks",
    DIALECT_BIGQUERY: "bigquery",
}

# Solid renders a composite primary key as a single comma-joined scalar
# (`primary_key: 'ORDER_ID, LINE_NO'`); see semantic_layer_utils.py in solid-server.
# Apache Ossie models it as a column array, so the two are split/joined on this.
PK_SEPARATOR = ", "

# Solid embeds references to its own catalog objects inside `custom_instructions` as
# self-closing markup carrying an internal UUID:
#
#     @<assetlink id='00000000-0000-4000-8000-000000000000' type='column'
#                 name='orders.lead_source'>
#
# There is no closing tag and attribute order is not significant. Solid resolves these
# to their `name` before the text reaches an LLM; this converter does the same for the
# Apache Ossie `ai_context.instructions` a downstream tool will read, and keeps the raw
# tagged text in the stash so an export reproduces the original byte-for-byte.
_ASSET_LINK_RE = re.compile(r"@<assetlink\s+([^>]+)>", re.IGNORECASE)
_ASSET_LINK_ATTR_RE = re.compile(r"(\w+)\s*=\s*[\"']([^\"']*)[\"']")


class ConversionError(Exception):
    """Raised when an input cannot be converted."""


def warn(scope, message):
    """Emit a conversion warning. The CLI surfaces these on stderr."""
    warnings.warn(f"[{scope}] {message}", stacklevel=2)


def warn_unknown_keys(obj, known, scope, label, format_name):
    """Warn about keys of `obj` outside `known`, which the conversion does not read.

    A key a later version of either format adds would otherwise vanish without a word,
    as Solid's structured `custom_instructions` once did.
    """
    extra = [str(key) for key in obj if key not in known]
    if extra:
        names = ", ".join(f"'{key}'" for key in extra)
        verb = "is" if len(extra) == 1 else "are"
        warn(scope, f"{label}: {names} {verb} not part of {format_name} as this converter "
                    f"reads it, so {'it was' if len(extra) == 1 else 'they were'} dropped")


def require(obj, key, what):
    """Return `obj[key]`, or raise a clean ConversionError if it is missing/empty.

    Presence is tested by key rather than truthiness, so a legitimately falsy value such
    as `0` or `False` is returned; a missing key, a null, or an empty/whitespace-only
    string is rejected.
    """
    if not isinstance(obj, dict) or key not in obj or obj[key] is None:
        raise ConversionError(f"{what} is missing required '{key}'")
    value = obj[key]
    if isinstance(value, str) and not value.strip():
        raise ConversionError(f"{what} has an empty '{key}'")
    return value


def require_str(obj, key, what):
    """Like require(), but also enforce that the value is a string -- so a non-string
    scalar (e.g. a YAML number used as a name) raises a clean ConversionError instead of
    failing later inside a string operation."""
    value = require(obj, key, what)
    if not isinstance(value, str):
        raise ConversionError(
            f"{what}: '{key}' must be a string, got {type(value).__name__}")
    return value


# The readers below take the place of `value or []` and `isinstance(...) else {}`. A
# null still reads as empty -- Solid writes an empty `tables:` or `join_keys.left:` as
# null -- but a value of the wrong kind raises, where it used to be read as empty (and so
# dropped silently) or to fail later inside a loop with a Python error.


def as_list(value, what):
    """`value` as a list: [] when absent or null, else it must already be a list."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ConversionError(f"{what} must be a list, got {type(value).__name__}")
    return value


def as_mapping(value, what):
    """`value` as a dict: {} when absent or null, else it must already be a mapping."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConversionError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def as_str(value, what):
    """`value` when it is a string, None when absent or null; anything else raises."""
    if value is None or isinstance(value, str):
        return value
    raise ConversionError(f"{what} must be a string, got {type(value).__name__}")


def load_yaml(text):
    """Parse YAML, surfacing a syntax error as a ConversionError so callers (and the
    CLI) get a clean message rather than a raw traceback."""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConversionError(f"Invalid YAML: {e}") from e


def dump_yaml(obj):
    """Serialize to YAML, preserving key insertion order and allowing Unicode through
    unescaped (Solid descriptions routinely contain non-ASCII punctuation)."""
    return yaml.safe_dump(
        obj, sort_keys=False, default_flow_style=False, allow_unicode=True, width=100
    )


def custom_extensions(obj, what):
    """The `custom_extensions` entries on an Apache Ossie object, checked for shape.

    Each entry must be a mapping whose `vendor_name` and `data` are strings, as the spec
    requires; anything else raises rather than failing inside the stash reader.
    """
    entries = as_list((obj or {}).get("custom_extensions"), f"{what}: custom_extensions")
    for ext in entries:
        if not isinstance(ext, dict):
            raise ConversionError(
                f"{what}: each custom_extensions entry must be a mapping, got "
                f"{type(ext).__name__}")
        as_str(ext.get("vendor_name"), f"{what}: custom_extensions vendor_name")
        as_str(ext.get("data"), f"{what}: custom_extensions data")
    return entries


# YAML reads an unquoted `2024-01-01` as a date, which JSON cannot hold. The stash tags
# dates and datetimes so they come back as the same type and re-dump unquoted, keeping
# the round trip exact. (Solid's own export quotes every value, so these only arrive in
# hand-edited YAML.)
_JSON_TAG = "__ossie_solid__"
_JSON_TEMPORAL = {"date": datetime.date, "datetime": datetime.datetime}


def _json_default(value):
    for tag, cls in _JSON_TEMPORAL.items():
        if type(value) is cls:
            return {_JSON_TAG: tag, "v": value.isoformat()}
    raise ConversionError(
        f"a value of type {type(value).__name__} cannot be carried in "
        f"custom_extensions[SOLID]")


def _json_object_hook(data):
    tag = data.get(_JSON_TAG)
    if not isinstance(tag, str) or tag not in _JSON_TEMPORAL or set(data) != {
            _JSON_TAG, "v"}:
        return data
    try:
        return _JSON_TEMPORAL[tag].fromisoformat(data["v"])
    except (TypeError, ValueError) as e:
        raise ConversionError(
            f"custom_extensions[SOLID] holds a malformed {tag}: {data['v']!r}") from e


def read_stash(obj, what="object"):
    """Return the SOLID stash dict on an Apache Ossie object, or {} if absent.

    The `_v` version marker is stripped from the returned dict.
    """
    for ext in custom_extensions(obj, what):
        if ext.get("vendor_name") == VENDOR:
            try:
                data = json.loads(ext.get("data") or "{}",
                                  object_hook=_json_object_hook)
            except ValueError as e:
                raise ConversionError(
                    f"{what}: SOLID custom_extensions data is not valid JSON: {e}") from e
            if not isinstance(data, dict):
                raise ConversionError(
                    f"{what}: SOLID custom_extensions data must be a JSON object")
            data.pop("_v", None)
            return data
    return {}


def write_stash(obj, data):
    """Attach a SOLID `custom_extensions` entry holding `data` (a dict).

    No-op when `data` is empty, so an object with nothing Solid-specific to carry stays
    clean. An existing SOLID entry is replaced, not merged into.
    """
    if not data:
        return
    payload = {"_v": STASH_VERSION}
    payload.update(data)
    try:
        blob = json.dumps(payload, ensure_ascii=False, default=_json_default)
    except (TypeError, ValueError) as e:
        # A mapping keyed by something JSON cannot key by, such as a YAML date.
        raise ConversionError(
            f"a value cannot be carried in custom_extensions[SOLID]: {e}") from e
    exts = obj.setdefault("custom_extensions", [])
    for ext in exts:
        if ext.get("vendor_name") == VENDOR:
            ext["data"] = blob
            return
    exts.append({"vendor_name": VENDOR, "data": blob})


def foreign_vendor_extensions(obj, what="object"):
    """Return the non-SOLID custom_extensions on an object.

    Solid's YAML has no slot for vendor metadata, so these are dropped on export; the
    caller warns rather than discarding them silently.
    """
    return [
        ext for ext in custom_extensions(obj, what) if ext.get("vendor_name") != VENDOR
    ]


def pick_expression(ossie_expression, dialect, what):
    """Choose the SQL string for an Apache Ossie expression: `dialect`, else ANSI_SQL,
    else OSSIE_SQL_2026.

    Returns (expression, fallback) where `fallback` is None when the requested dialect
    was found and otherwise names the portable dialect the value came from, so the
    caller can warn. Returns (None, None) when none of them is present.

    A dialect listed twice with different SQL raises, since which one is read would
    otherwise come down to list order. An exact repeat says nothing new and is read
    once. `what` names the field or metric for the error message.
    """
    dialects = {}
    expression = as_mapping(ossie_expression, f"{what}: expression")
    for entry in as_list(expression.get("dialects"), f"{what}: expression dialects"):
        if not isinstance(entry, dict):
            raise ConversionError(
                f"{what}: each expression dialect entry must be a mapping, got "
                f"{type(entry).__name__}")
        name = as_str(entry.get("dialect"), f"{what}: expression dialect")
        expr = entry.get("expression")
        if name in dialects and dialects[name] != expr:
            raise ConversionError(
                f"{what} lists the {name} dialect more than once with different "
                f"expressions; each dialect may appear once")
        dialects[name] = expr
    candidates = (dialect,) + tuple(d for d in PORTABLE_DIALECTS if d != dialect)
    for candidate in candidates:
        expr = dialects.get(candidate)
        if expr is None:
            continue
        if not isinstance(expr, str):
            raise ConversionError(
                f"{what}: expression must be a string, got {type(expr).__name__}")
        return expr, None if candidate == dialect else candidate
    return None, None


def readable_dialects(dialect):
    """Name the dialects `pick_expression` will accept, for an error message.

    Reads as "SNOWFLAKE, ANSI_SQL or OSSIE_SQL_2026", and as "ANSI_SQL or
    OSSIE_SQL_2026" when the selected dialect *is* ANSI_SQL.
    """
    names = [dialect] + [d for d in PORTABLE_DIALECTS if d != dialect]
    return f"{', '.join(names[:-1])} or {names[-1]}"


def resolve_asset_links(text):
    """Replace Solid `@<assetlink ...>` markup with the tag's `name` attribute.

    Mirrors solid-server's `replace_asset_links_with_display_names`, which is applied
    before instructions are handed to an LLM. A tag with no usable `name` is left
    verbatim rather than deleted, so no text is silently lost.
    """
    if not isinstance(text, str) or "@<assetlink" not in text.lower():
        return text

    def _replace(match):
        attrs = {
            k.lower(): v for k, v in _ASSET_LINK_ATTR_RE.findall(match.group(1))
        }
        return attrs.get("name") or match.group(0)

    return _ASSET_LINK_RE.sub(_replace, text)


def instruction_entries(value, what):
    """Solid's structured custom instructions, checked for shape: a list of mappings."""
    entries = as_list(value, what)
    for entry in entries:
        if not isinstance(entry, dict):
            raise ConversionError(
                f"{what}: each entry must be a mapping, got {type(entry).__name__}")
        as_list(entry.get("associated_assets"), f"{what}: associated_assets")
    return entries


def render_custom_instructions(text, entries):
    """Solid's custom instructions as the one text an Apache Ossie consumer reads.

    Solid holds free text in `business_context.custom_instructions` and, in its
    structured form, a root-level list of titled entries, each enabled or not and
    optionally scoped by a condition and to catalog assets. The free text comes first,
    then each enabled entry; a disabled one is not in force, so it is not rendered.
    Asset-link markup is resolved to display names, as Solid does before the text reaches
    an LLM. Returns None when nothing is in force.
    """
    parts = [resolve_asset_links(text)] if text else []
    for entry in entries:
        content = clean_text(entry.get("content"))
        if entry.get("is_enabled") is False or not content:
            continue
        lines = []
        title = clean_text(entry.get("title"))
        if title:
            lines.append(f"{title}:")
        condition = clean_text(entry.get("condition"))
        if condition:
            lines.append(f"Applies when: {resolve_asset_links(condition)}")
        assets = [
            clean_text(asset.get("name"))
            for asset in entry.get("associated_assets") or []
            if isinstance(asset, dict) and clean_text(asset.get("name"))
        ]
        if assets:
            lines.append(f"Applies to: {', '.join(assets)}")
        lines.append(resolve_asset_links(content))
        parts.append("\n".join(lines))
    return "\n\n".join(parts) or None


def has_asset_links(text):
    """True if `text` carries Solid asset-link markup."""
    return isinstance(text, str) and bool(_ASSET_LINK_RE.search(text))


def split_source(source):
    """Split a `catalog.schema.table` source into its parts, honouring backtick quoting.

    Returns the list of parts with any surrounding backticks or double quotes removed.
    BigQuery project ids contain hyphens (`my-project.web.sessions`), which is why
    parts are not validated as bare SQL identifiers here.
    """
    parts, current, quote = [], [], None
    for ch in str(source or ""):
        if quote:
            if ch == quote:
                quote = None
            else:
                current.append(ch)
        elif ch in ('"', "`"):
            quote = ch
        elif ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts]


def dataset_name_for(source, taken):
    """Derive a unique Apache Ossie dataset name from a Solid table FQN.

    Solid identifies a table by its fully-qualified `catalog.schema.table` name, which
    doubles as the Apache Ossie `source`. Apache Ossie dataset names are also expression
    qualifiers (`store_sales.ss_quantity`), so a dotted name is not usable -- the last
    identifier is preferred, widening to `schema_table` and then to the whole FQN with
    dots replaced only when a shorter form is already taken.

    Names are kept unique ignoring case. Unquoted SQL identifiers are case-insensitive,
    so `ORDERS.amount` and `orders.amount` are the same qualifier to most consumers, and
    to this converter's own expression matching.

    `taken` is a set of the (lower-cased) names already assigned; the chosen name is
    added to it.
    """
    parts = [p for p in split_source(source) if p]
    if not parts:
        raise ConversionError(f"Table source '{source}' has no usable name")
    candidates = []
    for depth in range(1, len(parts) + 1):
        candidates.append("_".join(parts[-depth:]).replace("-", "_"))
    for candidate in candidates:
        if candidate and candidate.lower() not in taken:
            taken.add(candidate.lower())
            return candidate
    # Every qualified form collided. The caller rejects a repeated FQN (see
    # solid_to_ossie._convert_model), so a suffixed name only survives for two distinct
    # FQNs whose full forms normalize alike (`my-db.s.t` and `my_db.s.t`, once `t` and
    # `s_t` are taken). Suffix rather than raise: both tables are real, and relationships
    # and metrics find them by FQN, not by this name.
    base = candidates[-1]
    n = 2
    while f"{base}_{n}".lower() in taken:
        n += 1
    taken.add(f"{base}_{n}".lower())
    return f"{base}_{n}"


def unique_name(base, taken):
    """Return `base`, or `base_2`/`base_3`/... if it is already in `taken`."""
    name = base
    n = 2
    while name in taken:
        name = f"{base}_{n}"
        n += 1
    taken.add(name)
    return name


def clean_text(value, what=None):
    """Normalize a Solid free-text field: strip trailing whitespace, drop empties.

    Solid emits block scalars for descriptions, which leaves a trailing newline, and
    writes `manual_description: ''` for a column whose human description was cleared.
    Both should become "absent" rather than an empty Apache Ossie field. A scalar YAML
    read as something other than a string (an unquoted `2024-01-01`, a number) is kept
    as its text rather than dropped; a list or mapping in a text slot raises, naming
    `what`, rather than vanishing.
    """
    if isinstance(value, (list, dict)):
        raise ConversionError(
            f"{what or 'a text field'} must be text, got a {type(value).__name__}")
    if isinstance(value, (bool, int, float, datetime.date)):
        value = str(value)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def string_list(value, what):
    """Coerce a list field to a list of non-empty strings, or [] if absent or null.

    Scalar items are read as text; a value that is not a list, or an item that is itself
    a list or mapping, raises rather than being dropped or stringified.
    """
    items = []
    for item in as_list(value, what):
        if isinstance(item, (list, dict)):
            raise ConversionError(
                f"{what} must hold plain values, got a {type(item).__name__}")
        if item is not None and str(item).strip():
            items.append(str(item).strip())
    return items


def build_ai_context(instructions=None, synonyms=None, examples=None):
    """Assemble an Apache Ossie `ai_context` object, or None when nothing is present.

    Key order is fixed (instructions, synonyms, examples) so output is deterministic.
    """
    ctx = {}
    if instructions:
        ctx["instructions"] = instructions
    if synonyms:
        ctx["synonyms"] = list(synonyms)
    if examples:
        ctx["examples"] = list(examples)
    return ctx or None


def ai_context_parts(ai_context, what):
    """Split an Apache Ossie `ai_context` into (instructions, synonyms, examples).

    The spec allows `ai_context` to be a bare string; that form carries instructions
    only. Anything other than a string or a mapping raises.
    """
    if ai_context is None:
        return None, [], []
    if isinstance(ai_context, str):
        return clean_text(ai_context), [], []
    context = as_mapping(ai_context, f"{what}: ai_context")
    # The spec lets an ai_context carry keys of a tool's own; Solid has no slot for them.
    scope, _, label = what.partition(" ")
    for key in context:
        if key not in ("instructions", "synonyms", "examples"):
            warn(scope, f"{label or 'the model'}: ai_context.{key} has no Solid "
                        f"equivalent and was dropped")
    return (
        clean_text(as_str(context.get("instructions"), f"{what}: ai_context.instructions")),
        string_list(context.get("synonyms"), f"{what}: ai_context.synonyms"),
        string_list(context.get("examples"), f"{what}: ai_context.examples"),
    )
