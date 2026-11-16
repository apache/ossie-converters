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

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * Tests for the command-line wrapper: which command means which direction, which selector flag
 * each command accepts, and that stdout carries the same bytes as {@code -o}.
 */
public class OssieDatabricksConverterCliSuite {

  private static final String OSSIE_MODEL =
      "version: \"0.2.0.dev0\"\n"
      + "name: sales\n"
      + "datasets:\n"
      + "  - name: orders\n"
      + "    source: cat.sch.orders\n"
      + "    fields:\n"
      + "      - name: o_status\n"
      + "        expression:\n"
      + "          dialects: [{dialect: DATABRICKS, expression: o_orderstatus}]\n";

  private static final String METRIC_VIEW =
      "version: '1.1'\n"
      + "source: cat.sch.orders\n"
      + "dimensions:\n"
      + "- {name: o_status, expr: o_orderstatus}\n";

  @TempDir
  Path dir;

  private Path write(String name, String content) throws IOException {
    Path path = dir.resolve(name);
    Files.writeString(path, content, StandardCharsets.UTF_8);
    return path;
  }

  /** Runs the CLI core and returns whatever it wrote to stdout. */
  private static String stdout(String... args) {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    ByteArrayOutputStream err = new ByteArrayOutputStream();
    PrintStream outStream = new PrintStream(out, true, StandardCharsets.UTF_8);
    PrintStream errStream = new PrintStream(err, true, StandardCharsets.UTF_8);
    OssieDatabricksConverter.run(args, outStream, errStream);
    outStream.flush();
    errStream.flush();
    return out.toString(StandardCharsets.UTF_8);
  }

  private static OssieDatabricksConverter.ExitException exitFrom(String... args) {
    return assertThrows(OssieDatabricksConverter.ExitException.class, () -> stdout(args));
  }

  @Test
  public void exportConvertsAnOssieModelToAMetricView() throws IOException {
    // The directions are named from the Apache Ossie model's point of view, matching the library
    // Javadoc: `export` takes a model OUT to a Metric View.
    String out = stdout("export", write("model.yaml", OSSIE_MODEL).toString());
    assertTrue(out.contains("version: \"1.1\""), "expected a Metric View, got:\n" + out);
    assertTrue(out.contains("cat.sch.orders"), "expected the source, got:\n" + out);
  }

  @Test
  public void importConvertsAMetricViewToAnOssieModel() throws IOException {
    String out = stdout("import", write("view.yaml", METRIC_VIEW).toString());
    assertTrue(out.contains("datasets:"), "expected an Apache Ossie model, got:\n" + out);
    assertTrue(out.contains("0.2.0.dev0"), "expected the Apache Ossie version, got:\n" + out);
    assertFalse(out.contains("primary_key"), "the fact key is supplied by the caller");
  }

  @Test
  public void stdoutCarriesTheSameBytesAsTheOutputFile() throws IOException {
    Path model = write("model.yaml", OSSIE_MODEL);
    String piped = stdout("export", model.toString());
    Path file = dir.resolve("view.yaml");
    stdout("export", model.toString(), "-o", file.toString());
    // println used to add a second newline, so piping the CLI produced different bytes than -o.
    assertEquals(Files.readString(file, StandardCharsets.UTF_8), piped,
        "stdout and -o must agree byte-for-byte");
  }

  @Test
  public void exportRejectsTheImportSelectorFlag() throws IOException {
    // --name is the import's flag. Both flags used to share one field, so this was silently
    // accepted and used as the model name -- an export naming its own output model.
    OssieDatabricksConverter.ExitException e =
        exitFrom("export", write("model.yaml", OSSIE_MODEL).toString(), "--name", "zzz");
    assertEquals(2, e.code);
    assertTrue(e.getMessage().contains("'--name' is not valid for 'export'"),
        "expected the flag to be rejected, got: " + e.getMessage());
  }

  @Test
  public void importRejectsTheExportSelectorFlag() throws IOException {
    OssieDatabricksConverter.ExitException e =
        exitFrom("import", write("view.yaml", METRIC_VIEW).toString(), "--source", "zzz");
    assertEquals(2, e.code);
    assertTrue(e.getMessage().contains("'--source' is not valid for 'import'"),
        "expected the flag to be rejected, got: " + e.getMessage());
  }

  @Test
  public void importUsesNameForTheModelName() throws IOException {
    String out = stdout("import", write("view.yaml", METRIC_VIEW).toString(), "--name", "custom");
    assertTrue(out.contains("name: \"custom\""), "expected the named model, got:\n" + out);
  }

  @Test
  public void importUsesRepeatedPrimaryKeyFlagsOnTheFactSource() throws IOException {
    String joined = METRIC_VIEW
        + "joins:\n"
        + "- {name: customer, source: cat.sch.customer, "
        + "on: source.customer_id = customer.customer_id}\n";
    String out = stdout("import", write("view.yaml", joined).toString(),
        "--primary-key", " item_id ", "--name", "sales", "--primary-key", "ticket_number");
    Map<String, Object> model = OssieConverterCommon.asMap(OssieConverter.parseYaml(out));
    List<Object> datasets = OssieConverterCommon.asList(model.get("datasets"));
    Map<String, Object> fact = datasets.stream().map(OssieConverterCommon::asMap)
        .filter(dataset -> "sales".equals(dataset.get("name"))).findFirst().orElseThrow();
    assertEquals("cat.sch.orders", fact.get("source"));
    assertEquals(List.of("item_id", "ticket_number"), fact.get("primary_key"));
    assertTrue(datasets.size() > 1);
    assertTrue(datasets.stream().map(OssieConverterCommon::asMap)
        .filter(dataset -> dataset != fact).noneMatch(dataset -> dataset.containsKey("primary_key")));
  }

  @Test
  public void exportRejectsPrimaryKeyFlag() throws IOException {
    OssieDatabricksConverter.ExitException error =
        exitFrom("export", write("model.yaml", OSSIE_MODEL).toString(), "--primary-key", "id");
    assertEquals(2, error.code);
    assertTrue(error.getMessage().contains("'--primary-key' is only valid for 'import'"));
  }

  @Test
  public void importRejectsMissingPrimaryKeyValue() throws IOException {
    OssieDatabricksConverter.ExitException error =
        exitFrom("import", write("view.yaml", METRIC_VIEW).toString(), "--primary-key");
    assertEquals(2, error.code);
    assertTrue(error.getMessage().contains("'--primary-key' requires a value"));
  }

  @Test
  public void importApiRejectsBlankAndDuplicatePrimaryKeyColumns() {
    for (List<String> invalid : List.of(List.of(" "), List.of("order_id", " ORDER_ID "))) {
      OssieConverter.ConversionException error = assertThrows(
          OssieConverter.ConversionException.class,
          () -> OssieConverter.convertMetricViewToOssie(METRIC_VIEW, null, invalid));
      assertEquals(OssieConverter.ConversionException.Kind.INVALID_INPUT, error.getKind());
    }
  }

  @Test
  public void helpPrintsUsage() {
    // Arguments used to be parsed before the command was looked at, so `--help` reported a
    // missing input file.
    String out = stdout("--help");
    assertTrue(out.contains("Usage:"), "expected usage, got:\n" + out);
    assertTrue(out.contains("export <model.yaml>"), "expected the export line, got:\n" + out);
  }

  @Test
  public void unknownCommandNamesItself() throws IOException {
    OssieDatabricksConverter.ExitException e =
        exitFrom("frobnicate", write("model.yaml", OSSIE_MODEL).toString());
    assertEquals(2, e.code);
    assertTrue(e.getMessage().contains("Unknown command 'frobnicate'"),
        "expected the command to be named, got: " + e.getMessage());
  }

  @Test
  public void missingInputFileIsReported() {
    OssieDatabricksConverter.ExitException e = exitFrom("export");
    assertEquals(2, e.code);
    assertTrue(e.getMessage().contains("Missing input file"),
        "expected a missing-input error, got: " + e.getMessage());
  }
}
