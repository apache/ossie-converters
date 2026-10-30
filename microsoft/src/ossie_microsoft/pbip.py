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

"""Resolve a Power BI project folder into TMDL documents.

The documents keep the PBIP folder layout. TOM resolves references between
documents before the existing TMSL-to-Ossie mapping runs. A project that stores its
model as TMSL (``model.bim``) is read with the existing BIM import instead.
"""

import json
import re
from pathlib import Path
from typing import NoReturn

from ._common import LOGGER, ConversionError, warn
from .semantic_model_to_ossie import convert_semantic_model_to_ossie
from .tom import TomUnavailableError, deserialize_tmdl_documents

_LOAD_FIRST = ("database.tmdl", "model.tmdl")
# The folders TOM's TMDL serializer writes below ``definition/``, one document per
# object. A ``.tmdl`` file anywhere else is not part of the model.
_TMDL_FOLDERS = frozenset({"tables", "roles", "cultures", "perspectives"})
# The first declaration of a folder document: ``table Orders``, ``table 'Order Lines'``.
_DECLARATION_RE = re.compile(r"^\s*\w+\s+('(?:[^']|'')*'|\S+)")


def resolve_definition_dir(path: Path) -> Path:
    """Return the ``definition`` directory that holds ``database.tmdl``."""
    path = path.expanduser().resolve()
    if not path.exists():
        raise ConversionError(f"{path} does not exist")
    if path.is_file() and path.suffix.lower() == ".pbip":
        return _definition_from_pbip(path)
    if path.is_dir() and (path / "database.tmdl").is_file():
        return path
    if path.is_dir() and (path / "definition" / "database.tmdl").is_file():
        return path / "definition"
    raise ConversionError(
        f"{path} is not a .pbip file or a semantic model folder "
        "(no definition/database.tmdl)"
    )


def tmsl_model(path: Path) -> Path | None:
    """The ``model.bim`` of a project that stores its model as TMSL, or None.

    For a ``.SemanticModel`` folder, or a ``.pbip`` whose ``byPath`` points at one,
    that has ``model.bim`` and no ``definition/``. A folder with both is ambiguous, as
    it is to Power BI, and is refused.
    """
    path = path.expanduser().resolve()
    if not path.exists():
        raise ConversionError(f"{path} does not exist")
    if path.is_file() and path.suffix.lower() == ".pbip":
        folder = _semantic_model_from_pbip(path)
    elif path.is_dir():
        folder = path
    else:
        return None
    model_bim = folder / "model.bim"
    if not model_bim.is_file():
        return None
    if (folder / "definition" / "database.tmdl").is_file():
        raise ConversionError(
            f"{folder} has both model.bim and definition/; remove one so the storage "
            "format is unambiguous"
        )
    return model_bim


def tmdl_documents(definition_dir: Path) -> list[tuple[str, str]]:
    """List ``*.tmdl`` files as ``(logical_path, text)`` for TOM.

    ``logical_path`` is relative to ``definition_dir`` and uses ``/``.
    ``database.tmdl`` and ``model.tmdl`` come first. Everything else is
    sorted by that path. Non-TMDL files are omitted.
    """
    if not (definition_dir / "database.tmdl").is_file():
        raise ConversionError(f"{definition_dir} has no database.tmdl")

    found: list[tuple[str, str]] = []
    declared: dict[tuple[str, str], str] = {}
    for file in definition_dir.rglob("*.tmdl"):
        relative = file.relative_to(definition_dir)
        # `.Hidden.tmdl` is a real TOM table file. Skip hidden directories
        # such as `.pbi`, not file names.
        if any(part.startswith(".") for part in relative.parts[:-1]):
            continue
        parts = relative.parts
        if len(parts) > 2 or (len(parts) == 2 and parts[0] not in _TMDL_FOLDERS):
            warn(f"file '{relative.as_posix()}'",
                 "not in the TMDL folder layout; not loaded")
            continue
        text = file.read_text(encoding="utf-8-sig")
        if len(parts) == 2:
            # A leftover copy (`tables/Orders.backup.tmdl`) declares an object a second
            # time; name both files rather than leave TOM to report a duplicate.
            name = _declared_name(text)
            if name is not None:
                key = (parts[0], name)
                if key in declared:
                    raise ConversionError(
                        f"{declared[key]} and {relative.as_posix()} both declare {name}; "
                        "remove the leftover copy"
                    )
                declared[key] = relative.as_posix()
        LOGGER.debug("loading TMDL document %s", relative.as_posix())
        found.append((relative.as_posix(), text))

    def sort_key(item: tuple[str, str]) -> tuple[int, str]:
        logical = item[0]
        if logical in _LOAD_FIRST:
            return (_LOAD_FIRST.index(logical), logical)
        return (len(_LOAD_FIRST), logical)

    return sorted(found, key=sort_key)


def import_powerbi_project(path: str | Path, *, translate_dax: bool = False) -> str:
    """Import a ``.pbip``, ``*.SemanticModel`` folder, or ``definition`` folder.

    Returns Apache Ossie YAML. Table references stay in the TMDL documents;
    TOM resolves them to TMSL before the existing Ossie mapping runs.
    ``translate_dax`` enables the same opt-in DAX-to-SQL translation as BIM import.
    """

    model_bim = tmsl_model(Path(path))
    if model_bim is not None:
        with model_bim.open(encoding="utf-8-sig") as fh:
            return convert_semantic_model_to_ossie(json.load(fh), translate_dax=translate_dax)
    documents = tmdl_documents(resolve_definition_dir(Path(path)))
    try:
        semantic_model = deserialize_tmdl_documents(documents)
    except Exception as exc:
        reraise_tom_load_failure(exc)
    return convert_semantic_model_to_ossie(semantic_model, translate_dax=translate_dax)


def reraise_tom_load_failure(exc: BaseException) -> NoReturn:
    """Raise a ConversionError for a failure while TOM loads TMDL.

    ``ConversionError`` passes through. ``TomUnavailableError`` keeps its
    message. Other exceptions use a .NET ``Message`` when one is present, so
    the CLI can report them without a traceback.
    """

    if isinstance(exc, ConversionError):
        raise exc
    if isinstance(exc, TomUnavailableError):
        raise ConversionError(str(exc)) from exc
    raise ConversionError(getattr(exc, "Message", str(exc))) from exc


def _declared_name(text: str) -> str | None:
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("///"):
            continue
        match = _DECLARATION_RE.match(line)
        return match.group(1) if match else None
    return None


def _definition_from_pbip(pbip: Path) -> Path:
    definition = _semantic_model_from_pbip(pbip) / "definition"
    if not (definition / "database.tmdl").is_file():
        raise ConversionError(
            f"{pbip} byPath does not contain definition/database.tmdl or model.bim")
    return definition


def _inside_project(pbip: Path, target: Path, what: str) -> Path:
    # An absolute path, or one with enough `..`, would point the importer at any folder;
    # the referenced artifacts belong next to the .pbip.
    root = pbip.parent.resolve()
    if not target.is_relative_to(root):
        raise ConversionError(f"{pbip} {what} points outside the project folder {root}")
    return target


def _semantic_model_from_pbip(pbip: Path) -> Path:
    try:
        payload = json.loads(pbip.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{pbip} is not a PBIP JSON file") from exc
    if not isinstance(payload, dict):
        raise ConversionError(f"{pbip} is not a PBIP JSON object")

    report_rel = _report_path(payload)
    if report_rel is None:
        raise ConversionError(f"{pbip} has no report artifact")

    report_dir = _inside_project(pbip, (pbip.parent / report_rel).resolve(), "report path")
    pbir_path = report_dir / "definition.pbir"
    if not pbir_path.is_file():
        raise ConversionError(f"{pbip} report has no definition.pbir")
    try:
        pbir = json.loads(pbir_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConversionError(f"{pbir_path} is not JSON") from exc
    if not isinstance(pbir, dict):
        raise ConversionError(f"{pbir_path} is not a report JSON object")

    dataset = pbir.get("datasetReference") or {}
    if not isinstance(dataset, dict):
        raise ConversionError(f"{pbir_path} has no valid datasetReference object")
    if "byConnection" in dataset and "byPath" not in dataset:
        raise ConversionError(
            f"{pbip} references a remote semantic model (byConnection); "
            "import needs a local SemanticModel folder"
        )
    by_path = dataset.get("byPath") or {}
    if not isinstance(by_path, dict) or not isinstance(by_path.get("path"), str):
        raise ConversionError(f"{pbip} has no datasetReference.byPath.path string")
    model_rel = by_path["path"]
    if not model_rel:
        raise ConversionError(f"{pbip} has no datasetReference.byPath")

    return _inside_project(pbip, (report_dir / model_rel).resolve(), "byPath")


def _report_path(payload: dict) -> str | None:
    artifacts = payload.get("artifacts") or []
    if not isinstance(artifacts, list):
        raise ConversionError("PBIP artifacts must be an array")
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        report = artifact.get("report")
        if isinstance(report, dict) and isinstance(report.get("path"), str) and report["path"]:
            return report["path"]
    return None
