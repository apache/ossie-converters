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

## Summary

<!-- Describe what this PR does and why. -->

## Related Issues

<!-- Link any related GitHub issues: Closes #123, Fixes #456 -->

## Checklist

### Converters
- [ ] Converter logic is updated to reflect spec or ontology changes in [apache/ossie-converters](https://github.com/apache/ossie-converters)
- [ ] New converters include tests under the converter's test directory
- [ ] If adding a new converter, `.github/labeler.yml`, `.github/dependabot.yml`, a `converter-<name>-ci.yml` workflow and the `Converter` dropdown in `.github/ISSUE_TEMPLATE/bug_report.yml` and `feature_request.yml` are updated for the new path

### Documentation
- [ ] The converter's `README.md` is updated to reflect any user-facing changes

### Tests
- [ ] All existing tests pass (`pytest` / `mvn verify` / CI green)
- [ ] New functionality is covered by tests

### Compliance
- [ ] ASF license headers are present on all new source files
- [ ] No third-party dependencies are added without PMC/IPMC approval
- [ ] If third-party source code is vendored/copied (not just declared as a dependency), `NOTICE` and/or `LICENSE` have been updated per [ASF policy](https://infra.apache.org/licensing-howto.html)
