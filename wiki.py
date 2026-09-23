#!/usr/bin/env python3
"""wiki: curated per-repo + global knowledge pages (portable; repo pages live in
<repo>/.claude/wiki and travel with the repo).

Usage:
  wiki.py write <slug> --title "T" [--scope X] [--tags a,b] [--summary "..."] \
                 [--related x,y] [--web url1,url2]      # body read from stdin
  wiki.py get <slug>
  wiki.py search "<query>"
  wiki.py list
  wiki.py index                                          # rebuild INDEX.json(s)

Scope is auto-detected from the cwd's git remote (e.g. catboyindustries-arg);
omit --scope for the current repo, or pass --scope global for cross-repo docs.
"""
import os, sys, json, argparse, subprocess
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import wiki_lib as W
from scope_util import current_scope

MANIFEST = {
    "role": "cli",
    "provides": "wiki: curated per-repo + global knowledge pages (portable; repo pages live "
                 "in <repo>/.claude/wiki and travel with the repo)",
}



def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _mem():
    subprocess.run(["/home/claude/.claude-memory/ensure-server.sh"], timeout=25, capture_output=True)
    from mcp_client import McpHttpClient, memory_client
    return memory_client(timeout=8)


def _index_page(slug, scope, title, summary, body):
    """Index a page into the Qdrant memory (tag wiki.<slug>) for semantic search/recall."""
    try:
        content = f"[wiki:{slug}] {title}\n{summary}\n{(body or '')[:1400]}"
        _mem().call_tool("remember", {"content": content, "tag": f"wiki.{slug}", "scope": scope or "global"})
    except Exception:
        pass


COMMANDS = {
    "write": {"help": "create/overwrite a wiki page from stdin body", "eg": "echo '...' | wiki.py write my-slug --title 'My Page'", "power": "script"},
    "get": {"help": "print a wiki page (title, tags, summary, body)", "eg": "wiki.py get context-governor", "power": "script"},
    "search": {"help": "find pages by semantic + keyword match", "eg": "wiki.py search 'deploy pipeline'", "power": "script"},
    "list": {"help": "list all wiki pages with scope/tags", "eg": "wiki.py list", "power": "script"},
    "index": {"help": "(re)emit page summary hints for recall", "eg": "wiki.py index", "power": "script"},
    "draft": {"help": "AI-draft a page from the repo's own files", "eg": "wiki.py draft overview", "power": "ai"},
    "reindex": {"help": "re-upsert every page into semantic memory", "eg": "wiki.py reindex", "power": "script"},
    "crosslink": {"help": "compute/refresh related-page cross links", "eg": "wiki.py crosslink", "power": "script"},
    "synopsis": {"help": "print a synopsis of the repo's wiki", "eg": "wiki.py synopsis", "power": "script"},
    "patchnotes": {"help": "summarize recent wiki changes", "eg": "wiki.py patchnotes", "power": "script"},
    "brief": {"help": "compact wiki brief hinted into context", "eg": "wiki.py brief", "power": "script"},
    "tree": {"help": "topic-indented map of all pages (hierarchy view)", "eg": "wiki.py tree", "power": "script"},
    "nav": {"help": "parent/siblings/children of a page", "eg": "wiki.py nav auth/tokens", "power": "script"},
    "sections": {"help": "list a page's H2 sections (get slug#anchor for one)", "eg": "wiki.py sections effort-control", "power": "script"},
    "move": {"help": "move/rename a page (git-mv-aware; keeps old slug resolving via .moved.json)", "eg": "wiki.py move old-slug auth/tokens", "power": "script"},
    "rm": {"help": "delete a page + rebuild indexes + drop its recall hint (no orphan rows)", "eg": "wiki.py rm hinttest-tmp", "power": "script"},
    "promote": {"help": "turn a flat page into a topic (slug.md → slug/index.md)", "eg": "wiki.py promote auth", "power": "script"},
    "split": {"help": "split a big page's H2 sections into child pages (dry-run default; --apply to write)", "eg": "wiki.py split big-page", "power": "script"},
    "stats": {"help": "wiki adoption metrics (pages, topics, %-in-topics, inbox throughput, digests)", "eg": "wiki.py stats", "power": "script"},
}


def main():
    import cli_util
    cli_util.pre("wiki", COMMANDS)
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("cmd", choices=["write", "get", "search", "list", "index", "draft", "reindex",
                                    "crosslink", "synopsis", "patchnotes", "brief",
                                    "tree", "nav", "sections", "move", "rm", "promote", "split", "stats"])
    ap.add_argument("arg", nargs="?", default="")
    ap.add_argument("arg2", nargs="?", default="")
    ap.add_argument("--apply", action="store_true", help="split: actually write (default dry-run)")
    ap.add_argument("--to-global", action="store_true", dest="to_global",
                    help="promote: write a compact global digest instead of flat→topic")
    ap.add_argument("--title", default="")
    ap.add_argument("--scope", default=None)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--tags", default="")
    ap.add_argument("--summary", default="")
    ap.add_argument("--related", default="")
    ap.add_argument("--web", default="")
    ap.add_argument("--focus", default="the architecture, layout, and how to run/deploy it")
    ap.add_argument("--model", default="sonnet")
    a = ap.parse_args()

    if a.cmd == "write":
        # Body comes from STDIN. `arg2` is NOT the body — passing content there is silently
        # ignored, and the write then reports ok:true for an empty page. That happened: a
        # long page was "written" successfully and landed as front-matter and nothing else,
        # which is only noticeable if you go and count the lines afterwards. Both failure
        # shapes are now loud, because a tool that reports success for a no-op teaches you
        # to stop checking.
        if a.arg2:
            sys.exit("wiki write: the body is read from STDIN, not an argument — got an "
                     "extra positional that would be IGNORED.\n  wiki.py write <slug> "
                     "[--title ...] < page.md\n  ... | wiki.py write <slug>")
        body = sys.stdin.read() if not sys.stdin.isatty() else ""
        if not body.strip():
            sys.exit("wiki write: empty body on STDIN — refusing to write a page with only "
                     "front-matter. Redirect a file or pipe content in.")
        r = W.write_page(a.arg, a.title or a.arg, body, cwd=a.cwd, scope=a.scope,
                         tags=_csv(a.tags), summary=a.summary,
                         related=_csv(a.related), web=_csv(a.web))
        if r.get("ok"):
            _index_page(r["slug"], r["scope"], a.title or a.arg, a.summary, body)
        print(json.dumps(r))
    elif a.cmd == "get":
        # `get slug#anchor` returns just that section (context-lean read)
        ref, _, anchor = a.arg.partition("#")
        if anchor:
            sec = W.get_section(ref, W._slug(anchor, allow_path=False), cwd=a.cwd)
            if not sec:
                print(f"no section '#{anchor}' in '{ref}'"); sys.exit(1)
            print(sec["text"])
            nav = sec.get("nav") or {}
            foot = []
            if nav.get("parent"): foot.append(f"↑ {nav['parent']}/index")
            sibs = [s["slug"].rsplit("/", 1)[-1] for s in nav.get("siblings", [])]
            if sibs: foot.append("siblings: " + ", ".join(sibs))
            if foot: print("\n_" + " · ".join(foot) + "_")
            sys.exit(0)
        p = W.get_page(a.arg, cwd=a.cwd)
        if not p:
            print(f"no page '{a.arg}'"); sys.exit(1)
        if p.get("ambiguous"):
            print(f"'{a.arg}' is ambiguous — matches: " + ", ".join(p["ambiguous"]))
            print("re-run with the full path slug."); sys.exit(2)
        print(f"# {p.get('title')}  [{p.get('scope')}/{p['slug']}]")
        if p.get("tags"): print("tags:", ", ".join(map(str, p["tags"])))
        if p.get("summary"): print("summary:", p["summary"])
        if p.get("related"): print("related:", ", ".join(map(str, p["related"])))
        if p.get("web"): print("web:", ", ".join(map(str, p["web"])))
        sec_list = W.sections_of(p.get("body", ""))
        if len(sec_list) > 1:
            print("sections:", ", ".join("#" + s["anchor"] for s in sec_list))
        print("\n" + p.get("body", ""))
    elif a.cmd == "search":
        seen, rows = set(), []
        try:  # semantic: query the Qdrant memory for wiki.* entries
            hits = json.loads(_mem().call_tool("recall", {"query": a.arg, "scope": current_scope(), "limit": 8}).get("text") or "[]")
            for h in hits:
                t = h.get("tag", "")
                if t.startswith("wiki.") and t[5:] not in seen:
                    seen.add(t[5:]); rows.append((h.get("scope", ""), t[5:], "semantic"))
        except Exception:
            pass
        for h in W.search(a.arg):  # keyword/grep fallback + extras
            if h["slug"] not in seen:
                seen.add(h["slug"]); rows.append((h["scope"], h["slug"], "grep"))
        if not rows:
            print("no matches")
        for scope, slug, how in rows[:8]:
            pg = W.get_page(slug) or {}
            print(f"  [{scope}/{slug}] ({how}) {pg.get('summary','') or pg.get('title', slug)}")
    elif a.cmd == "reindex":
        n = 0
        for pg in W.list_pages():
            full = W.get_page(pg["slug"]) or {}
            _index_page(pg["slug"], pg.get("scope", "global"), full.get("title", ""), full.get("summary", ""), full.get("body", ""))
            n += 1
        print(f"reindexed {n} pages into semantic memory")
    elif a.cmd == "draft":
        # Bootstrap a wiki page from the repo's own files via claude -p (organic docs).
        import subprocess
        from scope_util import repo_root
        root = repo_root() or os.getcwd()
        ctx = []
        tree = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout.splitlines()
        ctx.append("FILE LIST:\n" + "\n".join(tree[:250]))
        # A4: ground the draft on the deterministic repo-map page when it exists —
        # far richer signal than a bare file list (stack, layout, entry points).
        try:
            rm = W.get_page("repo-map", cwd=root)
            if rm and rm.get("body"):
                ctx.append("=== repo-map (deterministic facts) ===\n" + rm["body"][:2500])
        except Exception:
            pass
        for fn in ["README.md", "ARCHITECTURE.md", "CLAUDE.md", "composer.json", "package.json",
                   "pyproject.toml", "docker-compose.yml", "docker-compose.yaml", "Dockerfile"]:
            p = os.path.join(root, fn)
            if os.path.exists(p):
                ctx.append(f"=== {fn} ===\n" + open(p, encoding="utf-8", errors="replace").read()[:1800])
        prompt = ("You are a documentation GENERATOR. Output ONLY the markdown document text — "
                  "no preamble, no sign-off, and never mention saving, files, permissions, or tools "
                  "(the caller saves your output). Document " + a.focus + " of this repository based "
                  "ONLY on the provided files (do not invent). Start DIRECTLY with a single "
                  "one-sentence summary line (no heading), then short sections like Overview, Layout, "
                  "Key components, Run/Deploy. Keep it tight.\n\nREPO CONTEXT:\n" + "\n\n".join(ctx)[:14000])
        # A4: LOCAL-FIRST draft — try the free local/remote model and accept it only
        # if it passes quality gates (substantial, sectioned, balanced fences);
        # escalate to claude -p (cloud tokens) only on failure. The stop-hook's
        # auto-draft path becomes cloud-cost-free in the common case.
        body = ""
        try:
            import llm_util
            if llm_util.available():
                cand = (llm_util.call(prompt, heavy=True, allow_claude=False,
                                      timeout=240, cache=False) or "").strip()
                # strip a wrapping ```markdown fence (leading/trailing independently)
                import re as _re2
                _cl = cand.splitlines()
                if _cl and _re2.match(r"^```(?:markdown|md)?\s*$", _cl[0]):
                    _cl = _cl[1:]
                if _cl and _cl[-1].strip() == "```":
                    _cl = _cl[:-1]
                cand = "\n".join(_cl).strip()
                if (len(cand) >= 400 and cand.count("## ") >= 2
                        and cand.count("```") % 2 == 0
                        and not cand.lower().startswith(("i cannot", "i can't", "sorry"))):
                    body = cand
        except Exception:
            pass
        if not body:
            try:
                body = subprocess.run(["claude", "-p", "--model", a.model, "--allowed-tools", ""],
                                      input=prompt, capture_output=True, text=True, timeout=240).stdout.strip()
            except Exception as e:
                print(f"draft failed: {e}"); sys.exit(1)
        if not body:
            print("draft produced no output"); sys.exit(1)
        # strip any leading meta/preamble the headless agent may add before the real content
        import re as _re
        lines = body.splitlines()
        while lines and (not lines[0].strip() or lines[0].strip() == "---" or
                         _re.match(r"^(I |I'|Here'?s|Sorry|Note:|Unfortunately|The wiki|I cannot|I can'?t)", lines[0].strip())):
            lines.pop(0)
        body = "\n".join(lines).strip()
        first = next((l.strip().lstrip("# ").strip() for l in body.splitlines() if l.strip()), "")
        r = W.write_page(a.arg or "overview", a.title or (a.arg or "Overview").replace("-", " ").title(),
                         body, scope=a.scope, tags=_csv(a.tags) or ["overview", "auto-draft"],
                         summary=a.summary or first[:200])
        if r.get("ok"):
            _index_page(r["slug"], r["scope"], a.title or a.arg, a.summary or first[:200], body)
        print(json.dumps(r) + "\n(review/edit the draft, then commit it with the repo)")
    elif a.cmd == "crosslink":
        print(json.dumps(W.crosslink(cwd=a.cwd)))
    elif a.cmd == "synopsis":
        print(json.dumps(W.synopsis(cwd=a.cwd, scope=a.scope)))
    elif a.cmd == "patchnotes":
        print(json.dumps(W.patchnotes(cwd=a.cwd, scope=a.scope)))
    elif a.cmd == "brief":
        print(W.brief(cwd=a.cwd))
    elif a.cmd == "tree":
        # topic-indented map: roots first, then each topic with its children
        pages = sorted(W.list_pages(cwd=a.cwd), key=lambda x: x["slug"])
        roots = [p for p in pages if "/" not in p["slug"]]
        topics = {}
        for p in pages:
            if "/" in p["slug"]:
                topics.setdefault(p["slug"].split("/", 1)[0], []).append(p)
        for p in roots:
            print(f"{p['slug']}  — {(p.get('summary') or p.get('title') or '')[:80]}")
        for topic in sorted(topics):
            print(f"{topic}/")
            for p in topics[topic]:
                leaf = p["slug"].split("/", 1)[1]
                print(f"  {leaf}  — {(p.get('summary') or p.get('title') or '')[:76]}")
    elif a.cmd == "nav":
        nav = W.nav_of(a.arg, cwd=a.cwd)
        print(f"parent: {nav.get('parent') or '(root)'}")
        for kind in ("siblings", "children"):
            items = nav.get(kind) or []
            print(f"{kind} ({len(items)}):")
            for it in items:
                print(f"  - {it['slug']}" + (f" — {it['summary'][:70]}" if it.get("summary") else ""))
    elif a.cmd == "sections":
        p = W.get_page(a.arg)
        if not p or "body" not in p:
            print(f"no page '{a.arg}'"); sys.exit(1)
        secs = W.sections_of(p.get("body", ""))
        if not secs:
            print("(no H2 sections)")
        for s in secs:
            print(f"  #{s['anchor']}  ({s['chars']}c)  {s['title']}")
    elif a.cmd == "move":
        print(json.dumps(W.move_page(a.arg, a.arg2, cwd=a.cwd)))
    elif a.cmd == "promote":
        if a.to_global:
            print(json.dumps(W.make_digest(a.arg, cwd=a.cwd)))
        else:
            print(json.dumps(W.promote_page(a.arg, cwd=a.cwd)))
    elif a.cmd == "split":
        print(json.dumps(W.split_page(a.arg, cwd=a.cwd, dry_run=not a.apply), indent=1))
    elif a.cmd == "stats":
        print(json.dumps(W.stats(cwd=a.cwd), indent=1))
    elif a.cmd == "rm":
        if not a.arg:
            print(json.dumps({"ok": False, "reason": "usage: wiki.py rm <slug>"})); return
        res = W.rm_page(a.arg, cwd=a.cwd)
        if res.get("ok"):
            try:  # drop the page's recall memory hint so it stops surfacing
                _mem().call_tool("forget", {"tag": f"wiki.{res['slug']}",
                                            "scope": res.get("scope") or "global"})
            except Exception:
                pass
        print(json.dumps(res))
    elif a.cmd == "index":
        print(f"indexed {len(W.index_hints())} pages")
    else:  # list
        for h in W.list_pages(cwd=a.cwd):
            print(f"  [{h['scope']}/{h['slug']}] {h['title']}  ({', '.join(map(str, h['tags']))})")


if __name__ == "__main__":
    try:
        import tel as _t; _t.hit("wiki")   # telemetry (fail-open)
    except Exception:
        pass
    main()
