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
import java.util.LinkedHashMap;
import java.util.Locale;
import java.util.Map;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;

/** Preserves the joined dataset grain for sums of declared integer columns. */
final class JoinedIntegerSum {
  private static final String ID = "[A-Za-z_][A-Za-z0-9_]*";
  private static final String COLUMN = ID + "(?:\\." + ID + ")+";
  private static final Pattern QUALIFIED_COLUMN = Pattern.compile(COLUMN);
  private static final Pattern SUM = Pattern.compile(
      "(?i)(?<![\\w.])SUM\\s*\\(\\s*(" + COLUMN + ")\\s*\\)");
  private static final Pattern AGGREGATE_MODIFIER = Pattern.compile(
      "(?is)\\s*(?:(?:/\\*.*?\\*/|--[^\\r\\n]*)\\s*)*(?:FILTER|OVER)\\b");
  private static final String PREFIX = "AGGREGATE(COLLECT_SET(STRUCT(";
  private static final String SEPARATOR = " AS ossie_key, ";
  private static final String FILTER = " AS ossie_value)) FILTER (WHERE ";
  private static final String NOT_NULL = " IS NOT NULL";
  private static final String SUFFIX = "), CAST(NULL AS BIGINT), "
      + "(ossie_acc, ossie_row) -> COALESCE(ossie_acc, 0L) + CAST(ossie_row.ossie_value AS BIGINT))";
  private static final Pattern ENCODED = Pattern.compile(
      "(?i)\\bAGGREGATE\\s*\\(\\s*COLLECT_SET\\s*\\(\\s*STRUCT\\s*\\(\\s*("
          + COLUMN + ")\\s+AS\\s+ossie_key\\s*,\\s*(" + COLUMN + ")"
          + "\\s+AS\\s+ossie_value\\s*\\)\\s*\\)\\s*FILTER\\s*\\(\\s*WHERE\\s+"
          + "\\1\\s+IS\\s+NOT\\s+NULL\\s+AND\\s+\\2\\s+IS\\s+NOT\\s+NULL\\s*\\)\\s*,\\s*"
          + "CAST\\s*\\(\\s*NULL\\s+AS\\s+BIGINT\\s*\\)\\s*,\\s*"
          + "\\(\\s*ossie_acc\\s*,\\s*ossie_row\\s*\\)\\s*->\\s*"
          + "COALESCE\\s*\\(\\s*ossie_acc\\s*,\\s*0L\\s*\\)\\s*\\+\\s*"
          + "CAST\\s*\\(\\s*ossie_row\\.ossie_value\\s+AS\\s+BIGINT\\s*\\)\\s*\\)");

  private JoinedIntegerSum() {}

  static String encode(String expression, Map<String, String> keys, Set<String> grainPaths) {
    if (grainPaths.isEmpty()) {
      return expression;
    }
    // A quoted identifier may name a join path that the unquoted matcher cannot prove.
    if (expression.indexOf('`') >= 0 || expression.indexOf('"') >= 0) {
      return null;
    }
    StringBuilder remaining = new StringBuilder(expression);
    Set<String> rewrittenPaths = new HashSet<>();
    String encoded = replaceOutsideLiterals(expression, SUM, match -> {
      String value = match.group(1);
      String key = keys.get(value);
      String path = qualifier(value);
      if (key == null || !QUALIFIED_COLUMN.matcher(key).matches() || !qualifier(key).equals(path)
          || !grainPaths.contains(path)
          || AGGREGATE_MODIFIER.matcher(expression).region(match.end(), expression.length())
              .lookingAt()) {
        return match.group();
      }
      // The struct deduplicates rows by key, including stores with identical values. Removing
      // null operands and starting with NULL preserve SUM on unmatched and all-null groups.
      remaining.replace(match.start(), match.end(), " ".repeat(match.end() - match.start()));
      rewrittenPaths.add(path);
      return PREFIX + key + SEPARATOR + value + FILTER + key + NOT_NULL
          + " AND " + value + NOT_NULL + SUFFIX;
    });
    // If any operand on a hinted path remains, rewriting only the supported sums
    // could combine different grains in one measure. Keep the entire measure as-is.
    String pathPattern = grainPaths.stream().map(path ->
        java.util.Arrays.stream(path.split("\\.")).map(Pattern::quote)
            .collect(Collectors.joining("\\s*\\.\\s*"))).collect(Collectors.joining("|"));
    Pattern reference = Pattern.compile("(?i)(?<![\\w.])(?:" + pathPattern + ")\\s*\\.");
    return rewrittenPaths.containsAll(grainPaths) && !findOutsideLiterals(remaining.toString(), reference)
        ? encoded : null;
  }

  /** Whether {@code expression} already holds a keyed sum outside literals and comments. */
  static boolean isEncoded(String expression) {
    return findOutsideLiterals(expression, ENCODED);
  }

  static String qualifier(String column) {
    return column.substring(0, column.lastIndexOf('.'));
  }

  record Decoded(String expression, Set<String> physicalColumns, Map<String, String> keys) {}

  static Decoded decode(String expression) {
    Set<String> columns = new HashSet<>();
    Map<String, String> keys = new LinkedHashMap<>();
    Map<String, String> pathKeys = new LinkedHashMap<>();
    Set<String> conflictingPaths = new HashSet<>();
    String decoded = replaceOutsideLiterals(expression, ENCODED, match -> {
      String value = match.group(2);
      String key = match.group(1);
      if (!qualifier(value).equalsIgnoreCase(qualifier(key))) {
        return match.group();
      }
      String path = qualifier(value);
      String priorKey = pathKeys.putIfAbsent(path, key);
      if (priorKey != null && !priorKey.equalsIgnoreCase(key)) {
        conflictingPaths.add(path);
      }
      keys.put(value, key);
      columns.add(value.substring(value.lastIndexOf('.') + 1).toLowerCase(Locale.ROOT));
      return "SUM(" + value + ")";
    });
    if (!conflictingPaths.isEmpty() || encode(decoded, keys, pathKeys.keySet()) == null) {
      return new Decoded(expression, Set.of(), Map.of());
    }
    return new Decoded(decoded, columns, keys);
  }
}
