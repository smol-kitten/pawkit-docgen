#!/usr/bin/env python3
"""repomap — deterministic, dependency-free repo cartographer.

Generates a structural map of a repo WITHOUT any AI (git + stdlib only): stack,
layout, entry points, manifests, and cheap route/test signals. Writes it as a
wiki page (slug `repo-map`) so it's auto-hinted + semantically searchable and
travels in-repo. This is the script-based auto-documentation that fixes "the wiki
only fills when explicitly asked" — run it on a hook/schedule per repo.

Usage:  repomap.py [gen] [--cwd <path>] [--scope <name>] [--print]
"""
import os
import sys
import re
import json
import argparse
import subprocess
import time
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from scope_util import current_scope, repo_root

MANIFEST = {
    "role": "cli",
    "provides": "deterministic, dependency-free repo cartographer",
}


LANG = {
    ".php": "PHP", ".py": "Python", ".js": "JavaScript", ".ts": "TypeScript",
    ".tsx": "TypeScript/React", ".jsx": "JavaScript/React", ".go": "Go", ".rs": "Rust",
    ".rb": "Ruby", ".java": "Java", ".kt": "Kotlin", ".cs": "C#", ".c": "C", ".cpp": "C++",
    ".sh": "Shell", ".css": "CSS", ".scss": "SCSS", ".html": "HTML", ".sql": "SQL",
    ".vue": "Vue", ".swift": "Swift", ".ex": "Elixir", ".dart": "Dart",
}
MANIFESTS = {
    "composer.json": "PHP/Composer", "package.json": "Node", "pyproject.toml": "Python",
    "requirements.txt": "Python", "go.mod": "Go", "Cargo.toml": "Rust", "Gemfile": "Ruby",
    "pom.xml": "Java/Maven", "build.gradle": "Gradle", "Dockerfile": "Docker",
    "docker-compose.yml": "Docker Compose", "docker-compose.yaml": "Docker Compose",
    ".csproj": "C#", "CMakeLists.txt": "CMake",
}
ENTRY_HINTS = ("index.php", "main.go", "main.py", "main.rs", "app.py", "manage.py",
               "server.js", "server.ts", "index.js", "index.ts", "wsgi.py", "asgi.py")
ROUTE_PATTERNS = [
    (r"->(get|post|put|delete|patch|map)\s*\(", "PHP/Slim routes"),
    (r"@(app|router|blueprint)\.(route|get|post)", "Python routes"),
    (r"(app|router)\.(get|post|put|delete|patch)\s*\(", "JS/Express routes"),
    (r"func\s+\w+\(.*http\.(ResponseWriter|Request)", "Go handlers"),
]


def _git(root, *args):
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


# dependency/build dirs excluded from the map (same spirit as .claudeignore)
IGNORE_DIRS = ("vendor/", "node_modules/", ".venv/", "venv/", "dist/", "build/",
               "target/", ".next/", "__pycache__/", ".git/", "bower_components/",
               "vendor.bundle/", "third_party/")


def build(root):
    files = [f for f in _git(root, "ls-files").splitlines()
             if f and not any(f.startswith(d) or ("/" + d) in f for d in IGNORE_DIRS)]
    if not files:
        return None
    exts = Counter(os.path.splitext(f)[1].lower() for f in files)
    langs = Counter()
    for e, n in exts.items():
        if e in LANG:
            langs[LANG[e]] += n
    manifests = sorted({MANIFESTS[os.path.basename(f)] for f in files
                        if os.path.basename(f) in MANIFESTS}
                       | {MANIFESTS[k] for f in files for k in MANIFESTS if f.endswith(k) and k.startswith(".")})
    topdirs = Counter(f.split("/")[0] if "/" in f else "(root)" for f in files)
    entries = sorted({f for f in files if os.path.basename(f) in ENTRY_HINTS
                      or f.startswith(("cmd/", "bin/"))})[:12]
    tests = [f for f in files if re.search(r"(^|/)(tests?|spec)/|_test\.|\.test\.|test_", f)]
    # cheap route signal: scan a bounded sample of source files
    route_hits = Counter()
    scanned = 0
    for f in files:
        if scanned >= 400:
            break
        if os.path.splitext(f)[1].lower() not in LANG:
            continue
        p = os.path.join(root, f)
        try:
            txt = open(p, encoding="utf-8", errors="replace").read(40000)
        except Exception:
            continue
        scanned += 1
        for pat, label in ROUTE_PATTERNS:
            c = len(re.findall(pat, txt))
            if c:
                route_hits[label] += c
    head = _git(root, "rev-parse", "--short", "HEAD").strip()
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return {
        "files": len(files), "langs": langs, "manifests": manifests, "topdirs": topdirs,
        "entries": entries, "tests": len(tests), "routes": route_hits,
        "head": head, "branch": branch,
    }


def render(scope, d, stamp):
    top = ", ".join(f"{l} ({n})" for l, n in d["langs"].most_common(6)) or "—"
    summary = f"Repo map: {scope} — {top}; {d['files']} tracked files" + (
        f"; {', '.join(d['manifests'][:4])}" if d["manifests"] else "")
    lines = [summary, "",
             f"_Auto-generated (deterministic, no AI) {stamp} · {d['branch']}@{d['head']}._", "",
             "## Stack",
             "- Languages: " + (", ".join(f"{l} ({n})" for l, n in d["langs"].most_common(10)) or "—"),
             "- Manifests/build: " + (", ".join(d["manifests"]) or "—"), "",
             "## Layout (top-level)",
             *[f"- `{name}/` — {n} files" for name, n in d["topdirs"].most_common(15)], "",
             "## Entry points",
             *([f"- `{e}`" for e in d["entries"]] or ["- (none detected)"]), "",
             "## Signals",
             f"- Test files: {d['tests']}",
             *[f"- {label}: {n}" for label, n in d["routes"].most_common()], ]
    return summary, "\n".join(lines)


def selftest():
    """Regression guard for render(), no repo needed: feed a synthetic build() dict
    and assert it produces a non-empty, structurally-sane map (balanced ``` fences,
    required sections). Run in CI/dev to catch generator breakage before it ships."""
    d = {"files": 42, "langs": Counter({"Go": 30, "Python": 12}),
         "manifests": ["Go", "Docker"], "topdirs": Counter({"cmd": 5, "internal": 20}),
         "entries": ["cmd/main.go"], "tests": 8, "routes": Counter({"Go handlers": 4}),
         "head": "abc1234", "branch": "main"}
    summary, body = render("selftest", d, "2026-01-01")
    problems = []
    if body.count("```") % 2 != 0:
        problems.append("unbalanced code fence in body")
    for need in ("## Stack", "## Layout", "## Entry points"):
        if need not in body:
            problems.append(f"missing section {need!r}")
    if not summary or "selftest" not in summary:
        problems.append("summary missing/empty")
    if problems:
        print("FAIL — repomap render():")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"OK — repomap render() self-test passed ({len(body)} chars, all sections present)")
    return 0


COMMANDS = {
    "gen":      {"help": "(re)build the deterministic repo-map wiki page for a repo.",
                 "eg": "repomap.py gen --cwd /path/to/repo", "power": "script"},
    "selftest": {"help": "regression-guard the renderer (no repo needed).",
                 "eg": "repomap.py selftest", "power": "script"},
}


def main():
    import cli_util
    cli_util.pre("repomap", COMMANDS)
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="gen")
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--scope", default=None)
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()
    if a.cmd == "selftest":
        sys.exit(selftest())
    root = repo_root(a.cwd) or (a.cwd or os.getcwd())
    scope = a.scope or current_scope(a.cwd)
    d = build(root)
    if not d:
        print(json.dumps({"ok": False, "error": "no git-tracked files"})); sys.exit(1)
    stamp = subprocess.run(["date", "-u", "+%Y-%m-%d"], capture_output=True, text=True).stdout.strip()
    summary, body = render(scope, d, stamp)

    # OPTIONAL (capability-tiered): when a local model is up, add a short prose
    # overview GROUNDED on the deterministic facts (no model → structural map only).
    try:
        import llm_util
        if llm_util.available():
            readme = ""
            for rn in ("README.md", "readme.md", "README"):
                rp = os.path.join(root, rn)
                if os.path.exists(rp):
                    readme = open(rp, encoding="utf-8", errors="replace").read(1500)
                    break
            prose = llm_util.call(
                "Write a 2-4 sentence plain-English overview of this repository, GROUNDED ONLY "
                "in the facts below (do not invent, no preamble, no headings).\n\nFACTS:\n"
                + body[:1500] + (("\n\nREADME head:\n" + readme) if readme else ""),
                allow_claude=False).strip()
            if prose and len(prose) > 40 and "## Stack" in body:
                body = body.replace("## Stack", "## Overview\n" + prose + "\n\n## Stack", 1)
    except Exception:
        pass

    if a.print:
        print(body); return
    import wiki_lib
    r = wiki_lib.write_page("repo-map", f"{scope} repo map", body, cwd=root, scope=scope,
                            tags=["repo-map", "structure", "auto"], summary=summary[:200])
    print(json.dumps({"ok": bool(r.get("ok")), "scope": scope, "path": r.get("path"),
                      "files": d["files"], "langs": dict(d["langs"].most_common(6))}))
    try:
        import obs
        obs.record("repomap-gen", summary=f"{scope}: {d['files']} files, "
                   + ", ".join(f"{l}({n})" for l, n in d["langs"].most_common(4)),
                   ok=bool(r.get("ok")))
    except Exception:
        pass


if __name__ == "__main__":
    main()
