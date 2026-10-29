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

"""Write an Apache Ossie model as the semantic model folder of a Power BI project.

The folder holds ``definition.pbism`` and the split TMDL ``definition/`` folder that
Power BI Desktop, Fabric Git integration and ``fabric-cicd`` read. TOM writes the TMDL,
so the layout is the one Power BI itself produces rather than a hand-written imitation.
"""

import json
import re
from pathlib import Path

from ._common import ConversionError
from .ossie_to_semantic_model import convert_ossie_to_semantic_model
from .tom import TomUnavailableError, serialize_tmdl_folder

# 4.0 is the first .pbism version whose model lives in definition/ (TMDL) rather than
# in model.bim (TMSL).
PBISM = {
    "$schema": "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/"
    "definitionProperties/1.0.0/schema.json",
    "version": "4.0",
    "settings": {},
}
# Characters and device names Windows rejects in a file name; the folder must be portable
# to every clone. A device name is reserved with any extension too, so `NUL.sales` is.
_INVALID_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAME_RE = re.compile(r"CON|PRN|AUX|NUL|(COM|LPT)[0-9¹²³]", re.IGNORECASE)


def export_powerbi_project(
    ossie_yaml_str, output_dir, *, source=None, force=False, assembly_dir=None
):
    """Write an Apache Ossie document as ``<output_dir>/<name>.SemanticModel/``.

    Args:
        ossie_yaml_str: The Apache Ossie document as YAML text or a parsed mapping.
        output_dir: The directory that receives the semantic model folder, usually the
            root of a Power BI project or of a Fabric Git workspace.
        source: Optional OneLake location for generated Direct Lake partitions, as for
            :func:`convert_ossie_to_semantic_model`.
        force: Replace ``definition.pbism`` and ``definition/`` in an existing, non-empty
            folder. Other files there, such as ``.pbi/`` or ``diagramLayout.json``, are
            kept.
        assembly_dir: The TOM assembly directory, as for :func:`validate_tmsl`.

    Returns:
        The path of the written ``.SemanticModel`` folder.

    Raises:
        ConversionError: if the model name cannot name a folder, the folder already
            holds a model and ``force`` is not set, or TOM rejects the model.
        TomUnavailableError: if the optional TOM setup is missing.
    """
    semantic_model = convert_ossie_to_semantic_model(ossie_yaml_str, source=source)
    folder = Path(output_dir) / f"{_folder_name(semantic_model['name'])}.SemanticModel"
    _check_target(folder, force)

    definition = folder / "definition"
    try:
        serialize_tmdl_folder(semantic_model, definition, assembly_dir=assembly_dir)
    except TomUnavailableError:
        raise
    except Exception as exc:  # noqa: BLE001 - pythonnet surfaces managed exceptions here
        message = getattr(exc, "Message", str(exc))
        raise ConversionError(f"TOM could not write {definition}: {message}") from exc

    folder.mkdir(parents=True, exist_ok=True)
    (folder / "definition.pbism").write_text(json.dumps(PBISM, indent=2) + "\n", encoding="utf-8")
    return folder


def _folder_name(name):
    name = str(name)
    if (
        not name.strip()
        or _INVALID_NAME_RE.search(name)
        or _RESERVED_NAME_RE.fullmatch(name.split(".")[0].rstrip())
        or name.endswith((".", " "))
    ):
        raise ConversionError(
            f"model name {name!r} cannot name a semantic model folder; rename the "
            "Apache Ossie model"
        )
    return name


def _check_target(folder, force):
    # A folder with both model.bim and definition/ is ambiguous to Power BI, so a TMSL
    # project is never silently switched to TMDL, not even with force.
    if (folder / "model.bim").exists():
        raise ConversionError(
            f"{folder} stores its model as TMSL (model.bim); remove it to write TMDL"
        )
    if not force and folder.is_dir() and any(folder.iterdir()):
        raise ConversionError(
            f"{folder} already exists and is not empty; use --force (force=True) to "
            "replace its definition"
        )
