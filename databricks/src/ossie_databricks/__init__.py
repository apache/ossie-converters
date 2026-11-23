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

"""Apache Ossie converter for Databricks Unity Catalog metric views."""

from ossie_databricks.convert import (
    convert_ossie_model_from_databricks,
    convert_ossie_model_to_databricks,
)
from ossie_databricks.deploy import deploy_ossie_model_to_databricks
from ossie_databricks.result import ConversionError, ConversionWarning, Result, WarningCode
from ossie_databricks.wrap import wrap_metric_view_in_ossie_model

__all__ = [
    "ConversionError",
    "ConversionWarning",
    "Result",
    "WarningCode",
    "convert_ossie_model_from_databricks",
    "convert_ossie_model_to_databricks",
    "deploy_ossie_model_to_databricks",
    "wrap_metric_view_in_ossie_model",
]
