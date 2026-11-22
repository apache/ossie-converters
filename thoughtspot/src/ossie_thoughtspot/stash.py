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

"""Vendor extension payload handling, for the THOUGHTSPOT entry.

The stash lives in the *Ossie* document, so it is written on the way in and read
on the way out. It follows that the stash can only carry what TML contains.

The field holding those entries goes by two names: `custom_extensions`, which is
what the spec says today, and `nonportable_extensions`, which apache/ossie#522
renames it to without accepting the old one. `_extensions_field` is the single
place that decides which name a given document is on.
"""
import json
from typing import Any

from .constants import (
    EXTENSIONS_FIELD,
    EXTENSIONS_FIELD_RENAMED,
    STASH_VERSION,
    VENDOR_KEY,
)
from .errors import ConversionError

#: Instance-local identity never travels in a portable document.
#:
#: `obj_id` is in this scan but is NOT wholly forbidden: the scan stops it
#: riding along unnoticed inside a block copied wholesale from source TML, while
#: `constants.STASH_OBJ_ID` preserves it DELIBERATELY, under its own payload key.
#: The three are not equivalent -- `guid` is a raw cluster UUID and `fqn` a
#: reference to one, but `obj_id` is the handle ThoughtSpot introduced so objects
#: can be referenced ACROSS environments, and unlike an `fqn` it survives import.
_FORBIDDEN_KEYS = frozenset({"guid", "obj_id", "fqn"})


def _object_label(obj: dict) -> str:
    return str(obj.get("name", "<unnamed>"))


def _extensions_field(obj: dict) -> str:
    """The name this object spells the vendor-extensions field with.

    Both names are read, which is what lets a document written under
    apache/ossie#522 be understood here before that change lands -- the same
    readers-first order this module already applies to the object form of
    `data`, and for the same reason: the rename does not accept the old name,
    so there is no version of the spec under which a reader may wait.

    Only one name at a time, though. A document carrying both is refused rather
    than resolved by precedence: the two lists are not alternative spellings of
    one fact once they both exist, and silently reading one while
    `write_stash` extends the other would drop whatever was in the loser. What
    counts as present is the key, not a truthy value under it: apache/ossie#522
    does not accept the old name even carrying nothing.

    Neither name present is the ordinary case -- an object with no extensions
    at all -- and answers with today's name, so `write_stash` writes what the
    spec says.
    """
    present = [name for name in (EXTENSIONS_FIELD, EXTENSIONS_FIELD_RENAMED) if name in obj]
    if len(present) > 1:
        raise ConversionError(
            f"{_object_label(obj)!r} carries both {EXTENSIONS_FIELD} and "
            f"{EXTENSIONS_FIELD_RENAMED}; apache/ossie#522 renames the field "
            f"rather than adding one, so a document uses one name or the other"
        )
    return present[0] if present else EXTENSIONS_FIELD


def find_forbidden_key(value: Any, forbidden: frozenset[str] | None = None) -> str | None:
    """The first key from `forbidden` found anywhere inside `value`, at any
    depth, or `None`.

    `forbidden` defaults to `_FORBIDDEN_KEYS` (`guid`/`obj_id`/`fqn`). A caller
    with a wider identity vocabulary to check for — this
    package's own `dataset_id`/`custom_file_guid` additions, documented
    identity-shaped keys the default set does not name — passes its own set rather
    than this module maintaining a second, wider copy of its own; the scan
    itself is shared either way, so the two vocabularies cannot drift apart
    the way two independently maintained scans could.

    `write_stash` is the single point every stashed payload passes through,
    so this is the one place the check needs to live for no caller — present
    or future — to bypass it by nesting identity content one level below a
    payload's own top-level keys instead of putting it there directly. A
    value copied wholesale from source data, rather than rebuilt field by
    field, is exactly how that happens in practice — the documented
    ThoughtSpot shape `geo_config.custom_file_guid` naming a custom map is
    one real example.
    """
    names = forbidden if forbidden is not None else _FORBIDDEN_KEYS
    if isinstance(value, dict):
        for key, v in value.items():
            if key in names:
                return key
            found = find_forbidden_key(v, names)
            if found is not None:
                return found
        return None
    if isinstance(value, list):
        for item in value:
            found = find_forbidden_key(item, names)
            if found is not None:
                return found
        return None
    return None


def read_stash(obj: dict) -> dict[str, Any]:
    """Return this object's parsed THOUGHTSPOT payload, or {} if it has none.

    Both the field name and the shape of `data` are accepted in two forms: the
    `custom_extensions` field and JSON-string `data` the spec defines today, and
    the `nonportable_extensions` field and object `data` apache/ossie#522
    renames and reshapes them to. The two axes are independent -- a document may
    use either name with either shape -- so they are read independently.

    The two `data` shapes are NOT equivalent, and saying so was wrong in an
    earlier revision of this docstring. A JSON string is parsed by a JSON parser, so every
    scalar in it is typed by what was written. An object has already been
    through a YAML loader, which types unquoted scalars by guessing: `2024` is
    an int, `12:30` is 750, `2024-01-01` is a date. This function normalises
    the object form to JSON types and refuses what JSON cannot hold, which
    closes the gap for value KINDS but not for a string that was written
    unquoted and came back a number. A reader that needs a string checks.
    """
    field = _extensions_field(obj)
    extensions = obj.get(field) or []
    if not isinstance(extensions, list):
        raise ConversionError(
            f"{field} must be a list, not {type(extensions).__name__}"
        )
    for entry in extensions:
        # A hand-authored document can put anything here. Without this the
        # `.get` below raised a bare AttributeError, which escapes the CLI's
        # (ConversionError, OSError, UnicodeDecodeError) handler and prints a
        # traceback -- breaking
        # this module's own never-a-bare-traceback contract.
        if not isinstance(entry, dict):
            raise ConversionError(
                f"each {field} entry must be a mapping, not "
                f"{type(entry).__name__}"
            )
        if entry.get("vendor_name") != VENDOR_KEY:
            continue
        raw = entry.get("data")
        if raw is None:
            return {}
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                # Name the object; never surface a bare json traceback.
                raise ConversionError(
                    f"malformed THOUGHTSPOT {field} payload on "
                    f"{_object_label(obj)!r}: {exc}"
                ) from exc
        elif isinstance(raw, dict):
            # The object form apache/ossie#522 proposes. The reader accepts it
            # before any writer emits it: a document written under the new shape
            # is then readable here whenever that lands, and whichever way the
            # vote goes, where the reverse order would leave every such document
            # unreadable until we shipped a release of our own. `write_stash`
            # deliberately still emits the string form, which is what the spec
            # says today -- a test pins that, and it is not an oversight.
            #
            # Round-tripped through JSON rather than taken as-is, for two
            # reasons that are easy to miss:
            #
            # 1. In the object form the payload has been through a YAML
            #    loader, so it carries YAML types. `_yaml.Yaml12Loader`
            #    narrows only the bool resolver; the rest is SafeLoader's
            #    YAML 1.1 set, under which an unquoted `2024-01-01` is a
            #    `datetime.date`, `12:30` is 750 and `0755` is 493. A date,
            #    set or binary value has no JSON form at all and would
            #    otherwise reach `write_stash`'s `json.dumps` and fail there
            #    with a bare TypeError, which `_write_stash_safely` does not
            #    catch -- a traceback in front of a user.
            # 2. Taking `raw` as-is returns a live reference INTO the caller's
            #    document, and what this returns is put straight into TML
            #    bodies by `_shared_body` and `build_model`. The round trip
            #    is also the copy.
            #
            # It does not turn an unquoted `2024` into `"2024"`; nothing here
            # can know which keys were meant as strings. The three readers
            # that require one check for themselves and say so.
            try:
                parsed = json.loads(json.dumps(raw))
            except (TypeError, ValueError) as exc:
                raise ConversionError(
                    f"THOUGHTSPOT {field} payload on "
                    f"{_object_label(obj)!r} is not JSON-representable: {exc}"
                ) from exc
        else:
            raise ConversionError(
                f"{field} data for {_object_label(obj)!r} is "
                f"{type(raw).__name__}, expected a JSON string or a mapping"
            )
        version = parsed.get("_v") if isinstance(parsed, dict) else None
        if isinstance(version, bool) or version != STASH_VERSION:
            # An unrecognised shape version is a hard failure, not a
            # partial read — a future payload shape this converter has never
            # seen would otherwise be silently misread as the current one.
            #
            # A bool is excluded explicitly because Python reads `True == 1`,
            # so `_v: true` would otherwise pass as version 1. That is not
            # theoretical under apache/ossie#522: with `data` an object the
            # payload is YAML, where a bare `true` is a boolean under both 1.1
            # and 1.2 rules. `_yaml.Yaml12Loader` already narrows the 1.1-only
            # spellings (`yes`, `on`) to strings, which is why those are
            # refused -- but narrowing the resolver moves which spelling
            # bites, it does not stop one biting, and this is the check whose
            # whole job is to refuse a shape we do not recognise.
            raise ConversionError(
                f"THOUGHTSPOT {field} payload on "
                f"{_object_label(obj)!r} has shape version {version!r}, "
                f"which this converter does not recognise (expected "
                f"{STASH_VERSION!r})"
            )
        return parsed
    return {}


def write_stash(obj: dict, payload: dict[str, Any]) -> dict:
    """Merge `payload` into this object's THOUGHTSPOT entry, returning a new dict.

    Foreign-vendor entries are preserved untouched. An empty resulting
    payload writes nothing at all.
    """
    forbidden_key = find_forbidden_key(payload)
    if forbidden_key is not None:
        # Checked at any depth — see find_forbidden_key.
        raise ConversionError(
            f"refusing to stash instance-local identity key {forbidden_key!r} "
            f"on {_object_label(obj)!r}"
        )

    merged = {**read_stash(obj), **payload}
    if not merged:
        return dict(obj)

    merged["_v"] = STASH_VERSION  # stamp the shape version so a future reader can recognise it
    # Whichever name this document already uses, NOT whichever the spec will
    # settle on. Emitting EXTENSIONS_FIELD unconditionally would hand a
    # #522-shaped document back under both names; emitting
    # EXTENSIONS_FIELD_RENAMED unconditionally would make every document we
    # write unreadable to the spec as it stands. Neither present answers with
    # today's name, so the writer has not moved -- a test pins that.
    field = _extensions_field(obj)
    others = [e for e in obj.get(field) or [] if e.get("vendor_name") != VENDOR_KEY]
    out = dict(obj)
    # Exactly one own entry, merged rather than appended.
    out[field] = [
        *others,
        {"vendor_name": VENDOR_KEY, "data": json.dumps(merged, sort_keys=True)},
    ]
    return out


def restore(
    payload: dict[str, Any],
    key: str,
    derived: Any,
    *,
    witness: Any = None,
    witness_key: str | None = None,
) -> Any:
    """Stash-if-present-and-still-current-else-derive.

    `witness` is the live Ossie value and `witness_key` names the copy recorded
    alongside the stashed value. When they disagree the Ossie document has been
    edited since the stash was written, so the stash is stale for this key and
    `derived` wins. Without a witness this degrades to stash-if-present, which is
    correct only for values nothing downstream can edit.
    """
    if key not in payload:
        return derived
    if witness_key is not None and payload.get(witness_key) != witness:
        return derived
    return payload[key]
