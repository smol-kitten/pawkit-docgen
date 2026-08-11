#!/usr/bin/env python3
"""agentsmd.py — deterministic AGENTS.md generator (no AI).

Emits/refreshes a thin, cross-agent-standard AGENTS.md at a repo root:
what the repo is, how to build/test/run it, and POINTERS to the existing
.claude/ discovery artifacts (repo-map, code-map, symbols, wiki) rather than
re-documenting them. Also drops a one-line CLAUDE.md pointer when absent.

Fleet convention (see `dev agentsmd`):
  - AGENTS.md is the canonical entry file for ANY agent (Claude, Codex,
    Cursor, Copilot, Gemini, ...). CLAUDE.md is a pointer to it.
  - Content between the custom markers is PRESERVED across regenerations;
    everything else is regenerated from deterministic sources.
  - This tool only WRITES files. It never commits — per the m-144 rule,
    generated docs are left uncommitted on the default branch and are to be
    committed from a feature branch -> PR.

Usage:
  agentsmd.py gen [--cwd DIR]     generate/refresh AGENTS.md (+ CLAUDE.md pointer)
  agentsmd.py check [--cwd DIR]   exit 1 if AGENTS.md missing or stale (no write)
  agentsmd.py print [--cwd DIR]   print generated body to stdout, write nothing
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

CUSTOM_START = "<!-- agentsmd:custom:start -->"
CUSTOM_END = "<!-- agentsmd:custom:end -->"
GEN_MARK = "<!-- agentsmd:generated -->"


def _run(cmd, cwd):
    try:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                              timeout=10).stdout.strip()
    except Exception:
        return ""


def git_info(root):
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], root) or "?"
    commit = _run(["git", "rev-parse", "--short", "HEAD"], root) or "?"
    return branch, commit


def read_repo_map(root):
    """Pull description/stack/layout/entrypoints from the deterministic repo-map."""
    p = os.path.join(root, ".claude", "wiki", "repo-map.md")
    out = {"overview": "", "stack": "", "layout": [], "entrypoints": []}
    if not os.path.isfile(p):
        return out
    txt = open(p, encoding="utf-8", errors="replace").read()
    # strip frontmatter
    if txt.startswith("---"):
        txt = txt.split("---", 2)[-1]
    sec = {}
    cur = None
    for line in txt.splitlines():
        m = re.match(r"^##\s+(.*)", line)
        if m:
            cur = m.group(1).strip().lower()
            sec[cur] = []
            continue
        if cur is not None:
            sec[cur].append(line)

    def joined(name):
        return "\n".join(sec.get(name, [])).strip()

    out["overview"] = joined("overview")
    out["stack"] = joined("stack")
    for line in sec.get("layout (top-level)", []):
        line = line.strip()
        if line.startswith("- "):
            out["layout"].append(line[2:])
    for line in sec.get("entry points", []):
        line = line.strip()
        if line.startswith("- ") and "none detected" not in line.lower():
            out["entrypoints"].append(line[2:])
    return out


def readme_desc(root):
    """First substantive prose paragraph of the README (skip title/badges/quotes)."""
    p = os.path.join(root, "README.md")
    if not os.path.isfile(p):
        return ""
    para = []
    for line in open(p, encoding="utf-8", errors="replace"):
        s = line.strip()
        if not s:
            if para:
                break
            continue
        if s.startswith("#") or s.startswith(">") or s.startswith("[!") \
           or s.startswith("![") or s.startswith("<"):
            continue
        # drop inline badge-only lines
        if re.fullmatch(r"(\[!\[.*?\]\(.*?\)\]\(.*?\)\s*)+", s):
            continue
        para.append(s)
    return " ".join(para).strip()


def detect_commands(root):
    """Deterministically infer build/test/run commands from manifest files."""
    def has(pat):
        try:
            return any(f.endswith(pat) or f == pat for f in os.listdir(root))
        except OSError:
            return False

    def glob(ext):
        try:
            return [f for f in os.listdir(root) if f.endswith(ext)]
        except OSError:
            return []

    cmds = []  # (label, command)
    # .NET
    slns = glob(".sln")
    csproj = glob(".csproj")
    if slns or csproj:
        target = slns[0] if slns else ""
        cmds.append(("Build", f"dotnet build {target}".strip()))
        cmds.append(("Test", "dotnet test"))
    # Node
    if has("package.json"):
        pm = "pnpm" if os.path.isfile(os.path.join(root, "pnpm-lock.yaml")) else \
             "yarn" if os.path.isfile(os.path.join(root, "yarn.lock")) else "npm"
        try:
            pkg = json.load(open(os.path.join(root, "package.json")))
            scripts = pkg.get("scripts", {})
        except Exception:
            scripts = {}
        run = "run " if pm != "yarn" else ""
        cmds.append(("Install", f"{pm} install"))
        if "build" in scripts:
            cmds.append(("Build", f"{pm} {run}build"))
        if "test" in scripts:
            cmds.append(("Test", f"{pm} {run}test"))
        if "dev" in scripts:
            cmds.append(("Run", f"{pm} {run}dev"))
        elif "start" in scripts:
            cmds.append(("Run", f"{pm} start"))
    # Go
    if has("go.mod"):
        cmds.append(("Build", "go build ./..."))
        cmds.append(("Test", "go test ./..."))
    # Rust
    if has("Cargo.toml"):
        cmds.append(("Build", "cargo build"))
        cmds.append(("Test", "cargo test"))
    # PHP
    if has("composer.json"):
        cmds.append(("Install", "composer install"))
        if os.path.isfile(os.path.join(root, "phpunit.xml")) or \
           os.path.isfile(os.path.join(root, "phpunit.xml.dist")):
            cmds.append(("Test", "vendor/bin/phpunit"))
    # Python
    if has("pyproject.toml") or has("requirements.txt") or has("setup.py"):
        if has("requirements.txt"):
            cmds.append(("Install", "pip install -r requirements.txt"))
        elif has("pyproject.toml"):
            cmds.append(("Install", "pip install -e ."))
        cmds.append(("Test", "pytest"))
    # de-dup by label keeping first
    seen, out = set(), []
    for label, c in cmds:
        if label in seen:
            continue
        seen.add(label)
        out.append((label, c))
    return out


def pointers(root):
    """List of (path, note) for discovery artifacts that actually exist."""
    out = []
    checks = [
        (".claude/wiki/repo-map.md", "auto repo map: stack, layout, entry points"),
        (".claude/wiki/code-map.md", "who-calls-whom Mermaid graph"),
        (".claude/wiki/symbol-map.md", "every symbol with signature + callers"),
        (".claude/symbols.json", "machine symbol index (`symbols` skill / symbols.py)"),
        (".claude/wiki/INDEX.md", "curated wiki page index"),
        ("docs/", "human design docs"),
    ]
    for rel, note in checks:
        p = os.path.join(root, rel)
        if os.path.exists(p):
            out.append((rel, note))
    return out


def build_body(root):
    name = os.path.basename(os.path.abspath(root))
    branch, commit = git_info(root)
    rm = read_repo_map(root)
    desc = readme_desc(root) or rm["overview"] or "(no description found)"
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    L = []
    L.append(f"# AGENTS.md — {name}")
    L.append("")
    L.append(GEN_MARK)
    L.append(f"> Auto-generated by `agentsmd.py` (deterministic, no AI) · "
             f"{stamp} · {branch}@{commit}.")
    L.append("> This is the canonical entry file for **any** coding agent. "
             "Edit only inside the custom block below — the rest is regenerated.")
    L.append("")
    L.append("## What this is")
    L.append("")
    L.append(desc)
    L.append("")
    if rm["stack"]:
        L.append("## Stack")
        L.append("")
        L.append(rm["stack"])
        L.append("")

    cmds = detect_commands(root)
    if cmds:
        L.append("## Build / test / run")
        L.append("")
        L.append("```bash")
        for label, c in cmds:
            L.append(f"{c:<40}# {label}")
        L.append("```")
        L.append("")

    if rm["entrypoints"]:
        L.append("## Entry points")
        L.append("")
        for e in rm["entrypoints"]:
            L.append(f"- {e}")
        L.append("")

    ptrs = pointers(root)
    if ptrs:
        L.append("## Where to look (machine discovery)")
        L.append("")
        L.append("These are kept fresh automatically — read them before grepping:")
        L.append("")
        for rel, note in ptrs:
            L.append(f"- `{rel}` — {note}")
        L.append("")

    L.append("## Contributing conventions")
    L.append("")
    L.append("- Branch → PR → merge. Never commit directly to the default branch, "
             "never force-push, never `--admin`-merge.")
    L.append("- Keep this file's generated sections in sync by rerunning "
             "`agentsmd.py gen`; put durable hand-written notes in the custom block.")
    L.append("")
    L.append(CUSTOM_START)
    L.append("<!-- Hand-written notes below are preserved across regeneration. -->")
    L.append("")
    L.append(CUSTOM_END)
    L.append("")
    return "\n".join(L)


def extract_custom(existing):
    if not existing:
        return None
    m = re.search(re.escape(CUSTOM_START) + r"(.*)" + re.escape(CUSTOM_END),
                  existing, re.DOTALL)
    return m.group(1) if m else None


def merge_custom(new_body, existing):
    """Preserve the user's custom block from an existing file."""
    old = extract_custom(existing)
    if old is None:
        return new_body
    return re.sub(re.escape(CUSTOM_START) + r".*" + re.escape(CUSTOM_END),
                  CUSTOM_START + old + CUSTOM_END, new_body, flags=re.DOTALL)


def claude_pointer(root):
    """Write CLAUDE.md pointer only if absent or previously generated by us."""
    p = os.path.join(root, "CLAUDE.md")
    ptr = ("# CLAUDE.md\n\n" + GEN_MARK + "\n"
           "This repo's agent guidance lives in [AGENTS.md](./AGENTS.md) "
           "(the cross-agent standard). See it for build/test/run commands and "
           "discovery pointers.\n")
    if os.path.isfile(p):
        cur = open(p, encoding="utf-8", errors="replace").read()
        if GEN_MARK not in cur:
            return False  # hand-written CLAUDE.md — leave it alone
    open(p, "w", encoding="utf-8").write(ptr)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["gen", "check", "print"])
    ap.add_argument("--cwd", default=".")
    args = ap.parse_args()
    root = os.path.abspath(args.cwd)
    if not os.path.exists(os.path.join(root, ".git")):  # dir (normal) or file (worktree)
        print(f"agentsmd: {root} is not a git repo root", file=sys.stderr)
        return 2

    body = build_body(root)
    path = os.path.join(root, "AGENTS.md")
    existing = open(path, encoding="utf-8", errors="replace").read() \
        if os.path.isfile(path) else ""

    if args.cmd == "print":
        print(body)
        return 0

    merged = merge_custom(body, existing)

    if args.cmd == "check":
        # stale if missing or the non-custom body differs
        if not existing:
            print("AGENTS.md missing")
            return 1
        a = re.sub(re.escape(CUSTOM_START) + r".*" + re.escape(CUSTOM_END), "",
                   merged, flags=re.DOTALL).strip()
        b = re.sub(re.escape(CUSTOM_START) + r".*" + re.escape(CUSTOM_END), "",
                   existing, flags=re.DOTALL).strip()
        # ignore the stamp line which changes every commit
        strip_stamp = lambda s: re.sub(r"^> Auto-generated.*$", "", s,
                                       flags=re.MULTILINE)
        if strip_stamp(a) != strip_stamp(b):
            print("AGENTS.md stale")
            return 1
        print("AGENTS.md up to date")
        return 0

    open(path, "w", encoding="utf-8").write(merged)
    wrote_claude = claude_pointer(root)
    print(f"wrote {path}"
          + (" + CLAUDE.md pointer" if wrote_claude else ""))
    print("(uncommitted — commit from a feature branch, per m-144)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
