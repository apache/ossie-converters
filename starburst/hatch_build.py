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

"""Bundle the pinned Ossie schema in built distributions."""

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        filename = "ossie-schema.json"
        source = root / "src" / "ossie_starburst" / filename
        if not source.is_file():
            source = root.parent / "ossie" / "core-spec" / filename
        if not source.is_file():
            raise RuntimeError("Initialize the ossie submodule before building")
        destination = Path("ossie_starburst") / filename
        if self.target_name == "sdist":
            destination = Path("src") / destination
        build_data["force_include"][str(source)] = str(destination)
