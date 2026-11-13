// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.

// Picks the labels for a newly opened issue. Used by
// .github/workflows/auto-label-issues.yml; kept free of GitHub API calls so it
// can be run locally with plain node.

// Converter names from the converter/* labels in .github/labeler.yml, parsed the
// same way auto-label-prs.yml reads its label list.
function converterNames(labelerText) {
  return labelerText.split('\n')
    .filter(line => /^\S.*:/.test(line) && !line.startsWith('#'))
    .map(line => line.split(':')[0].trim())
    .filter(label => label.startsWith('converter/'))
    .map(label => label.slice('converter/'.length));
}

// Returns the converter/<name> labels for an issue filed through a bug report or
// feature request form. Issue forms render a dropdown as "### Converter" followed
// by the selected options, comma-separated, or "_No response_". Values that aren't
// a converter, such as the "N/A" option, add no label. The new-converter label
// comes from the New converter form's own front matter.
function labelsForIssue({ body }, names) {
  const known = new Set(names);
  const labels = new Set();

  const field = (body || '').match(/^###[ \t]+Converter[ \t]*\r?\n(?:[ \t]*\r?\n)*([^\r\n]+)/m);
  if (field) {
    for (const raw of field[1].split(',')) {
      const value = raw.trim().toLowerCase();
      if (known.has(value)) {
        labels.add(`converter/${value}`);
      }
    }
  }

  return [...labels];
}

module.exports = { converterNames, labelsForIssue };
