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

"""Minimal Tableau calculation tokenizer: string literals, comments and [bracketed] references ("]]" escapes "]")."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Tok:
    kind: str  # code | string | comment | ref
    start: int
    end: int
    value: str = ""  # ref name (unescaped) for kind == "ref"


def escape_ref(name: str) -> str:
    return name.replace("]", "]]")


def unescape_ref(name: str) -> str:
    return name.replace("]]", "]")


def tokenize(f: str) -> list[Tok]:
    out: list[Tok] = []
    i, n, code_start = 0, len(f), 0

    def flush(upto: int) -> None:
        if upto > code_start:
            out.append(Tok("code", code_start, upto))

    while i < n:
        c = f[i]
        j = None
        if c in "\"'":
            j = i + 1
            while j < n:
                if f[j] == "\\":
                    j += 2
                    continue
                if f[j] == c:
                    break
                j += 1
            j = min(j + 1, n)
            tok = Tok("string", i, j)
        elif f.startswith("//", i):
            j = f.find("\n", i)
            j = n if j < 0 else j
            tok = Tok("comment", i, j)
        elif f.startswith("/*", i):
            j = f.find("*/", i + 2)
            j = n if j < 0 else j + 2
            tok = Tok("comment", i, j)
        elif c == "[":
            j = i + 1
            while j < n:
                if f[j] == "]":
                    if j + 1 < n and f[j + 1] == "]":
                        j += 2
                        continue
                    break
                j += 1
            end = min(j + 1, n)
            tok = Tok("ref", i, end, unescape_ref(f[i + 1:j]))
            j = end
        else:
            i += 1
            continue
        flush(i)
        out.append(tok)
        i = j
        code_start = i
    flush(n)
    return out


def masked(f: str) -> str:
    """Formula with strings/comments blanked and reference names replaced by 'x', same length: safe for keyword regexes."""
    buf = list(f)
    for t in tokenize(f):
        if t.kind in ("string", "comment"):
            for k in range(t.start, t.end):
                if buf[k] != "\n":
                    buf[k] = " "
        elif t.kind == "ref":
            for k in range(t.start + 1, t.end - 1):
                buf[k] = "x"
    return "".join(buf)


def refs(f: str) -> list[str]:
    return [t.value for t in tokenize(f) if t.kind == "ref"]


def sub_refs(f: str, fn: Callable[[str], str | None]) -> str:
    """Replace each reference token with fn(name); None keeps the original text."""
    parts: list[str] = []
    for t in tokenize(f):
        text = f[t.start:t.end]
        if t.kind == "ref":
            r = fn(t.value)
            text = text if r is None else r
        parts.append(text)
    return "".join(parts)
