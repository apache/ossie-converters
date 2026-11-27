<!--
  Licensed to the Apache Software Foundation (ASF) under one
  or more contributor license agreements.  See the NOTICE file
  distributed with this work for additional information
  regarding copyright ownership.  The ASF licenses this file
  to you under the Apache License, Version 2.0 (the
  "License"); you may not use this file except in compliance
  with the License.  You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

  Unless required by applicable law or agreed to in writing,
  software distributed under the License is distributed on an
  "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
  KIND, either express or implied.  See the License for the
  specific language governing permissions and limitations
  under the License.
-->

# Apache Ossie ↔ SLayer

[SLayer](https://github.com/MotleyAI/slayer) (`motley-slayer` on PyPI) stores Ossie semantic-model documents
natively, so this registration has no converter code of its own. It pins `motley-slayer` and runs the command
SLayer ships:

```bash
uv sync
uv run slayer ossie-roundtrip input.ossie.yaml output.ossie.yaml
```

`slayer ossie-roundtrip` imports the document into a temporary SLayer store and exports it again; it never touches
a user's store. Import and export are both this command, because SLayer's native format is Ossie
(`native_format: ossie` in `interop/manifest.yaml`).

SLayer's own statement of what it supports is its
[Ossie compliance page](https://docs.motley.ai/slayer/interfaces/ossie-compliance/).

## Tests

```bash
uv sync
uv run pytest
```

The tests round-trip the core TPC-DS and Flights examples from the `ossie` submodule and check that every dataset,
field, metric and relationship comes back under the same name.
