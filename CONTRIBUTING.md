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

# Contributing

Each top-level directory is one converter with its own tests. The core spec,
examples, validation script, and shared Python package come from
[apache/ossie](https://github.com/apache/ossie), pinned as the `ossie` git
submodule, so clone with `--recurse-submodules` (or run
`git submodule update --init`).

## Filing issues

Use one of the issue forms and pick the converter the issue is about; the
issue is labeled `converter/<name>` automatically, so converter maintainers can
filter for it. Requests for a converter that doesn't exist yet go in the
**New converter** form and are labeled `new-converter`.

## Bumping the `ossie` submodule pin

The pin is the submodule gitlink at path `ossie` (not a version file); read it
with `git ls-tree HEAD ossie`. Dependabot proposes a weekly bump; bump manually
when you need a change sooner, such as a core-spec change a converter depends on
or a new dialect or vendor you are registering. To bump manually:

```bash
git checkout -b bump-ossie-<shortsha>
git submodule update --init ossie
git -C ossie fetch origin && git -C ossie checkout <new-sha>
git add ossie                       # stages the one-line gitlink change
git -C ossie log --oneline <old>..<new>   # review what you're adopting
```

The bump PR carries the one-line gitlink change plus any regenerated output and
converter changes the bump required.
