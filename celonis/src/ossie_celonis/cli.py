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

import argparse
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import quote

import yaml
from ossie import OssieDocument
from ossie._loading import UniqueKeyLoader

from .converter import celonis_to_ossie, ossie_to_celonis
from .ontology import celonis_to_ossie_bundle


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_document(path):
    text = path.read_text(encoding="utf-8")
    return (
        json.loads(text, object_pairs_hook=_unique_json_object)
        if path.suffix.lower() == ".json"
        else yaml.load(text, Loader=UniqueKeyLoader)
    )


def _serialize(document, path):
    # Validate JSON representability even when emitting YAML.
    json_text = (
        json.dumps(document, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    )
    return (
        yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
        if path.suffix.lower() in (".yaml", ".yml")
        else json_text
    )


def _companion_paths(core_path, args):
    suffix = (
        core_path.suffix
        if core_path.suffix.lower() in (".yaml", ".yml", ".json")
        else ".json"
    )
    return (
        args.ontology or core_path.with_name(core_path.stem + ".ontology" + suffix),
        args.mapping or core_path.with_name(core_path.stem + ".mapping" + suffix),
    )


def _write_outputs(outputs):
    # Stage every file before replacing any destination. Invalid input and
    # unwritable companion destinations leave existing output files intact.
    staged = []
    try:
        for path, content in outputs:
            if path.exists() and not path.is_file():
                raise ValueError(f"Output destination is not a file: {path}")
            with NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, delete=False
            ) as file:
                staged.append((Path(file.name), path))
                file.write(content)
        for temporary, destination in staged:
            temporary.replace(destination)
    finally:
        for temporary, _ in staged:
            temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert Celonis JSON and Apache Ossie core/ontology/mapping files"
    )
    parser.add_argument("direction", choices=["celonis-to-ossie", "ossie-to-celonis"])
    parser.add_argument("-i", "--input", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument(
        "--name", help="Ossie model name on import (defaults to the package key)"
    )
    parser.add_argument("--package-key", help="Authoring package key on export")
    parser.add_argument(
        "--namespace", default="ossie", help="Namespace for new entities on export"
    )
    parser.add_argument(
        "--ontology",
        type=Path,
        help="Ontology output on import, input on export (default: CORE.ontology.EXT)",
    )
    parser.add_argument(
        "--mapping",
        type=Path,
        help="Mapping output on import, input on export (default: CORE.mapping.EXT)",
    )
    parser.add_argument(
        "--core-only",
        action="store_true",
        help="Convert the core model without reading/writing companions",
    )
    args = parser.parse_args(argv)
    try:
        if args.core_only and (args.ontology or args.mapping):
            raise ValueError("--core-only cannot be combined with --ontology/--mapping")
        input_paths = [args.input]
        outputs = []
        if args.direction == "celonis-to-ossie":
            pig = json.loads(
                args.input.read_text(encoding="utf-8"),
                object_pairs_hook=_unique_json_object,
            )
            if args.core_only:
                result = celonis_to_ossie(pig, name=args.name)
            else:
                ontology_path, mapping_path = _companion_paths(args.output, args)
                relative_iri = lambda path: quote(
                    Path(os.path.relpath(path, mapping_path.parent)).as_posix(),
                    safe="/.",
                )
                bundle = celonis_to_ossie_bundle(
                    pig,
                    name=args.name,
                    semantic_model_iri=relative_iri(args.output),
                    ontology_iri=relative_iri(ontology_path),
                )
                result = bundle["semantic_model"]
                outputs.extend(
                    [
                        (ontology_path, _serialize(bundle["ontology"], ontology_path)),
                        (mapping_path, _serialize(bundle["mapping"], mapping_path)),
                    ]
                )
            output = _serialize(result, args.output)
        else:
            text = args.input.read_text(encoding="utf-8")
            document = (
                OssieDocument.from_ossie_json(text)
                if args.input.suffix.lower() == ".json"
                else OssieDocument.from_ossie_yaml(text)
            )
            model = json.loads(document.to_ossie_json())
            ontology, mapping = None, None
            if not args.core_only:
                ontology_path, mapping_path = _companion_paths(args.input, args)
                if (
                    args.ontology
                    or args.mapping
                    or ontology_path.exists()
                    or mapping_path.exists()
                ):
                    if not ontology_path.is_file() or not mapping_path.is_file():
                        raise ValueError(
                            "Both ontology and mapping companion files are required; use --core-only to skip them"
                        )
                    ontology, mapping = _read_document(ontology_path), _read_document(
                        mapping_path
                    )
                    input_paths.extend([ontology_path, mapping_path])
            result = ossie_to_celonis(
                model,
                package_key=args.package_key,
                namespace=args.namespace,
                ontology=ontology,
                mapping=mapping,
            )
            output = (
                json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
            )
        outputs.insert(0, (args.output, output))
        destinations = [p.resolve() for p, _ in outputs]
        if len(set(destinations)) != len(destinations) or set(destinations) & {
            p.resolve() for p in input_paths
        }:
            raise ValueError(
                "Output paths must be distinct from each other and from input files"
            )
        _write_outputs(outputs)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        RecursionError,
        yaml.YAMLError,
    ) as exc:
        parser.exit(2, f"ossie-celonis: {exc}\n")


if __name__ == "__main__":
    main()
