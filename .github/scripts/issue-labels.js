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

// Names that mean a converter but don't contain its directory name.
const ALIASES = {
  'power bi': 'microsoft',
  powerbi: 'microsoft',
  metricflow: 'dbt',
};

// Converter names from the converter/* labels in .github/labeler.yml, parsed the
// same way auto-label-prs.yml reads its label list.
function converterNames(labelerText) {
  return labelerText.split('\n')
    .filter(line => /^\S.*:/.test(line) && !line.startsWith('#'))
    .map(line => line.split(':')[0].trim())
    .filter(label => label.startsWith('converter/'))
    .map(label => label.slice('converter/'.length));
}

function escapeRegExp(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

// Matches term as a whole word; '-' counts as part of a word so that "wisdom"
// does not match inside "wisdom-semantic-view".
function mentions(text, term) {
  return new RegExp(`(^|[^a-z0-9-])${escapeRegExp(term)}([^a-z0-9-]|$)`).test(text);
}

function labelsForIssue({ title, body }, names) {
  const known = new Set(names);
  const labels = new Set();
  body = body || '';

  // Issue forms render a dropdown as "### Converter" followed by the selected
  // options, comma-separated, or "_No response_". Values that aren't a
  // converter, such as the "N/A" option, add no label.
  const field = body.match(/^###[ \t]+Converter[ \t]*\r?\n(?:[ \t]*\r?\n)*([^\r\n]+)/m);
  if (field) {
    for (const raw of field[1].split(',')) {
      const value = raw.trim().toLowerCase();
      if (known.has(value)) {
        labels.add(`converter/${value}`);
      }
    }
  } else if (!/^###[ \t]+Converter[ \t]*$/m.test(body)) {
    // Not filed through a form: fall back to converter names in the title.
    const text = (title || '').toLowerCase();
    for (const name of names) {
      if (mentions(text, name)) {
        labels.add(`converter/${name}`);
      }
    }
    for (const [alias, name] of Object.entries(ALIASES)) {
      if (known.has(name) && mentions(text, alias)) {
        labels.add(`converter/${name}`);
      }
    }
  }

  if (/^###[ \t]+Platform[ \t]*$/m.test(body)) {
    labels.add('new-converter');
  }

  return [...labels];
}

module.exports = { converterNames, labelsForIssue };
