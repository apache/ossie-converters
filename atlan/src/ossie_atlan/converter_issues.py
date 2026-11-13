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

"""What a conversion could not carry across, as data rather than text on stderr.

Same shape as the dbt and Cube converters' `ConverterIssue`: a registry file of many metrics
converts metric by metric, and a metric Ossie cannot express is an issue on that metric, not a
refusal of the whole file.
"""

from dataclasses import dataclass, field
from enum import Enum


class IssueType(Enum):
    # The metric has no Ossie form: a CTE or window function, a row listing, a list-valued
    # aggregate, a join with no declared cardinality, a non-SQL dialect, an unbound `?`.
    # The reason names the exact shape. See README "What the converter refuses".
    METRIC_NOT_CONVERTIBLE = "METRIC_NOT_CONVERTIBLE"

    # Two records define the same column or metric name differently; the first one in the
    # file is kept and this one is left out, so the Ossie file never carries two meanings
    # for one name.
    MERGE_CONFLICT = "MERGE_CONFLICT"

    # Two records define the same field with the same expression but different prose
    # (description, dimension values); the first one's prose is kept. Nothing numeric differs.
    PROSE_DIFFERENCE_KEPT_FIRST = "PROSE_DIFFERENCE_KEPT_FIRST"


@dataclass(frozen=True)
class ConverterIssue:
    """One thing that did not make it across, addressed to a named element."""

    issue_type: IssueType
    element_name: str
    detail: str = ""

    def __str__(self):
        suffix = f": {self.detail}" if self.detail else ""
        return f"[{self.issue_type.value}] {self.element_name}{suffix}"


@dataclass
class IssueLog:
    issues: list = field(default_factory=list)

    def add(self, issue_type, element_name, detail=""):
        issue = ConverterIssue(issue_type, element_name, detail)
        self.issues.append(issue)
        return issue

    def of_type(self, issue_type):
        return [i for i in self.issues if i.issue_type is issue_type]

    def __len__(self):
        return len(self.issues)

    def __iter__(self):
        return iter(self.issues)
