/*
 * Licensed to the Apache Software Foundation (ASF) under one or more
 * contributor license agreements.  See the NOTICE file distributed with
 * this work for additional information regarding copyright ownership.
 * The ASF licenses this file to You under the Apache License, Version 2.0
 * (the "License"); you may not use this file except in compliance with
 * the License.  You may obtain a copy of the License at
 *
 *    http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

package org.apache.ossie.converter.databricks;

import static org.apache.ossie.converter.databricks.OssieConverterCommon.findOutsideLiterals;
import static org.apache.ossie.converter.databricks.OssieConverterCommon.replaceOutsideLiterals;

import java.util.HashSet;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;

/** Preserves the joined dataset grain for sums of declared integer columns. */
final class JoinedIntegerSum {
  private static final String ID = "[A-Za-z_][A-Za-z0-9_]*";
  private static final String COLUMN = ID + "(?:\\." + ID + ")+";
  private static final Pattern SUM = Pattern.compile(
      "(?i)(?<![\\w.])SUM\\s*\\(\\s*(" + COLUMN + ")\\s*\\)");
  private static final Pattern AGGREGATE_MODIFIER = Pattern.compile("(?i)\\b(?:FILTER|OVER)\\s*\\(");
  private static final String PREFIX = "AGGREGATE(COLLECT_SET(STRUCT(";
  private static final String SEPARATOR = " AS ossie_key, ";
  private static final String FILTER = " AS ossie_value)) FILTER (WHERE ";
  private static final String NOT_NULL = " IS NOT NULL";
  private static final String SUFFIX = "), CAST(NULL AS BIGINT), "
      + "(ossie_acc, ossie_row) -> COALESCE(ossie_acc, 0L) + CAST(ossie_row.ossie_value AS BIGINT))";
  private static final Pattern ENCODED = Pattern.compile(
      Pattern.quote(PREFIX) + "(" + COLUMN + ")" + Pattern.quote(SEPARATOR)
          + "(" + COLUMN + ")" + Pattern.quote(FILTER) + "\\1"
          + Pattern.quote(NOT_NULL + " AND ") + "\\2" + Pattern.quote(NOT_NULL + SUFFIX));

  private JoinedIntegerSum() {}

  static String encode(String expression, Map<String, String> keys, Set<String> grainPaths) {
    if (grainPaths.isEmpty() || findOutsideLiterals(expression, AGGREGATE_MODIFIER)) {
      return expression;
    }
    return replaceOutsideLiterals(expression, SUM, match -> {
      String value = match.group(1);
      String key = keys.get(value);
      if (key == null || !grainPaths.contains(value.substring(0, value.lastIndexOf('.')))) {
        return match.group();
      }
      // The struct deduplicates rows by key, including stores with identical values. Removing
      // null operands and starting with NULL preserve SUM on unmatched and all-null groups.
      return PREFIX + key + SEPARATOR + value + FILTER + key + NOT_NULL
          + " AND " + value + NOT_NULL + SUFFIX;
    });
  }

  record Decoded(String expression, Set<String> physicalColumns) {}

  static Decoded decode(String expression) {
    Set<String> columns = new HashSet<>();
    String decoded = replaceOutsideLiterals(expression, ENCODED, match -> {
      String value = match.group(2);
      columns.add(value.substring(value.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT));
      return "SUM(" + value + ")";
    });
    return new Decoded(decoded, columns);
  }
}
