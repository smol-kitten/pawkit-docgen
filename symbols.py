#!/usr/bin/env python3
"""symbols — deterministic symbol & call index (no AI).

The layer above repomap.py: where repomap maps a repo's STRUCTURE, this maps its
SYMBOLS — every function/method/class, its signature (params + types), whether it
has a docblock, and a reverse CALL index (who calls it, with the call-site line so
you can see the actual arguments). Built with each language's own tooling, no deps:
  * PHP    → the native tokenizer (php_symbols.php) — accurate scope + docblocks
  * Python → stdlib `ast`
  * C#/TS/JS → light regex (best-effort signatures + doc presence)

Output: <repo>/.claude/symbols.json (in-repo, travels with the repo). The recall
hook reads it to ANSWER "where/what is foo()" inline — no re-grepping after a
compaction. `index` additionally upserts symbols into Qdrant for semantic lookup.

Usage:
  symbols.py gen   [--cwd P] [--scope S]        (re)build the index
  symbols.py find  <query> [--cwd P]            look up a symbol (exact/substring)
  symbols.py callers <name> [--cwd P]           who calls it (+ call-site lines)
  symbols.py lint  [--lang php] [--strict] [--cwd P]   docblock coverage report
  symbols.py dead  [--all] [--cwd P]            unused symbols (dead-code candidates)
  symbols.py deadparams [all] [--cwd P]         params declared but never used in body
  symbols.py index [--cwd P]                    upsert symbols into semantic memory
  symbols.py selftest                           regression-guard build_mermaid() (no repo needed)
"""
import os
import re
import sys
import json
import ast
import argparse
import subprocess

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from scope_util import current_scope, repo_root

MANIFEST = {
    "role": "cli",
    "provides": "deterministic symbol & call index (no AI)",
}


HERE = os.environ.get("PAWKIT_HOME", os.path.dirname(os.path.abspath(__file__)))
IGNORE_DIRS = ("vendor/", "node_modules/", ".venv/", "venv/", "dist/", "build/",
               "target/", ".next/", "__pycache__/", ".git/", "bower_components/",
               "third_party/")
# Archived FIRST-PARTY code (t-8002a9): kept in the index so `find` still locates it,
# but excluded from every quality signal (coverage %, lint, dead, deadparams) and
# sectioned separately in the generated wiki. IGNORE_DIRS is the wrong tool for this —
# removing archived paths entirely would make the archive unsearchable. Observed in
# SmashOrPass: legacy/ was 34/314 symbols and dragged coverage down with a WinForms
# app CI cannot even build; a finding nobody will action trains people to ignore the tool.
ARCHIVED_DEFAULT = ("legacy/", "archive/", "archived/", "deprecated/", "old/")


def archived_dirs(root):
    """Per-repo archived-path prefixes: .claude/tooling.json {"archived_dirs": [...]},
    else the defaults. Per-repo because 'legacy' means different things in different
    repos — a hardcoded tuple will be wrong somewhere."""
    try:
        cfg = json.load(open(os.path.join(root, ".claude", "tooling.json")))
        dirs = cfg.get("archived_dirs")
        if isinstance(dirs, list):
            return tuple(d if d.endswith("/") else d + "/" for d in dirs)
    except Exception:
        pass
    return ARCHIVED_DEFAULT


def _is_archived(f, dirs):
    return any(f.startswith(d) or ("/" + d) in f for d in dirs)


SRC_EXT = {".php", ".py", ".cs", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
           ".go", ".rb", ".rs", ".java", ".c", ".h", ".cpp", ".cc", ".hpp", ".kt", ".lua",
           ".vb"}
# names too generic / too short to give a useful reverse-call index
COMMON = {"__construct", "__destruct", "__toString", "__get", "__set", "__call",
          "__invoke", "main", "init", "setUp", "tearDown", "toString", "render"}
# tokens that look like calls but are language keywords (filtered anyway by the
# symbol set, but cheap to exclude up front)
KEYWORDS = {"if", "for", "while", "switch", "catch", "foreach", "function", "array",
            "list", "isset", "empty", "echo", "print", "return", "new", "match",
            "and", "or", "not", "in", "is", "elif", "else", "with", "assert"}
CALL_RE = re.compile(r"\b([A-Za-z_]\w{2,})\s*\(")


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def _tracked_sources(root):
    out = []
    for f in _git(root, "ls-files").splitlines():
        if not f or any(f.startswith(d) or ("/" + d) in f for d in IGNORE_DIRS):
            continue
        if os.path.splitext(f)[1].lower() in SRC_EXT:
            out.append(f)
    return out


def _first_line(doc):
    for ln in (doc or "").splitlines():
        ln = ln.strip().lstrip("#").strip()
        if ln and not ln.startswith("@"):
            return ln[:160]
    return ""


# ---- PHP (native tokenizer) -------------------------------------------------
def php_symbols(root, files):
    syms = []
    if not files or not _has_php():
        return syms
    for i in range(0, len(files), 150):          # chunk to stay under argv limits
        batch = [os.path.join(root, f) for f in files[i:i + 150]]
        try:
            r = subprocess.run(["php", os.path.join(HERE, "php_symbols.php"), *batch],
                               capture_output=True, text=True, timeout=120)
        except Exception:
            continue
        for line in r.stdout.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            d["file"] = os.path.relpath(d["file"], root)
            syms.append(d)
    return syms


def _has_php():
    try:
        return subprocess.run(["php", "-v"], capture_output=True, timeout=8).returncode == 0
    except Exception:
        return False


# ---- Python (stdlib ast) ----------------------------------------------------
def py_symbols(root, rel):
    syms = []
    try:
        src = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        tree = ast.parse(src)
    except Exception:
        return syms
    stack = []

    def sig(node):
        try:
            return "(" + ast.unparse(node.args) + ")"
        except Exception:
            return "(...)"

    def walk(node):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, ast.ClassDef):
                doc = ast.get_docstring(ch)
                syms.append({"file": rel, "line": ch.lineno, "kind": "class",
                             "class": stack[-1] if stack else "", "name": ch.name,
                             "signature": "", "doc": bool(doc),
                             "doc_summary": _first_line(doc), "lang": "Python"})
                stack.append(ch.name); walk(ch); stack.pop()
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                doc = ast.get_docstring(ch)
                syms.append({"file": rel, "line": ch.lineno,
                             "kind": "method" if stack else "function",
                             "class": stack[-1] if stack else "", "name": ch.name,
                             "signature": sig(ch), "doc": bool(doc),
                             "doc_summary": _first_line(doc),
                             "generated": "@generated" in (doc or ""), "lang": "Python"})
                walk(ch)
            else:
                walk(ch)
    walk(tree)
    return syms


# ---- C# / TS / JS — brace-scanning extractor (no parser available) ----------
# Tracks class scope via brace depth so members get their class (code-map/systems)
# and visibility (dead-code tiers). Best-effort but far better than flat regex.
RE_LANG = {".cs": "C#", ".ts": "TypeScript", ".tsx": "TypeScript", ".js": "JavaScript", ".jsx": "JavaScript"}
_CTRL = {"if", "for", "while", "switch", "catch", "foreach", "lock", "fixed", "using",
         "return", "await", "yield", "throw", "else", "do", "function", "get", "set",
         "constructor", "super", "import", "export", "typeof", "new", "in", "of", "as"}
# if a line STARTS with one of these, it's a statement/expression, not a declaration
# (fixes `return Ok(...)`, `await Foo(...)`, `var x = Bar()` misparsing as methods)
_LEAD_SKIP = {"return", "await", "yield", "throw", "else", "var", "new", "if", "for",
              "while", "switch", "using", "do", "lock", "=>", "=", "&&", "||", "."}
_CLASS_RE = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:export\s+|default\s+)*"
                       r"(?:(public|private|protected|internal)\s+)?"
                       r"(?:static\s+|abstract\s+|sealed\s+|partial\s+|final\s+)*"
                       r"(class|interface|struct|enum|record|trait)\s+(\w+)")
# C#: `[attr] public static Foo Bar(...) {` — type then name then params
_CS_METHOD = re.compile(r"^\s*(?:\[[^\]]*\]\s*)*(?:(public|private|protected|internal)\s+)?"
                        r"(?:static\s+|virtual\s+|override\s+|async\s+|sealed\s+|abstract\s+|"
                        r"extern\s+|new\s+|partial\s+|readonly\s+|unsafe\s+)*"
                        r"[\w<>\[\].,?]+\s+(\w+)\s*\(([^;]*?)\)\s*(?:where[^{};]*)?(\{|=>|$)")
# JS/TS class method: `public async foo(...) {` / `foo(...): T {`
_JS_METHOD = re.compile(r"^\s*(?:(public|private|protected)\s+)?(?:static\s+|async\s+|readonly\s+)*"
                        r"(\w+)\s*\(([^;]*?)\)\s*(?::\s*[\w<>\[\].,\s|&]+)?\s*\{")
# JS/TS arrow / function declarations (top-level or class field)
_JS_FUNC = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:(public|private|protected)\s+)?"
                      r"(?:static\s+)?(?:async\s+)?function\s+(\w+)\s*\(([^;]*?)\)")
_JS_ARROW = re.compile(r"^\s*(?:export\s+)?(?:(public|private|protected)\s+)?(?:static\s+)?"
                       r"(?:const|let|var)?\s*(\w+)\s*[:=]\s*(?:async\s+)?\(([^)]*)\)"
                       r"\s*(?::\s*[\w<>\[\].,\s|&]+)?=>")


def scan_symbols(root, rel, lang):
    """Line scanner with brace-tracked class scope for C#/TS/JS."""
    try:
        lines = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read().splitlines()
    except Exception:
        return []
    syms, stack, depth, pending = [], [], 0, None   # stack: (class_name, body_depth)
    default_vis = "private" if lang == "C#" else "public"
    is_cs = lang == "C#"
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        cls_now = stack[-1][0] if stack else ""
        prev = lines[i - 2].strip() if i >= 2 else ""
        doc = prev.startswith(("///", "*", "/**", "//", "*/"))

        mc = _CLASS_RE.match(s)
        if mc:
            syms.append({"file": rel, "line": i, "kind": "class", "class": cls_now,
                         "name": mc.group(3), "signature": "", "doc": doc, "doc_summary": "",
                         "generated": False, "vis": mc.group(1) or "", "lang": lang})
            pending = mc.group(3)        # pushed when its body brace opens (Allman-safe)
        elif s[:1] and (s[0].isalpha() or s[0] in "_[@#") and s.split(" ", 1)[0] not in _LEAD_SKIP:
            # declarations start with an identifier/modifier/attribute — skip lines that
            # begin with punctuation (`? Ok(...)`, `: Foo(...)`, `.bar()`) or a keyword.
            mm = (_CS_METHOD if is_cs else _JS_METHOD).match(s)
            mf = None if mm else (_JS_FUNC.match(s) or (None if is_cs else _JS_ARROW.match(s)))
            m = mm or mf
            if m and m.group(2) not in _CTRL and not m.group(2)[0].isdigit():
                vis = m.group(1) or (default_vis if cls_now else "")
                syms.append({"file": rel, "line": i,
                             "kind": "method" if cls_now else "function", "class": cls_now,
                             "name": m.group(2), "signature": "(" + (m.group(3) or "").strip() + ")",
                             "doc": doc, "doc_summary": "", "generated": False, "vis": vis,
                             "override": bool(re.search(r"\boverride\b", s)), "lang": lang})

        new_depth = depth + raw.count("{") - raw.count("}")
        if pending and new_depth > depth:    # the brace that opens the class body
            stack.append((pending, new_depth)); pending = None
        depth = new_depth
        while stack and depth < stack[-1][1]:
            stack.pop()
    return syms


# ---- VB.NET extractor (no tree-sitter grammar → dedicated End-tracked scan) ---
# VB has no braces: scope is delimited by `Class X … End Class` / `Module`, and
# members by `Sub`/`Function`/`Property`. A brace scanner (scan_symbols) can't
# model this, so VB gets its own line scanner. Deterministic, stdlib-only.
# leading modifiers/visibility in ANY order (VB allows `Partial Friend Class`,
# `Public Shared Function`, …). Captured as one run; visibility teased out after.
_VB_KW = (r'(?:Public|Private|Friend|Protected|MustInherit|NotInheritable|Partial|'
          r'Shared|Overloads|Overrides|Overridable|MustOverride|NotOverridable|Shadows|'
          r'Async|Iterator|Default|ReadOnly|WriteOnly)')
_VB_LEAD = r'((?:' + _VB_KW + r'\s+)*)'
_VB_TYPE = re.compile(r'^\s*' + _VB_LEAD +
                      r'(Class|Module|Structure|Interface|Enum)\s+([A-Za-z_]\w*)', re.I)
_VB_END = re.compile(r'^\s*End\s+(Class|Module|Structure|Interface|Enum)\b', re.I)
_VB_MEMBER = re.compile(r'^\s*' + _VB_LEAD +
                        r'(Sub|Function|Property)\s+([A-Za-z_]\w*)\s*(\([^)]*\))?', re.I)
_VB_VIS_RE = re.compile(r'\b(Public|Private|Friend|Protected\s+Friend|Protected)\b', re.I)


def scan_vb_symbols(root, rel):
    """VB.NET symbols via line scan — scope tracked by Class/Module … End (no braces).
    XML-doc comments (''') count as docs; *.Designer.vb is flagged generated."""
    try:
        with open(os.path.join(root, rel), encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    generated = rel.lower().endswith(".designer.vb")
    syms, stack = [], []                                # stack of enclosing type names
    for i, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith("'"):                  # blank or comment line
            continue
        prev = lines[i - 2].strip() if i >= 2 else ""
        doc = prev.startswith("'''")                    # only XML-doc comments count
        if _VB_END.match(s):
            if stack:
                stack.pop()
            continue
        mt = _VB_TYPE.match(s)
        if mt:
            vm = _VB_VIS_RE.search(mt.group(1) or "")
            syms.append({"file": rel, "line": i, "kind": "class", "class": stack[-1] if stack else "",
                         "name": mt.group(3), "signature": "", "doc": doc, "doc_summary": "",
                         "generated": generated, "vis": (vm.group(1).lower() if vm else ""),
                         "lang": "VB.NET"})
            stack.append(mt.group(3))
            continue
        mm = _VB_MEMBER.match(s)
        if mm:
            cls_now = stack[-1] if stack else ""
            vm = _VB_VIS_RE.search(mm.group(1) or "")
            vis = vm.group(1).lower().replace(" ", "") if vm else ("public" if cls_now else "")
            syms.append({"file": rel, "line": i,
                         "kind": "method" if cls_now else "function", "class": cls_now,
                         "name": mm.group(3), "signature": (mm.group(4) or "()"),
                         "doc": doc, "doc_summary": "", "generated": generated,
                         "vis": vis, "lang": "VB.NET"})
    return syms


# ---- tree-sitter extractor (primary for non-native langs; CST-accurate) ------
# ext -> (tree-sitter grammar, display label). PHP/Python keep their native
# extractors (docblocks/visibility); everything else prefers tree-sitter, falling
# back to the regex scan_symbols (script-only floor) if the grammar can't load.
TS_LANG = {
    ".cs": ("csharp", "C#"), ".ts": ("typescript", "TypeScript"), ".tsx": ("tsx", "TypeScript"),
    ".js": ("javascript", "JavaScript"), ".jsx": ("javascript", "JavaScript"),
    ".mjs": ("javascript", "JavaScript"), ".cjs": ("javascript", "JavaScript"),
    ".go": ("go", "Go"), ".rb": ("ruby", "Ruby"), ".rs": ("rust", "Rust"),
    ".java": ("java", "Java"), ".kt": ("kotlin", "Kotlin"),
    ".c": ("c", "C"), ".h": ("c", "C"), ".cpp": ("cpp", "C++"), ".cc": ("cpp", "C++"), ".hpp": ("cpp", "C++"),
    ".lua": ("lua", "Lua"),
}
_TS_CLASS = {"class_declaration", "interface_declaration", "struct_declaration", "enum_declaration",
             "record_declaration", "abstract_class_declaration", "struct_item", "enum_item",
             "trait_item", "class_specifier", "struct_specifier", "type_spec", "module",
             "class", "object_declaration"}
_TS_FUNC = {"method_declaration", "constructor_declaration", "local_function_statement",
            "function_declaration", "method_definition", "function_signature", "method_signature",
            "function_item", "function_definition", "method", "singleton_method", "function"}
_TS_CACHE = {}


def _ts_parser(grammar):
    if grammar not in _TS_CACHE:
        parser = None
        # Prefer the core Parser(Language) API: language_pack 1.11's get_parser()
        # returns a parser whose .parse() rejects bytes ("'bytes' object is not an
        # instance of 'str'"), which silently broke ALL tree-sitter languages
        # (Go/Rust/TS/…). Building Parser(get_language(...)) accepts bytes correctly.
        try:
            import tree_sitter as _ts
            from tree_sitter_language_pack import get_language
            parser = _ts.Parser(get_language(grammar))
            parser.parse(b"")  # smoke-test the bytes API before trusting it
        except Exception:
            try:
                from tree_sitter_language_pack import get_parser
                parser = get_parser(grammar)
            except Exception:
                parser = None
        _TS_CACHE[grammar] = parser
    return _TS_CACHE[grammar]


def _ts_name(node):
    n = node.child_by_field_name("name")
    if n is not None:
        return n.text.decode("utf-8", "replace")
    d = node.child_by_field_name("declarator")     # C/C++/Go: walk declarators
    seen = 0
    while d is not None and seen < 5:
        if d.type in ("identifier", "field_identifier", "type_identifier"):
            return d.text.decode("utf-8", "replace")
        nd = d.child_by_field_name("declarator")
        d = nd if nd is not None else (d.named_children[0] if d.named_children else None)
        seen += 1
    # Grammars with no `name` field (e.g. Kotlin): the name is an unnamed
    # identifier child. Prefer a plain identifier (method/func name) over a
    # type_identifier (class name, or an extension-function receiver type).
    kids = node.named_children
    for want in (("simple_identifier", "identifier"), ("type_identifier",)):
        for ch in kids:
            if ch.type in want:
                return ch.text.decode("utf-8", "replace")
    return ""


_PARAM_SKIP = {"self", "cls", "_", "this", "$this"}


def _ident_names_in(node):
    """Every identifier-token text under a node — the body's 'used names' set."""
    out, stack = set(), [node]
    while stack:
        x = stack.pop()
        if x.type in ("identifier", "field_identifier", "shorthand_property_identifier",
                      "property_identifier", "variable_name"):
            out.add(x.text.decode("utf-8", "replace").lstrip("$"))
        stack.extend(x.children)
    return out


def _declared_param_names(params):
    """Best-effort simple parameter names (skips destructuring / variadics / receivers)."""
    names = []
    for p in params.named_children:
        t = p.type
        if t in ("comment",):
            continue
        if t in ("list_splat_pattern", "dictionary_splat_pattern", "spread_element",
                 "variadic_parameter", "self_parameter", "self", "receiver"):
            continue
        # Parameter PROPERTIES (t-f56c92): `constructor(readonly status: number)` in TS,
        # promoted ctor properties in PHP, and anything else carrying an accessibility/
        # readonly modifier declares a real FIELD that other files read. It is never
        # referenced in the ctor body by design, so a body-scan calls it unused — and
        # acting on that deletes a live field. Skip them entirely.
        kid_types = {c.type for c in p.children}
        if ("accessibility_modifier" in kid_types or "readonly" in kid_types
                or t == "property_promotion_parameter"
                or re.match(r"^\s*(public|private|protected|internal|readonly)\b",
                            p.text.decode("utf-8", "replace"))):
            continue
        nm = p.child_by_field_name("name") or p.child_by_field_name("pattern")
        if nm is None:
            if t == "identifier":
                nm = p
            else:
                nm = next((c for c in p.children if c.type in ("identifier", "variable_name")), None)
        if nm is None or nm.type not in ("identifier", "variable_name"):
            continue  # destructuring pattern or unrecognised → not a simple name, skip
        txt = nm.text.decode("utf-8", "replace").lstrip("$")
        if txt and txt not in _PARAM_SKIP and txt.lstrip("_"):
            names.append(txt)
    return names


def _unused_params(node):
    """Params declared but never referenced in the body. [] when there's no body
    (abstract/interface) or the params can't be read simply — conservative by design."""
    body = node.child_by_field_name("body")
    params = node.child_by_field_name("parameters")
    if body is None or params is None:
        return []
    declared = _declared_param_names(params)
    if not declared:
        return []
    # Stub bodies that only `throw` (NotSupported/NotImplemented, or an interface/contract
    # placeholder) keep their params to satisfy the signature — not dead params.
    bt = body.text.decode("utf-8", "replace").strip()
    bt_inner = bt[1:-1].strip() if bt.startswith("{") and bt.endswith("}") else bt
    bt_inner = bt_inner.lstrip("=>").strip()          # expression-bodied `=> throw …`
    if bt_inner.startswith("throw ") and bt_inner.rstrip(";").count(";") == 0:
        return []
    used = _ident_names_in(body)
    return [d for d in declared if d.lstrip("$") not in used]


def ts_symbols(root, rel, grammar, label):
    """Extract symbols via tree-sitter CST. Returns list, or None to signal fallback."""
    parser = _ts_parser(grammar)
    if parser is None:
        return None
    try:
        src = open(os.path.join(root, rel), "rb").read()
        tree = parser.parse(src)
    except Exception:
        return None

    syms = []

    def vis_of(node, name):
        for ch in node.children:
            if ch.type in ("modifiers", "modifier", "accessibility_modifier", "visibility_modifier"):
                txt = ch.text.decode("utf-8", "replace")
                for v in ("public", "private", "protected", "internal"):
                    if v in txt:
                        return v
        if grammar == "go":                       # Go: exported = Capitalised
            return "public" if name[:1].isupper() else "private"
        if grammar == "rust":
            return "private"                      # no `pub` modifier matched above
        return ""

    def doc_of(node):
        p = node.prev_sibling
        if p is not None and "comment" in p.type:
            txt = p.text.decode("utf-8", "replace")
            first = next((ln.strip().lstrip("/*# ").strip()
                          for ln in txt.splitlines() if ln.strip().lstrip("/*# ").strip()
                          and not ln.strip().lstrip("/*# ").startswith("@")), "")
            return True, first[:160], "@generated" in txt
        return False, "", False

    def walk(node, cls):
        for ch in node.children:
            t = ch.type
            if t in _TS_CLASS:
                nm = _ts_name(ch)
                if nm:
                    has, summ, gen = doc_of(ch)
                    syms.append({"file": rel, "line": ch.start_point[0] + 1, "kind": "class",
                                 "class": cls, "name": nm, "signature": "", "doc": has,
                                 "doc_summary": summ, "generated": gen, "vis": vis_of(ch, nm),
                                 "lang": label})
                    walk(ch, nm)
                    continue
            if t in _TS_FUNC:
                nm = _ts_name(ch)
                if nm:
                    params = ch.child_by_field_name("parameters")
                    sig = params.text.decode("utf-8", "replace") if params is not None else "()"
                    has, summ, gen = doc_of(ch)
                    ov = any(c.type in ("modifiers", "modifier") and "override" in c.text.decode("utf-8", "replace")
                             for c in ch.children)
                    syms.append({"file": rel, "line": ch.start_point[0] + 1,
                                 "kind": "method" if cls else "function", "class": cls, "name": nm,
                                 "signature": re.sub(r"\s+", " ", sig)[:200], "doc": has,
                                 "doc_summary": summ, "generated": gen, "vis": vis_of(ch, nm),
                                 "override": ov,
                                 "unused_params": [] if ov else _unused_params(ch), "lang": label})
                    walk(ch, cls)
                    continue
            # JS/TS arrow/function assigned to a name: `const Foo = (..) => {}`
            if grammar in ("javascript", "typescript", "tsx") and t in (
                    "variable_declarator", "public_field_definition", "field_definition"):
                val = ch.child_by_field_name("value")
                if val is not None and val.type in ("arrow_function", "function", "function_expression"):
                    nm = _ts_name(ch)
                    if nm:
                        params = val.child_by_field_name("parameters")
                        sig = params.text.decode("utf-8", "replace") if params is not None else "()"
                        has, summ, gen = doc_of(ch)
                        syms.append({"file": rel, "line": ch.start_point[0] + 1,
                                     "kind": "method" if cls else "function", "class": cls, "name": nm,
                                     "signature": re.sub(r"\s+", " ", sig)[:200], "doc": has,
                                     "doc_summary": summ, "generated": gen, "vis": "",
                                     "unused_params": _unused_params(val), "lang": label})
                        walk(ch, cls)
                        continue
            # Lua named function assigned to a name: `M.foo = function(..)` or
            # `local x = function(..)`. Anonymous callbacks (hook.Add('e', function()..))
            # are function_call args, not assignments, so they're correctly skipped.
            if grammar == "lua" and t == "assignment_statement":
                # structure: variable_list ( var, .. ) '=' expression_list ( fn, .. )
                vlist = next((c for c in ch.children if c.type == "variable_list"), None)
                elist = next((c for c in ch.children if c.type == "expression_list"), None)
                fn = next((g for g in elist.children if g.type == "function_definition"),
                          None) if elist is not None else None
                v0 = next((g for g in vlist.children if g.type in
                           ("identifier", "dot_index_expression", "method_index_expression")),
                          None) if vlist is not None else None
                nm = v0.text.decode("utf-8", "replace") if v0 is not None else ""
                if fn is not None and nm:
                    params = fn.child_by_field_name("parameters")
                    sig = params.text.decode("utf-8", "replace") if params is not None else "()"
                    has, summ, gen = doc_of(ch)
                    syms.append({"file": rel, "line": ch.start_point[0] + 1,
                                 "kind": "method" if cls else "function", "class": cls, "name": nm,
                                 "signature": re.sub(r"\s+", " ", sig)[:200], "doc": has,
                                 "doc_summary": summ, "generated": gen, "vis": "",
                                 "unused_params": _unused_params(fn), "lang": label})
                    walk(ch, cls)
                    continue
            walk(ch, cls)

    walk(tree.root_node, "")
    return syms


# ---- reverse call index -----------------------------------------------------
def call_index(root, files, names):
    """One pass over sources: name( occurrences whose name is a known symbol."""
    nameset = {n for n in names if len(n) >= 3 and n not in COMMON}
    calls = {}
    for rel in files:
        try:
            lines = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read().splitlines()
        except Exception:
            continue
        for i, ln in enumerate(lines, 1):
            seen = set()
            for m in CALL_RE.finditer(ln):
                nm = m.group(1)
                if nm in nameset and nm not in seen:
                    seen.add(nm)
                    lst = calls.setdefault(nm, [])
                    if len(lst) < 12:
                        lst.append({"file": rel, "line": i, "text": ln.strip()[:160]})
    return calls


# ---- build ------------------------------------------------------------------
def _parse_symbols(root, files):
    """Parse a list of source files into symbol records (language-dispatched).
    Shared by the full build and the incremental path."""
    by_ext = {}
    for f in files:
        by_ext.setdefault(os.path.splitext(f)[1].lower(), []).append(f)
    syms = []
    syms += php_symbols(root, by_ext.get(".php", []))
    for f in by_ext.get(".py", []):
        syms += py_symbols(root, f)
    for f in by_ext.get(".vb", []):                       # VB.NET (no tree-sitter grammar)
        syms += scan_vb_symbols(root, f)
    ts_used = False
    for ext, (grammar, label) in TS_LANG.items():
        for f in by_ext.get(ext, []):
            r = ts_symbols(root, f, grammar, label)
            if r is None:                         # grammar unavailable → regex floor
                r = scan_symbols(root, f, label) if label in ("C#", "TypeScript", "JavaScript") else []
            else:
                ts_used = True
            syms += r
    build.ts_used = getattr(build, "ts_used", False) or ts_used
    return syms


def build(root):
    files = _tracked_sources(root)
    build.ts_used = False
    syms = _parse_symbols(root, files)
    # drop call sites of the definition lines themselves
    defset = {(s["file"], s["line"]) for s in syms}
    calls = call_index(root, files, [s["name"] for s in syms])
    for nm, lst in calls.items():
        calls[nm] = [c for c in lst if (c["file"], c["line"]) not in defset]
    refs = sorted(_referenced_names(root, [s["name"] for s in syms], files))
    return {"files": len(files), "symbols": syms, "calls": calls, "refs": refs}


# A bare-word identifier NOT immediately followed by `(` — a method-group / delegate
# reference rather than a direct call (`.Where(IsSaveRoot)`, `x += OnDone`, passing
# `DrawWindow` to `GUI.Window(id, DrawWindow)`). The definition site is excluded because
# there the name IS followed by `(` (its parameter list).
_MGROUP_RE = re.compile(r"\b([A-Za-z_]\w{2,})\b(?!\s*\()")


def _referenced_names(root, names, files=None):
    """Subset of `names` that appear as a method-group / delegate reference anywhere in
    the given sources (default: all tracked). Lets dead-code / unused-param checks spare
    symbols whose signature is contract-bound (LINQ predicates, event handlers, delegate
    callbacks) — the C# usage forms a call-only scan misses. Name-based + coarse, matching
    the call index; errs toward 'used' (fewer false positives, some false negatives)."""
    want = {n for n in names if len(n) >= 3 and n not in COMMON}
    if not want:
        return set()
    found = set()
    for rel in (files if files is not None else _tracked_sources(root)):
        try:
            txt = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        for m in _MGROUP_RE.finditer(txt):
            nm = m.group(1)
            if nm in want:
                found.add(nm)
                if len(found) == len(want):
                    return found
    return found


def build_incremental(root, prev, index_mtime):
    """Re-parse only files changed since the last index (mtime > index mtime) and
    merge with the retained prior results — much cheaper than a whole-repo rebuild
    on a big repo after a small edit. Returns (dict, n_changed) or (None, reason)
    when a full rebuild is cheaper/safer (no prior index, or too much changed).

    Correct for the common case (editing bodies / adding-removing symbols within a
    file). Cross-file edges that hinge on a symbol NAME newly added in a changed
    file but CALLED from an unchanged file are only refreshed on the next full gen;
    that's an accepted approximation for a freshness index (full `gen` still runs
    periodically via bg-refresh)."""
    prev_syms = prev.get("symbols") or []
    if not prev_syms:
        return None, "no prior symbols"
    files = _tracked_sources(root)
    fileset = set(files)

    def _mtime(rel):
        try:
            return os.path.getmtime(os.path.join(root, rel))
        except Exception:
            return 0

    changed = [f for f in files if _mtime(f) > index_mtime]
    prev_files = {s["file"] for s in prev_syms}
    deleted = prev_files - fileset
    # gate: if too much changed, a clean full build is simpler and not much dearer
    if len(changed) > max(20, int(0.4 * len(files))):
        return None, f"{len(changed)} files changed (too many)"
    if not changed and not deleted:
        return None, "nothing changed"

    changed_set = set(changed)
    stale = changed_set | deleted
    kept_syms = [s for s in prev_syms if s["file"] not in stale]
    new_syms = _parse_symbols(root, changed)
    syms = kept_syms + new_syms

    # calls: keep prior sites not located in a stale file, add re-scanned sites for
    # the changed files (against the FULL updated name set).
    names = [s["name"] for s in syms]
    defset = {(s["file"], s["line"]) for s in syms}
    fresh = call_index(root, changed, names)
    calls = {}
    for nm, lst in (prev.get("calls") or {}).items():
        kept = [c for c in lst if c.get("file") not in stale]
        if kept:
            calls[nm] = kept
    for nm, lst in fresh.items():
        merged = calls.get(nm, []) + [c for c in lst if (c["file"], c["line"]) not in defset]
        # keep the same per-name cap as call_index
        calls[nm] = merged[:12]
    # method-group refs: recompute over all files (one text scan; cheap vs re-parse)
    refs = sorted(_referenced_names(root, names, files))
    return {"files": len(files), "symbols": syms, "calls": calls, "refs": refs}, len(changed)


# --- write-target seam (autowiki rework, Phase 1) --------------------------------
# LOCAL regen writes the symbol index to an OUT-OF-REPO cache so a live session never
# dirties tracked <repo>/.claude/** — that churn was blocking git checkout/stash/rebase
# mid-session and riding uninvited into PRs (peer reports m-263/m-264). READS prefer the
# fresh cache, then fall back to the committed in-repo copy (fresh clone / never-yet
# regenerated). CI or a manual publish (--publish / SYMBOLS_PUBLISH=1) writes the
# committed in-repo copy that travels with the repo for discovery.
_IDX_CACHE = os.path.join(HERE, ".idx")
_PUBLISH = os.environ.get("SYMBOLS_PUBLISH") == "1"


def _repo_slug(root):
    return re.sub(r"[^A-Za-z0-9]+", "-", (root or "").strip("/")) or "root"


def _cache_dir(root):
    return os.path.join(_IDX_CACHE, _repo_slug(root))


def _committed_index_path(root):
    return os.path.join(root, ".claude", "symbols.json")


def _index_path(root, write=False):
    if _PUBLISH:
        return _committed_index_path(root)
    cache = os.path.join(_cache_dir(root), "symbols.json")
    if write:
        return cache
    return cache if os.path.exists(cache) else _committed_index_path(root)


# ---- shared dependency-edge computation (used by mermaid + systems) ---------
# matches test files across conventions: tests/ spec/ dirs, C#'s *.Tests/ projects,
# JS __tests__, *_test.* / *.test.* / *.spec.*, and CamelCase FooTests.cs / FooTest.cs
_TEST_RE = re.compile(r"(^|/)(tests?|spec)/|\.Tests?/|__tests__|_test\.|\.test\.|\.spec\."
                      r"|(^|/)test_|Tests?\.[a-z]+$")


def class_edges(idx, skip_tests=True):
    """Component dependency edges from the call index: edge A->B (weight = #calls)
    when code in component A calls a symbol DEFINED in component B. Component =
    class (or file basename). Only unambiguously-defined names count, so edges are
    accurate. Returns (edges Counter[(src,dst)->w], file_node map)."""
    from collections import Counter, defaultdict
    syms, calls = idx["symbols"], idx["calls"]
    defof = defaultdict(set)
    file_classes = defaultdict(Counter)
    for s in syms:
        defof[s["name"]].add(s["class"] or os.path.basename(s["file"]))
        if s["class"]:
            file_classes[s["file"]][s["class"]] += 1
    file_node = {}
    for s in syms:
        f = s["file"]
        file_node[f] = (file_classes[f].most_common(1)[0][0] if file_classes[f]
                        else os.path.basename(f))
    edges = Counter()
    for name, sites in calls.items():
        defs = defof.get(name)
        if not defs or len(defs) != 1:        # skip ambiguous / external
            continue
        dst = next(iter(defs))
        for c in sites:
            if skip_tests and _TEST_RE.search(c["file"]):
                continue  # tests call everything — exclude from the architecture view
            src = file_node.get(c["file"], os.path.basename(c["file"]))
            if src and dst and src != dst:
                edges[(src, dst)] += 1
    return edges, file_node


# ---- PageRank over the call-graph (pure Python, no deps) ---------------------
def pagerank(edges, iterations=40, damping=0.85):
    """Personalized-PageRank-style centrality over component edges A->B (A calls B).
    Rank flows to the MOST DEPENDED-UPON components (high incoming weight) — the
    architectural core. Returns {component: score}. Deterministic, no NetworkX."""
    from collections import defaultdict
    out, nodes = defaultdict(list), set()
    for (a, b), w in edges.items():
        nodes.add(a); nodes.add(b); out[a].append((b, w))
    n = len(nodes) or 1
    pr = {x: 1.0 / n for x in nodes}
    outw = {a: sum(w for _, w in lst) for a, lst in out.items()}
    for _ in range(iterations):
        nxt = {x: (1 - damping) / n for x in nodes}
        dangling = damping * sum(pr[x] for x in nodes if outw.get(x, 0) == 0) / n
        for a, lst in out.items():
            if outw[a]:
                share = damping * pr[a] / outw[a]
                for b, w in lst:
                    nxt[b] += share * w
        for x in nodes:
            nxt[x] += dangling
        pr = nxt
    return pr


# ---- Mermaid code-map (deterministic, no AI) --------------------------------
def build_mermaid(idx, max_edges=40):
    """Class/module dependency graph (Mermaid). Returns (block, n_edges, n_nodes)."""
    edges, _ = class_edges(idx)
    top = edges.most_common(max_edges)

    def nid(x):
        return "n_" + re.sub(r"\W", "_", x)
    nodes = sorted({n for (s, d), _ in top for n in (s, d)})
    out = ["```mermaid", "graph LR"]
    for n in nodes:
        out.append(f'  {nid(n)}["{n.replace(chr(34), chr(39))}"]')
    for (s, d), w in top:
        # label sits between the arrow and the destination (`A -->|w| B`), NOT
        # after it — `A --> B|w|` is invalid mermaid and fails to render.
        out.append(f"  {nid(s)} -->" + (f"|{w}|" if w > 3 else "") + f" {nid(d)}")
    out.append("```")
    return "\n".join(out), len(top), len(nodes)


_MERMAID_NODE_RE = re.compile(r'^  n_\w+\["[^"\n]*"\]$')
_MERMAID_EDGE_RE = re.compile(r'^  n_\w+ -->(\|\d+\|)? n_\w+$')


def lint_mermaid(block):
    """Cheap structural check (regex, no external deps): flag any content line
    that isn't a well-formed node declaration or edge. Returns [(line_no, text)]."""
    lines = block.splitlines()
    bad = []
    for i, line in enumerate(lines[2:-1], start=3):  # skip ```mermaid / graph LR / trailing ```
        if not (_MERMAID_NODE_RE.match(line) or _MERMAID_EDGE_RE.match(line)):
            bad.append((i, line))
    return bad


def cmd_selftest():
    """Regression guard for build_mermaid(), no repo/index needed: feeds synthetic
    edges through it and asserts lint_mermaid() finds nothing. Run in CI/dev to
    catch this class of bug (invalid mermaid shipped to a wiki page) before it
    ships — this is exactly what would have caught the `A --> B|16|` bug."""
    from collections import Counter
    global class_edges
    fake_idx = {"symbols": []}
    orig = class_edges
    class_edges = lambda idx: (Counter({("A", "B"): 16, ("C", "D"): 1, ("A", "C"): 4}), None)
    try:
        mer, ne, nn = build_mermaid(fake_idx)
        bad = lint_mermaid(mer)
    finally:
        class_edges = orig
    if bad:
        print("FAIL — build_mermaid() produced invalid mermaid:")
        for i, t in bad:
            print(f"  L{i}: {t}")
        return 1
    print(f"OK — build_mermaid() self-test passed ({ne} edges, {nn} nodes, 0 lint errors)")
    return 0


def write_code_map(root, scope, idx):
    """Write the Mermaid code-map as a wiki page (auto-doc expansion). No AI."""
    from collections import Counter
    mer, ne, nn = build_mermaid(idx)
    bad = lint_mermaid(mer)
    if bad:
        # Self-heal: drop malformed lines rather than ship a diagram GitHub can't
        # render, and leave a one-shot advisory for recall-hook.py to surface on
        # the next prompt (this runs detached in the background, so nothing is
        # watching stdout right now).
        drop = {i - 1 for i, _ in bad}
        mer = "\n".join(l for i, l in enumerate(mer.splitlines()) if i not in drop)
        msg = (f"⚠ mermaid code-map ({scope}): dropped {len(bad)} malformed line(s) "
               f"before writing — fix build_mermaid() in symbols.py:\n" +
               "\n".join(f"  L{i}: {t}" for i, t in bad[:5]))
        print(msg)
        try:
            with open("/home/claude/.claude-memory/.mermaid_lint_advice", "w") as fh:
                fh.write(msg)
        except Exception:
            pass
    classes = Counter()
    for s in idx["symbols"]:
        if s["class"]:
            classes[s["class"]] += 1
    edges, _ = class_edges(idx)
    pr = pagerank(edges)
    core = sorted(pr.items(), key=lambda x: -x[1])[:12]
    summary = f"Code map: {scope} — {nn} components, {ne} call-dependencies (top); core: " + \
              ", ".join(c for c, _ in core[:4])
    body = [summary, "",
            "_Auto-generated (deterministic, no AI) from the symbol index._", "",
            "## Core components (PageRank — most depended-upon)",
            *[f"- `{c}` — {s:.3f}" for c, s in core], "",
            "## Call dependencies (who calls whom)", mer, "",
            "## Largest components (by member count)",
            *[f"- `{c}` — {n} members" for c, n in classes.most_common(15)]]
    try:
        import wiki_lib
        r = wiki_lib.write_page("code-map", f"{scope} code map", "\n".join(body), cwd=root,
                                scope=scope, tags=["code-map", "mermaid", "structure", "auto"],
                                summary=summary[:200])
        return bool(r.get("ok"))
    except Exception:
        return False


def cmd_gen(root, scope, incremental=True):
    p = _index_path(root, write=True)
    mode = "full"
    d = None
    if incremental and os.path.exists(p):
        try:
            prev = json.load(open(p))
            cur_head = _git(root, "rev-parse", "--short", "HEAD").strip()
            # a commit since the last index may have merged cross-file changes the
            # incremental approximation doesn't capture — rebuild fully in that case.
            if cur_head and prev.get("head") and cur_head != prev.get("head"):
                d = None
            else:
                inc, info = build_incremental(root, prev, os.path.getmtime(p))
                if inc is not None:
                    d, mode = inc, f"incremental(+{info})"
        except Exception:
            d = None
    if d is None:
        d = build(root)
    head = _git(root, "rev-parse", "--short", "HEAD").strip()
    stamp = subprocess.run(["date", "-u", "+%Y-%m-%dT%H:%M:%SZ"], capture_output=True, text=True).stdout.strip()
    # annotate archived first-party code (t-8002a9): recomputed on every gen from the
    # file path (cheap + deterministic), so incremental builds stay correct too
    arch = archived_dirs(root)
    n_arch = 0
    for s in d["symbols"]:
        if _is_archived(s.get("file", ""), arch):
            s["archived"] = True
            n_arch += 1
        else:
            s.pop("archived", None)
    # coverage counts LIVE code only — an archived tree nobody will document must not
    # drag the number down (the nag it produces is unactionable)
    live_fns = [s for s in d["symbols"] if s["kind"] in ("function", "method")
                and not s.get("archived")]
    doc_total = len(live_fns)
    doc_have = sum(1 for s in live_fns if s["doc"])
    out = {"generated": stamp, "scope": scope, "head": head, "files": d["files"],
           "n_symbols": len(d["symbols"]), "n_archived": n_arch,
           "doc_coverage": round(doc_have / doc_total, 3) if doc_total else 1.0,
           "symbols": d["symbols"], "calls": d["calls"], "refs": d.get("refs", [])}
    os.makedirs(os.path.dirname(p), exist_ok=True)
    json.dump(out, open(p, "w"), separators=(",", ":"))
    # The Mermaid code-map + EXHAUSTIVE symbol-map are the COMMITTED discovery copy and
    # are large/churny (symbol-map.md runs to tens of thousands of lines). Only (re)write
    # them into the worktree on --publish (CI/manual); local regen keeps the worktree
    # pristine and readers use the last published copy. (autowiki rework, Phase 1)
    mapped = False
    if _PUBLISH:
        # auto-doc expansion: refresh the Mermaid code-map wiki page alongside the index
        mapped = write_code_map(root, scope, out)
        # ...and the EXHAUSTIVE symbol map. code-map is the ranked visual summary; symbol-map
        # is the full reference (every symbol: file:line, signature, docblock, callers,
        # callees). Regenerated here so it can never drift from the index it is derived from.
        try:
            write_symbol_map(root, scope)
        except Exception:
            pass
    # An index of ZERO symbols is a failure wearing a success costume: run from a non-repo cwd
    # (e.g. /root) this printed {"ok":true,"symbols":0,"doc_coverage":1.0} and wrote a healthy
    # -looking index, because doc_coverage defaults to 1.0 when there is nothing to document.
    # Reported by the fleet-CI session (m-064) and reproduced exactly. Say so, and exit non-zero
    # so a caller/cron can actually notice.
    empty = len(d["symbols"]) == 0
    result = {"ok": not empty, "path": p, "symbols": len(d["symbols"]),
              "doc_coverage": out["doc_coverage"], "callable": doc_total,
              "code_map": mapped}
    if empty:
        result["warning"] = (f"indexed 0 symbols under {root} — this is almost certainly the "
                             f"WRONG cwd (not a repo root), not an empty project. "
                             f"doc_coverage=1.0 here means 'nothing to document', not 'healthy'.")
    print(json.dumps(result))
    try:   # heartbeat for the health check (background runs are otherwise invisible)
        import obs
        obs.record("symbols-gen", summary=f"{scope}: {len(d['symbols'])} symbols, "
                   f"{d['files']} files, doc {out['doc_coverage']}, {mode}, code_map={mapped}",
                   raw=json.dumps(result), ok=True)
    except Exception:
        pass
    if empty:
        sys.exit(2)


def _load(root):
    try:
        return json.load(open(_index_path(root)))
    except Exception:
        return None


# ---- unresolved calls: references to symbols that DO NOT EXIST ---------------
# The index already knows every DEFINITION (symbols) and every CALL SITE (calls: name ->
# [{file,line,text}]). The gap it never closed: a call whose target is defined NOWHERE. Those
# are typos, functions renamed on one side of a refactor, and APIs that were assumed rather
# than checked — the failure mode where code looks right and dies at runtime.
#
# PRECISION IS THE WHOLE FEATURE. A naive "call name not in symbol names" diff flags append,
# print, every stdlib call and every method on an object — hundreds of false positives. A noisy
# detector gets ignored (the auto-task suggester died at a 4% action rate for exactly this
# reason). So a call is reported ONLY when every cheap explanation is exhausted:
#   * the name is defined nowhere in the repo index
#   * it is not a language builtin (Python: builtins module; PHP: get_defined_functions(),
#     1233 real internal names, queried at runtime rather than guessed)
#   * the call site is a BARE call `name(` — never `.name(` / `->name(` / `::name(`, because a
#     method on an object we cannot resolve is unknowable, not wrong
#   * the name is not imported in that file (import / from-import / use)
#   * length >= 3, matching CALL_RE, so short names that are never indexed cannot false-fire
_PY_BUILTINS = None
_PHP_BUILTINS = None


def _py_builtins():
    global _PY_BUILTINS
    if _PY_BUILTINS is None:
        import builtins as _b
        _PY_BUILTINS = {n for n in dir(_b)}
        _PY_BUILTINS |= {"self", "cls", "super", "print", "range", "len", "open"}
    return _PY_BUILTINS


def _php_builtins():
    global _PHP_BUILTINS
    if _PHP_BUILTINS is None:
        out = set()
        try:
            r = subprocess.run(["php", "-r",
                                "echo implode(\",\", get_defined_functions()[\"internal\"]);"],
                               capture_output=True, text=True, timeout=20)
            out = {x.strip() for x in (r.stdout or "").split(",") if x.strip()}
        except Exception:
            pass
        _PHP_BUILTINS = out or {"count", "implode", "explode", "array_map", "json_encode"}
    return _PHP_BUILTINS


# Browser/JS/TS ambient globals — defined by the runtime, never by the repo. Without these,
# every setTimeout/requestAnimationFrame in a frontend file reads as a missing symbol.
_JS_GLOBALS = {
    "console", "window", "document", "navigator", "location", "history", "localStorage",
    "sessionStorage", "fetch", "setTimeout", "setInterval", "clearTimeout", "clearInterval",
    "requestAnimationFrame", "cancelAnimationFrame", "alert", "confirm", "prompt",
    "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI", "parseInt",
    "parseFloat", "isNaN", "isFinite", "String", "Number", "Boolean", "Array", "Object",
    "Date", "Math", "JSON", "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol", "Error",
    "TypeError", "RangeError", "RegExp", "Proxy", "Reflect", "BigInt", "Intl", "URL",
    "URLSearchParams", "FormData", "Headers", "Request", "Response", "Blob", "File",
    "FileReader", "WebSocket", "Worker", "Image", "Audio", "Event", "CustomEvent",
    "IntersectionObserver", "MutationObserver", "ResizeObserver", "AbortController",
    "structuredClone", "queueMicrotask", "atob", "btoa", "require", "define", "describe",
    "it", "expect", "beforeEach", "afterEach", "jest", "vi", "test",
}
# Language CONSTRUCTS (not functions, so get_defined_functions never lists them), built-in
# CLASSES, CSS functions that appear inside embedded <style> blocks, and C#/.NET BCL names.
# Each of these produced a whole false-positive family on a polyglot repo: declare() 198x,
# var() from CSS custom properties, NOW()/COUNT() from SQL heredocs, RuntimeException 28x.
_LANG_CONSTRUCTS = {
    "declare", "isset", "empty", "unset", "list", "array", "echo", "print", "exit", "die",
    "include", "include_once", "require", "require_once", "eval", "clone", "instanceof",
    "yield", "throw", "catch", "match", "fn", "static", "parent", "self", "endif", "elseif",
    "nameof", "typeof", "sizeof", "await", "async", "using", "lock", "checked", "unchecked",
    "default", "params", "readonly", "record", "when", "where", "select", "from", "let",
}
_CSS_FUNCS = {
    "var", "calc", "clamp", "minmax", "repeat", "rgba", "rgb", "hsl", "hsla", "url",
    "translate", "translateX", "translateY", "translate3d", "rotate", "rotateX", "rotateY",
    "scale", "scaleX", "scaleY", "skew", "matrix", "linear", "ease", "steps", "cubic",
    "cubicBezier", "attr", "counter", "env", "blur", "brightness", "contrast", "drop",
    "grayscale", "invert", "opacity", "saturate", "sepia", "polygon", "circle", "ellipse",
    "inset", "fit", "max", "min", "gradient", "linearGradient", "radialGradient",
}
_BCL = {
    "Exception", "RuntimeException", "InvalidArgumentException", "LogicException",
    "ArgumentNullException", "ArgumentException", "InvalidOperationException",
    "NotImplementedException", "NotSupportedException", "KeyNotFoundException",
    "OutOfRangeException", "TypeError", "ValueError", "JsonException", "PDOException",
    "DateTime", "DateTimeImmutable", "DateInterval", "DateTimeZone", "ArrayObject",
    "SplStack", "SplQueue", "Generator", "Closure", "stdClass", "Traversable", "Iterator",
    "IteratorAggregate", "ArrayAccess", "Countable", "JsonSerializable", "Stringable",
    "Throwable", "Task", "List", "Dictionary", "HashSet", "IEnumerable", "ICollection",
    "IList", "IDictionary", "Guid", "TimeSpan", "DateTimeOffset", "CancellationToken",
    "StringBuilder", "StringComparison", "Convert", "Encoding", "Path", "File", "Directory",
    "JsonSerializer", "JsonPropertyName", "StatusCode", "ILogger", "IServiceCollection",
}
_SQL_FUNCS = {
    "NOW", "COUNT", "VALUES", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IFNULL", "CONCAT",
    "CONCAT_WS", "DATE_SUB", "DATE_ADD", "DATEDIFF", "CURDATE", "CURTIME", "UNIX_TIMESTAMP",
    "FROM_UNIXTIME", "GROUP_CONCAT", "SUBSTRING", "CHAR_LENGTH", "LOWER", "UPPER", "TRIM",
    "CAST", "CONVERT", "IF", "NULLIF", "GREATEST", "LEAST", "ROW_NUMBER", "RANK", "JSON_EXTRACT",
    "JSON_OBJECT", "JSON_ARRAY", "LAST_INSERT_ID", "FOUND_ROWS", "DISTINCT", "ANY_VALUE",
}
_HEREDOC_RX = re.compile(r"<<<[\"']?(\w+)[\"']?")


_PHP_USE_RX = re.compile(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;", re.M)
_PHP_GROUP_RX = re.compile(r"^\s*use\s+[\w\\]+\{([^}]*)\}", re.M | re.S)
_JS_IMPORT_RX = re.compile(r"import\s*(?:([\w*]+)\s*,?\s*)?(?:\{([^}]*)\})?\s*from", re.S)


def _imported_names(root, rel):
    """Every name this file imports. An imported symbol is defined elsewhere, not missing.

    Python goes through `ast` rather than a regex: the first version used a single-line
    pattern and silently missed multi-line parenthesised imports
    (`from sqlalchemy import (\n Column,\n Table,\n)`), which alone produced 385 of 434
    false positives on one repo — every Column()/Table() call read as undefined."""
    names = set()
    path = os.path.join(root, rel)
    try:
        src = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return names
    if rel.endswith(".py"):
        try:
            for node in ast.walk(ast.parse(src)):
                if isinstance(node, ast.Import):
                    for al in node.names:
                        names.add(al.asname or al.name.split(".")[0])
                elif isinstance(node, ast.ImportFrom):
                    for al in node.names:
                        names.add(al.asname or al.name)
        except Exception:
            pass                                  # unparseable: fall through to regex below
    for m in _PHP_USE_RX.finditer(src):
        names.add(m.group(2) or m.group(1).split("\\")[-1])
    for m in _PHP_GROUP_RX.finditer(src):
        for part in re.split(r"[,\s]+", m.group(1)):
            part = part.strip()
            if part:
                names.add(part.split("\\")[-1])
    for m in _JS_IMPORT_RX.finditer(src):
        if m.group(1):
            names.add(m.group(1).strip())
        for part in re.split(r"[,\s]+", m.group(2) or ""):
            part = part.strip()
            if part and part != "as":
                names.add(part)
    return names


def _bare_call(text, name):
    """True when the source shows `name(` and NOT `.name(` / `->name(` / `::name(`."""
    m = re.search(r"([.>:]?)\s*\b" + re.escape(name) + r"\s*\(", text or "")
    if not m:
        return False
    pre = (text or "")[:m.start(0)].rstrip()
    return not (pre.endswith(".") or pre.endswith("->") or pre.endswith("::") or m.group(1))


def _code_only(line):
    """Return the line with comments AND string CONTENTS removed.

    Scanning raw lines produced 713 false positives on one repo: prose in docstrings and
    f-strings parses as calls — "Egress build gate (ticket 0.1)" -> gate(), "http(s)://" ->
    http(), "URL(s) found" -> URL(). Only real code may be scanned, so string bodies are
    blanked (quotes kept, so structure survives) and trailing comments are cut."""
    out, i, quote = [], 0, None
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")            # blank the string body
        elif ch in "\"'":
            quote = ch
            out.append(ch)
        elif ch == "#":
            break
        elif ch == "/" and i + 1 < len(line) and line[i + 1] in "/*":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out)


# A real call is `name(` — no space. Prose is "gate (ticket 0.1)". Requiring adjacency is the
# single highest-precision filter available here; `foo (x)` is legal but vanishingly rare in
# real code, and losing it is far cheaper than drowning the signal in prose.
STRICT_CALL_RE = re.compile(r"\b([A-Za-z_]\w{2,})\(")
_TRIPLE_RE = re.compile(r'("""|\'\'\')')


def unresolved_calls(root):
    """Scan SOURCE directly — not idx['calls'].

    idx['calls'] is a reverse-call map built only for names that ARE defined symbols, so by
    construction it can never contain an undefined name. Searching it for unresolved calls
    found nothing on a file that deliberately called a nonexistent function. The call sites
    have to come from the source itself.
    """
    idx = _load(root)
    if not idx:
        return None
    defined = {s["name"] for s in idx["symbols"]}
    classes = {s["name"] for s in idx["symbols"] if s["kind"] in ("class", "interface", "trait")}
    langs = {s.get("lang") for s in idx["symbols"]}
    skip = set(KEYWORDS) | set(COMMON) | classes | defined
    if "Python" in langs or True:
        skip |= _py_builtins()
    if "PHP" in langs:
        skip |= _php_builtins()
    skip |= _JS_GLOBALS | _LANG_CONSTRUCTS | _CSS_FUNCS | _BCL | _SQL_FUNCS
    out = []
    for rel in _tracked_sources(root):
        if _TEST_RE.search(rel):
            continue
        try:
            src = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        imported = _imported_names(root, rel)
        # names defined locally in THIS file (nested defs, lambdas assigned to a name, consts)
        # `function f`/`async function f` ANYWHERE on the line (embedded <script> in .php
        # declares handlers mid-file), arrow consts, and plain defs.
        local = set(re.findall(r"\b(?:async\s+)?function\s+(\w+)", src))
        local |= set(re.findall(r"^\s*(?:def|const|class|let|var)\s+(\w+)", src, re.M))
        local |= set(re.findall(r"\b(\w+)\s*[:=]\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>)", src))
        local |= set(re.findall(r"^\s*(\w+)\s*=\s*lambda", src, re.M))
        # parameters are callables sometimes (callbacks) — treat any bare word that is a param
        # of the enclosing def as known, cheaply: collect all param names in the file
        for pm in re.finditer(r"(?:def|function)\s+\w+\s*\(([^)]*)\)", src):
            for prm in re.split(r"[,\s]+", pm.group(1)):
                prm = prm.strip().lstrip("*&$").split(":")[0].split("=")[0]
                if prm:
                    local.add(prm)
        in_doc = False
        heredoc = None
        for ln_no, line in enumerate(src.splitlines(), 1):
            if heredoc:                        # PHP/JS heredoc body = data (often SQL/CSS)
                if line.strip().rstrip(";").strip() == heredoc:
                    heredoc = None
                continue
            _hd = _HEREDOC_RX.search(line)
            if _hd:
                heredoc = _hd.group(1)
                continue
            ticks = len(_TRIPLE_RE.findall(line))
            if in_doc:
                if ticks % 2 == 1:
                    in_doc = False
                continue                       # inside a docstring: prose, never code
            if ticks % 2 == 1:
                in_doc = True
                continue
            stripped = line.strip()
            if stripped.startswith(("#", "//", "*", "/*", "[")):
                continue                       # incl. C#/attribute lines: [HttpGet(...)]
            code = _code_only(line)
            for m in STRICT_CALL_RE.finditer(code):
                name = m.group(1)
                if name in skip or name in imported or name in local or len(name) < 3:
                    continue
                if not _bare_call(code, name):
                    continue
                ext = rel.rsplit(".", 1)[-1].lower()
                # HIGH only where call-site extraction is verifiable: Python imports come from
                # `ast`, so "not defined, not imported, not builtin" is trustworthy (measured
                # 0 false positives across two real repos). PHP/JS/C# call sites are regex-
                # scanned per symbols.py's own best-effort extractor, so they stay BEST-EFFORT
                # and are hidden unless asked for — a noisy default gets the whole tool ignored.
                conf = "high" if ext == "py" else "best-effort"
                out.append({"name": name, "file": rel, "line": ln_no,
                            "text": stripped[:120], "lang": ext, "confidence": conf})
    out.sort(key=lambda r: (r["file"], r["line"]))
    return out


def write_unresolved_page(root, rows):
    """Write the findings to an in-repo page so the HOOK can inject a one-line POINTER
    instead of the findings themselves. Injecting N findings every turn costs N lines of
    context forever; injecting 'see this file' costs one line and is read on demand."""
    hi = [r for r in rows if r.get("confidence") == "high"]
    be = [r for r in rows if r.get("confidence") != "high"]
    d = os.path.join(root, ".claude", "wiki")
    os.makedirs(d, exist_ok=True)
    pth = os.path.join(d, "unresolved-calls.md")
    out = ["# Unresolved calls", "",
           "Calls whose target is defined **nowhere** in this repo — not a definition, not a",
           "language builtin, not an import, and not a method on an object. Each is a typo, a",
           "half-finished rename, or an API that was assumed rather than checked.", "",
           f"- high confidence (Python, imports resolved via `ast`): **{len(hi)}**",
           f"- best-effort (PHP/JS/C#, regex call-site scan): **{len(be)}**", "",
           "Regenerate: `symbols.py unresolved --write`", ""]
    for title, rowset in (("## High confidence", hi), ("## Best effort", be)):
        if not rowset:
            continue
        out += [title, ""]
        cur = None
        for r in rowset:
            if r["file"] != cur:
                cur = r["file"]
                out.append(f"### `{cur}`")
            out.append(f"- **{r['name']}()** — line {r['line']}  \n  `{r['text']}`")
        out.append("")
    open(pth, "w").write("\n".join(out))
    return pth


def _callees_index(idx):
    """name -> set(names it calls), derived from call SITES grouped by containing symbol.

    idx['calls'] is keyed by the CALLED name with the call-site file+line. To get the reverse
    (what does X call?) we map each site back to the symbol whose body contains that line."""
    by_file = {}
    for sym in idx["symbols"]:
        by_file.setdefault(sym["file"], []).append(sym)
    for f in by_file:
        by_file[f].sort(key=lambda s: s["line"])
    def owner(file, line):
        best = None
        for sym in by_file.get(file, []):
            if sym["line"] <= line and sym["kind"] in ("function", "method"):
                best = sym
            elif sym["line"] > line:
                break
        return best["name"] if best else None
    out = {}
    for called, sites in (idx.get("calls") or {}).items():
        for site in sites:
            o = owner(site.get("file", ""), site.get("line") or 0)
            if o and o != called:
                out.setdefault(o, set()).add(called)
    return out


def write_symbol_map(root, scope=None):
    """Write an EXTENSIVE per-symbol reference page: every symbol with file:line, signature,
    docblock, who calls it (with call-site lines) and what it calls.

    Deliberately NOT injected — it is large by design and lives in-repo to be READ on demand
    (the recall hook points at it). code-map.md stays the ranked visual summary; this is the
    exhaustive index behind it, so 'what exists and how is it wired' is answerable without
    grepping after a compaction."""
    idx = _load(root)
    if not idx:
        return None
    calls = idx.get("calls") or {}
    callees = _callees_index(idx)
    syms = sorted(idx["symbols"], key=lambda s: (s["file"], s["line"]))
    by_file = {}
    for sym in syms:
        by_file.setdefault(sym["file"], []).append(sym)
    # archived trees get their own trailing section instead of interleaving (t-8002a9):
    # the map should describe what is actually RUNNING first, the record second
    arch_files = sorted(f for f, ss in by_file.items() if all(s.get("archived") for s in ss))
    live_files = sorted(f for f in by_file if f not in set(arch_files))
    live_syms = [s for f in live_files for s in by_file[f]]
    documented = sum(1 for s in live_syms if s.get("doc"))
    out = [
           f"Every function, method and class in this repo: signature, `file:line`, docblock, "
           f"callers (with call-site lines) and callees.", "",
           f"- symbols: **{len(live_syms)}** across **{len(live_files)}** files"
           + (f" (+{len(syms) - len(live_syms)} archived, sectioned at the end)"
              if arch_files else ""),
           f"- documented: **{documented}/{len(live_syms)}** "
           f"({100*documented//max(1,len(live_syms))}%) — live code only",
           f"- generated by `symbols.py map` (deterministic, no AI) — "
           f"see also `code-map.md` (visual) and `unresolved-calls.md` (bad references)", "",
           "## Index", ""]
    for f in live_files:
        anchor = re.sub(r"[^a-z0-9]+", "-", f.lower()).strip("-")
        out.append(f"- [`{f}`](#{anchor}) — {len(by_file[f])} symbols")
    if arch_files:
        out.append("")
        out.append("**Archived** (indexed for search, excluded from quality signals):")
        for f in arch_files:
            anchor = re.sub(r"[^a-z0-9]+", "-", f.lower()).strip("-")
            out.append(f"- [`{f}`](#{anchor}) — {len(by_file[f])} symbols")
    out.append("")
    ordered = live_files + arch_files
    for f in ordered:
        if arch_files and f == arch_files[0]:
            out += ["---", "", "# Archived code", "",
                    "_Kept for the record; not built by CI. Indexed so `symbols find` "
                    "works, excluded from coverage/lint/dead. Configure via "
                    "`.claude/tooling.json` `archived_dirs`._", ""]
        out += ["---", "", f"## `{f}`", ""]
        for sym in by_file[f]:
            name, cls = sym["name"], sym.get("class") or ""
            label = f"{cls}::{name}" if cls else name
            sites = calls.get(name, [])
            head = f"### `{label}{sym.get('signature','')}`"
            out.append(head)
            meta = [f"`{f}:{sym['line']}`", sym.get("lang", ""), sym["kind"]]
            out.append("  ".join(x for x in meta if x))
            out.append("")
            if sym.get("doc") and sym.get("doc_summary"):
                out.append(f"> {sym['doc_summary']}")
            elif not sym.get("doc"):
                out.append("> _no docblock_")
            out.append("")
            if sites:
                out.append(f"**Called by ({len(sites)}):**")
                for st in sites[:12]:
                    out.append(f"- `{st.get('file')}:{st.get('line')}` — "
                               f"`{(st.get('text') or '').strip()[:90]}`")
                if len(sites) > 12:
                    out.append(f"- _…and {len(sites)-12} more_")
            else:
                out.append("**Called by:** _nothing in this repo_ "
                           "(entry point, framework-invoked, or dead — see `symbols.py dead`)")
            cal = sorted(callees.get(name, []))
            if cal:
                out.append("")
                out.append(f"**Calls:** " + ", ".join(f"`{c}`" for c in cal[:20])
                           + (f" _…+{len(cal)-20}_" if len(cal) > 20 else ""))
            out.append("")
    # Register through wiki_lib so the page is INDEXED and discoverable (summary + tags),
    # exactly like code-map — an unindexed file on disk is not findable by the recall layer.
    try:
        import wiki_lib
        r = wiki_lib.write_page("symbol-map", f"{scope or os.path.basename(root)} symbol map",
                                "\n".join(out), cwd=root, scope=scope,
                                tags=["symbol-map", "symbols", "reference", "auto"],
                                summary=(f"Every symbol in {scope or os.path.basename(root)}: "
                                         f"{len(syms)} across {len(by_file)} files, with "
                                         f"file:line, signature, docblock, callers and callees."))
        # Use the path wiki_lib REPORTS. Reconstructing <repo>/.claude/wiki/... is wrong for
        # repos whose scope resolves to global — the page lands in the global wiki dir and the
        # assumed local path does not exist (FileNotFoundError on .claude-memory).
        pth = (r.get("path") if isinstance(r, dict) else r) or os.path.join(
            root, ".claude", "wiki", "symbol-map.md")
    except Exception:
        d = os.path.join(root, ".claude", "wiki")
        os.makedirs(d, exist_ok=True)
        pth = os.path.join(d, "symbol-map.md")
        open(pth, "w").write("\n".join(out))
    return pth


def cmd_map(root, scope=None):
    pth = write_symbol_map(root, scope)
    if not pth:
        print("no symbol index — run: symbols.py gen"); return
    size = os.path.getsize(pth)
    idx = _load(root)
    print(f"symbol map written: {pth}")
    print(f"  {len(idx['symbols'])} symbols, {size//1024}kB — read on demand, never injected")


def cmd_unresolved(root, as_json=False, write=False, show_all=False):
    rows = unresolved_calls(root)
    if rows is None:
        print("no symbol index — run: symbols.py gen"); return
    allrows = rows
    if not show_all:
        rows = [r for r in rows if r.get("confidence") == "high"]
    if as_json:
        print(json.dumps(allrows if show_all else rows, indent=1)); return
    if not rows:
        n_be = len([r for r in allrows if r.get("confidence") != "high"])
        print("no high-confidence unresolved calls — every bare call resolves to a "
              "definition, builtin or import")
        if n_be:
            print(f"  ({n_be} best-effort hits in regex-scanned languages — `unresolved --all`)")
        if write:
            print(f"  written: {write_unresolved_page(root, allrows)}")
        return
    print(f"{len(rows)} call(s) to symbols that are defined NOWHERE "
          f"(not a builtin, not imported, not a method):\n")
    cur = None
    for r in rows:
        if r["file"] != cur:
            cur = r["file"]; print(f"  {cur}")
        print(f"    :{r['line']:<5} {r['name']}()   {r['text']}")
    print("\n  Each is a typo, a half-finished rename, or an API assumed rather than checked.")
    if write:
        pth = write_unresolved_page(root, rows)
        print(f"  written: {pth}")


def _locate_span(root, rel, name):
    """Exact (start_line, end_line) of the named function/method/class in `rel`, 1-based
    inclusive, parser-derived. Exists because regex extraction shipped two real bugs
    (t-99cfce): default-param braces `(opts = {})` broke a body detector, and a manual
    brace-matcher silently ate 773 lines of a file containing GLSL template literals.
    A CST cannot make either mistake. Returns None when not found / no parser."""
    path = os.path.join(root, rel)
    if not os.path.isfile(path):
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext == ".py":
        import ast
        try:
            tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        except SyntaxError:
            return None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                    and node.name == name:
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                return (start, node.end_lineno)
        return None
    gram = dict(TS_LANG, **{".php": ("php", "PHP")}).get(ext)
    if not gram:
        return None
    parser = _ts_parser(gram[0])
    if parser is None:
        return None
    try:
        tree = parser.parse(open(path, "rb").read())
    except Exception:
        return None
    best = None

    def walk(n):
        nonlocal best
        for ch in n.children:
            if best is not None:
                return
            t = ch.type
            if (t in _TS_FUNC or t in _TS_CLASS) and _ts_name(ch) == name:
                best = ch
                return
            # JS/TS: const foo = () => {} / foo: function () {} / class fields
            if t in ("variable_declarator", "public_field_definition", "field_definition",
                     "pair") and _ts_name(ch) == name \
                    and any(c.type in ("arrow_function", "function_expression", "function",
                                       "generator_function") for c in ch.children):
                best = ch
                return
            walk(ch)

    walk(tree.root_node)
    if best is None:
        return None
    # a matched declarator sits inside its declaration statement — cut the whole line
    node = best
    if node.type == "variable_declarator" and node.parent is not None \
            and node.parent.type in ("lexical_declaration", "variable_declaration"):
        node = node.parent
    return (node.start_point[0] + 1, node.end_point[0] + 1)


def cmd_body(root, rel, name):
    span = _locate_span(root, rel, name)
    if not span:
        print(f"'{name}' not found in {rel} (or no parser for this language). "
              f"Coverage: tree-sitter langs + .py + .php; VB has no CST.", file=sys.stderr)
        return 1
    s, e = span
    lines = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read().splitlines()
    print(f"{rel}:{s}-{e}  ({e - s + 1} lines)")
    for i in range(s - 1, min(e, len(lines))):
        print(lines[i])
    return 0


def cmd_cut(root, rel, name):
    """Emit a unified diff deleting the symbol's exact span (never edits the file —
    apply with `git apply` after review). Preceding doc comments are NOT included."""
    import difflib
    span = _locate_span(root, rel, name)
    if not span:
        print(f"'{name}' not found in {rel} (or no parser for this language).", file=sys.stderr)
        return 1
    s, e = span
    orig = open(os.path.join(root, rel), encoding="utf-8", errors="replace").read().splitlines(keepends=True)
    new = orig[:s - 1] + orig[e:]
    sys.stdout.writelines(difflib.unified_diff(orig, new, fromfile=f"a/{rel}", tofile=f"b/{rel}"))
    print(f"# span {rel}:{s}-{e} ({e - s + 1} lines) — apply with: git apply <patchfile>",
          file=sys.stderr)
    return 0


def cmd_find(root, query):
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return
    q = query.lower()
    exact = [s for s in idx["symbols"] if s["name"].lower() == q]
    sub = [s for s in idx["symbols"] if q in s["name"].lower() and s not in exact]
    hits = (exact + sub)[:15]
    for s in hits:
        cls = (s["class"] + "::") if s["class"] else ""
        nc = len(idx["calls"].get(s["name"], []))
        doc = ("📝 " + s["doc_summary"]) if s["doc"] else "⚠ no docblock"
        arch = "ARCHIVED " if s.get("archived") else ""
        print(f"{cls}{s['name']}{s['signature']}  [{arch}{s['lang']} {s['kind']}] "
              f"{s['file']}:{s['line']}  ({nc} callers)  {doc}")
    if not hits:
        # cross-hint the sibling finder — this is the code index, not the filesystem.
        print(f"no symbol '{query}' in the code index. "
              f"For files/content on disk use `fnd {query}`; to (re)build the index: symbols.py gen")


def cmd_callers(root, name):
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return
    for c in idx["calls"].get(name, []):
        print(f"{c['file']}:{c['line']}  {c['text']}")
    if not idx["calls"].get(name):
        print(f"(no recorded callers of {name})")


def cmd_lint(root, lang, strict):
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return 0
    langf = {"php": "PHP", "py": "Python", "python": "Python", "cs": "C#"}.get((lang or "").lower())
    fns = [s for s in idx["symbols"] if s["kind"] in ("function", "method")
           and (not langf or s["lang"] == langf) and not s.get("archived")]
    n_arch = sum(1 for s in idx["symbols"] if s["kind"] in ("function", "method")
                 and (not langf or s["lang"] == langf) and s.get("archived"))
    if n_arch:
        print(f"({n_arch} archived symbol(s) excluded — legacy/archive trees are indexed "
              f"but not linted; see .claude/tooling.json archived_dirs)")
    missing = [s for s in fns if not s["doc"]]
    gen = [s for s in fns if s["doc"] and s.get("generated")]
    human = len(fns) - len(missing) - len(gen)
    cov = round(1 - len(missing) / len(fns), 3) if fns else 1.0
    # acknowledged false positives (.claude/checks-ignore.json) — count, never hide silently
    import checks_ignore
    missing, sup = checks_ignore.split_hits(
        root, "lint", missing, lambda s: s["file"],
        lambda s: f"{(s['class'] + '::') if s['class'] else ''}{s['name']}")
    sup_note = f", {len(sup)} suppressed" if sup else ""
    print(f"docblock coverage{(' [' + langf + ']') if langf else ''}: "
          f"{len(fns) - len(missing) - len(sup)}/{len(fns)} = {cov:.0%}  "
          f"({human} human, {len(gen)} @generated/unverified, {len(missing)} missing{sup_note})")
    for s in missing[:60]:
        cls = (s["class"] + "::") if s["class"] else ""
        print(f"  ⚠ {s['file']}:{s['line']}  {cls}{s['name']}{s['signature']}")
    if len(missing) > 60:
        print(f"  … and {len(missing) - 60} more")
    return 1 if (strict and cov < 0.8) else 0


# names invoked by frameworks / runtimes, not by explicit calls in the code
_ENTRY_NAMES = {"main", "handle", "run", "boot", "register", "index", "up", "down",
                "setUp", "tearDown", "execute", "invoke", "jsonSerialize", "process",
                "__construct", "__invoke", "configure", "handleRequest"}
_FRAMEWORK_CLASS = re.compile(r"(Controller|Routes?|Router|Middleware|Command|Migration|"
                              r"Seeder|Provider|Subscriber|Listener|Handler|Job|Event)$")


def cmd_dead(root, show_all=False):
    """Report symbols with ZERO callers in the index = dead-code CANDIDATES.
    Tiered by confidence; entry points, magic/framework methods, and tests excluded.
    Caveat: dynamic dispatch / reflection / external API use can't be seen — verify."""
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return
    calls = idx["calls"]
    # First pass: symbols with no DIRECT call site (name-based). Then spare any that are
    # referenced as a method-group/delegate (LINQ predicate, event handler, callback) —
    # a call-only scan can't see those, so they were guaranteed false positives.
    cand = []
    for s in idx["symbols"]:
        if s["kind"] not in ("function", "method"):
            continue
        if s.get("archived"):                    # archived tree: indexed, never nagged (t-8002a9)
            continue
        name, cls = s["name"], s.get("class", "")
        if len(name) < 3:                        # too short to track by name (CALL_RE needs ≥3);
            continue                             # its call sites are never indexed → always a FP
        if name.startswith("__") or name in _ENTRY_NAMES or name == cls:  # ctor/magic/entry
            continue
        if s.get("override"):                    # base/framework-invoked via polymorphism
            continue
        if _TEST_RE.search(s["file"]):
            continue
        if calls.get(name):                      # has at least one call site
            continue
        cand.append(s)
    refs = (set(idx["refs"]) if "refs" in idx
            else _referenced_names(root, [s["name"] for s in cand]))
    high, med, skipped, mgroup = [], [], 0, 0
    for s in cand:
        name, cls = s["name"], s.get("class", "")
        if name in refs:                         # used as a method group / delegate → not dead
            mgroup += 1
            continue
        private = (s.get("vis") in ("private", "protected")
                   or (name.startswith("_") and not name.startswith("__")))
        label = f"{(cls + '::') if cls else ''}{name}"
        rec = (f"{label}{s['signature']}  {s['file']}:{s['line']}", s["file"], label)
        if private:
            high.append(rec)                     # unreachable if unused → safe to remove
        elif cls and _FRAMEWORK_CLASS.search(cls):
            skipped += 1                          # framework-invoked public method
        else:
            med.append(rec)                      # public → may be external API / callback
    # acknowledged false positives (.claude/checks-ignore.json) — count, never hide silently
    import checks_ignore
    high, sup_h = checks_ignore.split_hits(root, "dead", high, lambda h: h[1], lambda h: h[2])
    med, sup_m = checks_ignore.split_hits(root, "dead", med, lambda h: h[1], lambda h: h[2])
    high = [h[0] for h in high]
    med = [h[0] for h in med]
    nsup = len(sup_h) + len(sup_m)
    sup_note = f", {nsup} suppressed via .claude/checks-ignore.json" if nsup else ""
    mg_note = f", {mgroup} method-group/delegate refs spared" if mgroup else ""
    print(f"dead-code candidates: {len(high)} high (unused private/protected), "
          f"{len(med)} review (unused public), {skipped} framework methods skipped"
          f"{mg_note}{sup_note}")
    if high:
        print("\nHIGH confidence — unused private/protected (safe to remove):")
        for r in (high if show_all else high[:40]):
            print("  ✗ " + r)
    if med:
        print("\nREVIEW — unused public (verify: external API, callback, or dynamic dispatch?):")
        for r in (med if show_all else med[:30]):
            print("  ? " + r)


def cmd_deadparams(root, show_all=False):
    """Functions/methods that DECLARE a parameter never referenced in their body —
    a hint at partial implementation, a stale signature after a refactor, or plain
    cruft. Conservative: abstract/interface bodies, overrides, constructor-promoted
    properties, and dynamic arg access (func_get_args/compact/extract) are excluded.
    Caveat: a param kept only to satisfy an interface/callback/override contract is a
    legitimate 'unused' — verify before deleting."""
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return
    # A method referenced as a method-group/delegate has a contract-bound signature
    # (event handler `OnFoo(sender,args)`, Unity `GUI.WindowFunction DrawWindow(int id)`,
    # LINQ/callback), so its "unused" params are legitimate — spare them. Also skip the
    # `On<Event>` handler-naming convention outright (delegate signature by convention).
    if "refs" in idx:
        refs = set(idx["refs"])
    else:
        cand_names = [s["name"] for s in idx["symbols"]
                      if s["kind"] in ("function", "method") and s.get("unused_params")]
        refs = _referenced_names(root, cand_names)
    hits, analyzed = [], 0
    for s in idx["symbols"]:
        if s["kind"] not in ("function", "method") or "unused_params" not in s:
            continue
        if s.get("archived"):                    # archived tree: indexed, never nagged (t-8002a9)
            continue
        analyzed += 1
        up = s.get("unused_params") or []
        if not up or s.get("generated"):
            continue
        name = s["name"]
        if name in refs or re.match(r"On[A-Z]", name):   # delegate/callback/event handler
            continue
        cls = (s.get("class") + "::") if s.get("class") else ""
        hits.append((s["file"], s["line"], f"{cls}{name}", up))
    hits.sort(key=lambda h: (h[0], h[1]))
    # acknowledged false positives (.claude/checks-ignore.json) — count, never hide silently
    import checks_ignore
    hits, suppressed = checks_ignore.split_hits(root, "deadparams", hits,
                                                lambda h: h[0], lambda h: h[2])
    sup_note = f" ({len(suppressed)} suppressed via .claude/checks-ignore.json)" if suppressed else ""
    print(f"unused-parameter candidates: {len(hits)} function(s) of {analyzed} analyzed{sup_note}")
    if not hits:
        return
    print("  (param declared but never referenced in body — verify interface/callback contracts)")
    shown = hits if show_all else hits[:60]
    for f, ln, nm, up in shown:
        print(f"  ⚠ {f}:{ln}  {nm} — unused: {', '.join(up)}")
    if len(hits) > len(shown):
        print(f"  … and {len(hits) - len(shown)} more (use: symbols.py deadparams all)")


def cmd_index(root, scope):
    """Upsert symbols into semantic memory (scope sym:<repo>) for fuzzy lookup.

    DEPRECATED / default-OFF: these sym:* points are NOT read by the hot path — lookup()
    uses the file index (symbols.json), and nothing recalls sym:* scope. Left unchecked
    they grew to ~70% of the Qdrant store and slowed every recall (hybrid search over 3×
    the vectors). Now a no-op unless SYMBOLS_SEM_INDEX=1 is set explicitly. Prefer
    `symbols.py find` (file-index fuzzy lookup) instead."""
    if os.environ.get("SYMBOLS_SEM_INDEX") != "1":
        print(json.dumps({"ok": True, "skipped": "deprecated (sym:* clutters the shared "
                          "memory store and is unused by recall; use `symbols.py find`). "
                          "Set SYMBOLS_SEM_INDEX=1 to force."}))
        return
    idx = _load(root)
    if not idx:
        print("no symbol index — run: symbols.py gen"); return
    subprocess.run([os.path.join(HERE, "ensure-server.sh")], timeout=25, capture_output=True)
    from mcp_client import McpHttpClient, memory_client
    c = memory_client()
    sscope = f"sym:{scope}"
    n = 0
    # prefer documented + public-looking symbols; cap to keep the store lean
    syms = sorted(idx["symbols"], key=lambda s: (not s["doc"], s["kind"] != "class"))[:600]
    for s in syms:
        cls = (s["class"] + "::") if s["class"] else ""
        body = (f"{cls}{s['name']}{s['signature']} [{s['lang']} {s['kind']}] @ "
                f"{s['file']}:{s['line']} — {s['doc_summary'] or 'no docblock'}")
        slug = re.sub(r"[^a-z0-9]+", "-", f"{s['file']}-{s['name']}".lower())[:60]
        try:
            c.call_tool("remember", {"content": body[:500], "tag": f"sym.{slug}", "scope": sscope})
            n += 1
        except Exception:
            pass
    print(json.dumps({"ok": True, "indexed": n, "scope": sscope}))


# ---- hot-path helpers (imported by recall-hook) -----------------------------
def lookup(cwd, terms, limit=3):
    """Return injectable hint lines for prompt terms that NAME an indexed symbol —
    so the agent recalls 'where/what is foo()' instead of re-grepping. Fast + det."""
    root = repo_root(cwd) or cwd
    idx = _load(root) if root else None
    if not idx:
        return []
    by = {}
    for s in idx["symbols"]:
        by.setdefault(s["name"].lower(), []).append(s)
    out, seen = [], set()
    for t in terms:
        for s in by.get(t, []):
            k = (s["file"], s["line"])
            if k in seen:
                continue
            seen.add(k)
            cls = (s["class"] + "::") if s["class"] else ""
            nc = len(idx["calls"].get(s["name"], []))
            doc = s["doc_summary"] if s["doc"] else "no docblock"
            out.append(f"{cls}{s['name']}{s['signature']} [{s['lang']} {s['kind']}] "
                       f"{s['file']}:{s['line']} ({nc} callers) — {doc}")
            if len(out) >= limit:
                return out
    return out


def coverage_info(cwd):
    """{scope, coverage, n} for the repo's symbol index, or None."""
    root = repo_root(cwd) or cwd
    idx = _load(root) if root else None
    if not idx:
        return None
    return {"scope": idx.get("scope"), "coverage": idx.get("doc_coverage", 1.0),
            "n": idx.get("n_symbols", 0)}


COMMANDS = {
    "gen": {"help": "(re)build the in-repo symbol index (.claude/symbols.json)", "eg": "symbols.py gen --cwd .", "power": "script"},
    "find": {"help": "look up a function/method/class by name (signature, file:line, callers)", "eg": "symbols.py find McpClient", "power": "script"},
    "callers": {"help": "who calls a symbol, with call-site lines", "eg": "symbols.py callers recall", "power": "script"},
    "lint": {"help": "docblock-coverage report (undocumented funcs, dead params)", "eg": "symbols.py lint", "power": "script"},
    "index": {"help": "upsert symbols into semantic memory (for recall)", "eg": "symbols.py index", "power": "script"},
    "map": {"help": "EXTENSIVE per-symbol page: file:line, signature, docblock, callers + callees", "eg": "symbols.py map", "power": "script"},
    "unresolved": {"help": "calls to symbols defined NOWHERE (typos, half-done renames, assumed APIs)", "eg": "symbols.py unresolved --write", "power": "script"},
    "dead": {"help": "unused symbols (dead-code candidates)", "eg": "symbols.py dead --all", "power": "script"},
    "deadparams": {"help": "declared-but-unused function parameters", "eg": "symbols.py deadparams", "power": "script"},
    "hotspots": {"help": "core components by PageRank (most depended-upon)", "eg": "symbols.py hotspots", "power": "script"},
    "mermaid": {"help": "print/emit the Mermaid code-map", "eg": "symbols.py mermaid --page", "power": "script"},
    "selftest": {"help": "regression guard for the index builder", "eg": "symbols.py selftest", "power": "script"},
    "body": {"help": "print a function/method/class's EXACT source span (CST-derived — no regex brace-matching)", "eg": "symbols.py body src/app.js renderCard", "power": "script"},
    "cut": {"help": "unified diff deleting a symbol's exact span (stdout patch; apply via git apply after review)", "eg": "symbols.py cut src/app.js oldHelper > /tmp/p.diff", "power": "script"},
}


def main():
    import cli_util
    cli_util.pre("symbols", COMMANDS)
    ap = argparse.ArgumentParser(prog="symbols",
        description="Deterministic code symbol index. For files on disk use `fnd`.")
    ap.add_argument("cmd", nargs="?", default="gen")
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("arg2", nargs="?", default="")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--scope", default=None)
    ap.add_argument("--lang", default=None)
    ap.add_argument("--strict", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--publish", action="store_true",
                    help="write the committed in-repo .claude/ discovery copy (CI/manual) "
                         "instead of the out-of-repo local cache")
    a = ap.parse_args()
    if a.publish:
        globals()["_PUBLISH"] = True
    root = repo_root(a.cwd) or (a.cwd or os.getcwd())
    scope = a.scope or current_scope(a.cwd)
    if a.cmd == "gen":
        cmd_gen(root, scope)
    elif a.cmd == "find":
        cmd_find(root, a.arg)
    elif a.cmd == "callers":
        cmd_callers(root, a.arg)
    elif a.cmd == "lint":
        sys.exit(cmd_lint(root, a.lang or a.arg, a.strict))
    elif a.cmd == "index":
        cmd_index(root, scope)
    elif a.cmd == "map":
        cmd_map(root, scope)
    elif a.cmd == "unresolved":
        cmd_unresolved(root, as_json=a.json, write=a.write, show_all=(a.arg == "--all"))
    elif a.cmd == "dead":
        cmd_dead(root, show_all=(a.arg == "--all"))
    elif a.cmd == "deadparams":
        cmd_deadparams(root, show_all=(a.arg in ("--all", "all")))
    elif a.cmd == "hotspots":
        idx = _load(root)
        if not idx:
            print("no symbol index — run: symbols.py gen"); return
        edges, _ = class_edges(idx)
        pr = pagerank(edges)
        print("Core components by PageRank (most depended-upon — change with care):")
        for c, s in sorted(pr.items(), key=lambda x: -x[1])[:20]:
            ins = sum(w for (a2, b2), w in edges.items() if b2 == c)
            print(f"  {s:.4f}  {c}  ({ins} incoming calls)")
    elif a.cmd == "body":
        sys.exit(cmd_body(root, a.arg, a.arg2))
    elif a.cmd == "cut":
        sys.exit(cmd_cut(root, a.arg, a.arg2))
    elif a.cmd == "selftest":
        sys.exit(cmd_selftest())
    elif a.cmd == "mermaid":
        idx = _load(root)
        if not idx:
            print("no symbol index — run: symbols.py gen"); return
        if a.arg == "--page":
            print(json.dumps({"ok": write_code_map(root, scope, idx)}))
        else:
            print(build_mermaid(idx)[0])
    else:
        print(f"unknown command: {a.cmd}")


if __name__ == "__main__":
    try:
        import tel as _t; _t.hit("symbols")   # telemetry (fail-open)
    except Exception:
        pass
    main()
