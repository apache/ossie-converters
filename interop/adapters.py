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

"""Small adapters run in each converter's environment, without changing its CLI."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import tempfile


def convert(name, direction, source, output):
    if name == "gooddata":
        import yaml
        from ossie_gooddata import gooddata_to_ossie, ossie_to_gooddata
        from ossie_gooddata.models import gd_model_from_dict, gd_model_to_dict

        if direction == "import":
            result = gooddata_to_ossie(
                gd_model_from_dict(json.loads(source.read_text()))
            )
            output.write_text(yaml.safe_dump(result, sort_keys=False))
        else:
            result = gd_model_to_dict(
                ossie_to_gooddata(yaml.safe_load(source.read_text()))
            )
            output.write_text(json.dumps(result, indent=2) + "\n")
    elif name == "thoughtspot":
        from ossie_thoughtspot.cli import main

        inputs = sorted(source.rglob("*.tml")) if source.is_dir() else [source]
        if not inputs:
            raise ValueError("no ThoughtSpot TML files found")
        raise SystemExit(main(["to-ossie", *map(str, inputs), "-o", str(output)]))
    elif name == "wisdom-semantic-view":
        from ossie_wisdom_semantic_view.model import dump_ossie, load_ossie
        from ossie_wisdom_semantic_view.semantic_view import (
            dump_semantic_view,
            load_semantic_view,
            ossie_to_semantic_view,
            semantic_view_to_ossie,
        )

        if direction == "import":
            result = dump_ossie(
                semantic_view_to_ossie(load_semantic_view(source.read_text()))
            )
        else:
            result = dump_semantic_view(
                ossie_to_semantic_view(load_ossie(source.read_text()))
            )
        output.write_text(result)
    elif name == "salesforce":
        # This CLI writes beside its input under a model-derived filename. Stage a
        # copy so fixtures and upstream evidence cannot be overwritten.
        jar = Path("target/ossie-salesforce-converter-0.1.0-SNAPSHOT.jar").resolve()
        with tempfile.TemporaryDirectory(
            prefix="salesforce-", dir=output.parent
        ) as work:
            work = Path(work)
            staged = work / ("input.json" if direction == "import" else "input.yaml")
            shutil.copyfile(source, staged)
            process = subprocess.run(
                [
                    "java",
                    "-jar",
                    str(jar),
                    "toOssie" if direction == "import" else "toSF",
                    str(staged),
                ],
                check=False,
            )
            if process.returncode:
                raise SystemExit(process.returncode)
            files = [
                path
                for path in work.iterdir()
                if path != staged
                and path.suffix == (".yaml" if direction == "import" else ".json")
            ]
            if len(files) != 1:
                raise ValueError(f"expected one Salesforce output, found {len(files)}")
            shutil.copyfile(files[0], output)
    else:
        raise ValueError(f"unknown adapter: {name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "converter",
        choices=["gooddata", "thoughtspot", "wisdom-semantic-view", "salesforce"],
    )
    parser.add_argument("direction", choices=["import", "export"])
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    convert(args.converter, args.direction, args.input, args.output)


if __name__ == "__main__":
    main()
