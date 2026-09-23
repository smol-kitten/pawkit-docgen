"""
Wiki layer — curated, longer-form knowledge that complements the short-fact
semantic memory.

Storage is PORTABLE: a repo's wiki lives IN the repo at `<repo>/.claude/wiki/`
(so it travels + is shareable via git); `global` (cross-repo) knowledge lives
centrally at /home/claude/.claude-memory/wiki/global/.

Pages are markdown with YAML frontmatter:
  title, scope, tags[], summary (short, auto-hintable), related[] (slugs), web[] (URLs)
Each wiki dir keeps an INDEX.json (title/tags/summary per page) for fast hints
and listing. Greppable on disk; driven via the `wiki.py` CLI (cwd-aware) and the
auto-hint in the recall hook. Reads span the current repo's wiki + global.
"""
import os
import re
import glob
import json
import yaml

import sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
from scope_util import current_scope, repo_root

MANIFEST = {
    "role": "lib",
    "provides": "Wiki layer — curated, longer-form knowledge that complements the "
                 "short-fact semantic memory",
}


CENTRAL = "/home/claude/.claude-memory/wiki"   # holds the "global" scope


def _slug(s, allow_path=True):
    """Slugify. Path-aware: a slug may be a wiki-relative PATH like `auth/tokens`
    (topic/page); each segment is slugified independently and depth is capped at 2
    (topic/page). A flat slug (no `/`) slugifies exactly as before — back-compat."""
    s = (s or "").lower().strip()
    if allow_path and "/" in s:
        segs = [re.sub(r"[^a-z0-9-]+", "-", seg).strip("-")[:60] for seg in s.split("/")]
        segs = [x for x in segs if x]
        return "/".join(segs[:2])          # depth cap: topic/page
    return re.sub(r"[^a-z0-9-]+", "-", s).strip("-")[:60]


_H2_RE = re.compile(r"^## +(.+?)\s*$")


def sections_of(body):
    """Deterministic list of a page's H2 sections: [{anchor, title, chars}].
    `##` headings ARE the sections — so every existing page becomes section-
    addressable retroactively, with zero edits (the core natural-migration win)."""
    lines = (body or "").splitlines()
    idxs = [i for i, l in enumerate(lines) if _H2_RE.match(l)]
    out = []
    for j, i in enumerate(idxs):
        title = _H2_RE.match(lines[i]).group(1).strip()
        end = idxs[j + 1] if j + 1 < len(idxs) else len(lines)
        out.append({"anchor": _slug(title, allow_path=False),
                    "title": title, "chars": sum(len(l) for l in lines[i + 1:end])})
    return out


def _walk(base):
    """Yield (slug, path) for every managed .md page under `base`, RECURSIVELY.
    slug = base-relative path without `.md` (e.g. `auth/tokens`). Skips INDEX*,
    dotfiles, and dot-dirs (e.g. the capture `.inbox/`). This single helper replaces
    the old non-recursive `glob('*.md')` at every discovery site — which is what makes
    subfolder topics visible while leaving flat pages exactly where they were."""
    if not os.path.isdir(base):
        return
    for root, dirs, files in os.walk(base):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if not fn.endswith(".md") or fn.startswith("INDEX") or fn.startswith("."):
                continue
            p = os.path.join(root, fn)
            slug = os.path.relpath(p, base)[:-3].replace(os.sep, "/")
            yield slug, p


REBUILD_LOCK_MAX_S = 600


def _read_index(base):
    """INDEX.json as a list — WITHOUT ever rebuilding on the caller's path.

    Order: INDEX.json, then the last-known-good copy, then [] with a background rebuild queued.
    The old behaviour — any read error means `_index(base)` — rebuilt the whole index IN MEMORY on
    every prompt and never saved it, so one bad file (a merge-conflict marker, a half-written
    file from a concurrent writer) turned every prompt into a full wiki walk until a human
    happened to regenerate it. A wiki hint missing for one prompt is harmless. A 37x IO blow-up
    on every prompt is not."""
    idx = os.path.join(base, "INDEX.json")
    for path in (idx, idx + ".last"):
        try:
            with open(path) as fh:
                rows = json.load(fh)
            if path != idx:
                _schedule_rebuild(base, "index-unreadable-used-last-good")
            return rows
        except Exception:
            continue
    if os.path.isdir(base):
        _schedule_rebuild(base, "index-missing")
    return []


def _schedule_rebuild(base, why):
    """Queue ONE detached rebuild per base, deduplicated by a lock file, and record that the hot
    path hit a miss. A fallback that stays silent is how this bug hid; this one leaves a trace."""
    try:
        import subprocess, sys as _sys, time as _t
        lock = os.path.join(base, ".index-rebuild.lock")
        try:
            if _t.time() - os.path.getmtime(lock) < REBUILD_LOCK_MAX_S:
                return                               # a rebuild is already on its way
        except OSError:
            pass
        open(lock, "w").write(str(os.getpid()))
        try:
            import tel
            tel.emit("hook", "wiki-index-miss", ok=False, meta={"base": base, "why": why})
        except Exception:
            pass
        wiki = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wiki.py")
        subprocess.Popen([_sys.executable, wiki, "index"], cwd=os.path.dirname(os.path.abspath(__file__)),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def _load_index(base):
    """INDEX.json as a list. Tolerant of v1/v2 rows. Never rebuilds inline (see _read_index)."""
    return _read_index(base)


def _moved_map(base):
    """Rename ledger {old-slug: new-slug} for slug continuity after move/promote.
    Populated in Phase 4; a missing file is the normal Phase-1 case (returns {})."""
    try:
        return json.load(open(os.path.join(base, ".moved.json")))
    except Exception:
        return {}


def write_base(scope, cwd=None):
    """Dir to write a page of `scope` into, or None if a repo scope has no checkout."""
    if scope == "global":
        return os.path.join(CENTRAL, "global")
    root = repo_root(cwd)
    return os.path.join(root, ".claude", "wiki") if root else None


def read_bases(cwd=None):
    """(scope, dir) pairs to read/search: the current repo's wiki + global."""
    bases = []
    root = repo_root(cwd)
    if root:
        bases.append((current_scope(cwd), os.path.join(root, ".claude", "wiki")))
    bases.append(("global", os.path.join(CENTRAL, "global")))
    return bases


def parse(path):
    try:
        txt = open(path, encoding="utf-8", errors="replace").read()
    except Exception:
        return {}, ""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", txt, re.S)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception:
            fm = {}
        return fm, m.group(2).strip()
    return {}, txt.strip()


def _index(base):
    """(Re)build INDEX.json for a wiki root. Recursive (topics included). v2 rows add
    `path`, `parent` (derived from dirname, or explicit frontmatter `parent:`), `order`,
    and `sections` — all ADDITIVE; existing consumers use `.get()` so v1 readers are
    unaffected. Stays a JSON LIST (not a wrapper) for back-compat with every reader.
    Derived hierarchy lives HERE, never in page frontmatter, to keep git diffs minimal."""
    pages = []
    for slug, p in _walk(base):
        fm, body = parse(p)
        parent = os.path.dirname(slug) or (fm.get("parent") or "")
        pages.append({"slug": slug, "scope": fm.get("scope", ""), "title": fm.get("title", slug),
                      "tags": fm.get("tags", []), "summary": fm.get("summary", ""),
                      "related": fm.get("related", []), "web": fm.get("web", []),
                      "path": slug, "parent": parent, "order": fm.get("order", 0),
                      "sections": sections_of(body), "mtime": int(os.path.getmtime(p))})
    pages.sort(key=lambda x: x["slug"])          # stable order → minimal INDEX.json diffs
    _write_index(base, pages)
    return pages


def _write_index(base, pages):
    """ATOMIC write (temp file + os.replace) plus a last-known-good copy.
    The old `json.dump(pages, open(path, "w"))` truncated the file before writing it, so any hook
    that read it mid-write got an empty file, failed to parse it, and fell into a full rebuild. With
    many sessions running hooks, that race fired routinely (measured 2026-09-23: 24,958 syscalls and
    450 directory scans per prompt while the index was unreadable, against 680 and 33)."""
    try:
        os.makedirs(base, exist_ok=True)
        dst = os.path.join(base, "INDEX.json")
        tmp = f"{dst}.tmp.{os.getpid()}"
        with open(tmp, "w") as fh:
            json.dump(pages, fh, sort_keys=True, separators=(",", ":"))
        os.replace(tmp, dst)                     # readers see the old file or the new one, never half
        try:
            import shutil
            shutil.copyfile(dst, dst + ".last")  # the fallback the hot path reads if INDEX.json breaks
        except Exception:
            pass
    except Exception:
        pass


_FENCE_RE = re.compile(r"^```", re.M)


def _lint_body(body):
    """Cheap structural check (regex, no deps): an odd number of ``` fence markers
    means a code block never closes, which renders as broken/half-raw markdown.
    Self-heals by appending a closing fence; returns (body, warning_or_None)."""
    n = len(_FENCE_RE.findall(body))
    if n % 2 == 0:
        return body, None
    return body.rstrip() + "\n```\n", f"unbalanced code fence ({n} ``` markers found) — appended a closing fence"


def write_page(slug, title, body, cwd=None, scope=None, tags=None, summary="", related=None, web=None):
    explicit_scope = scope is not None
    scope = scope or current_scope(cwd)
    base = write_base(scope, cwd)
    if base is None:
        return {"ok": False, "error": f"scope '{scope}' needs a repo checkout; cd into it or use scope=global"}
    slug = _slug(slug or title)
    os.makedirs(base, exist_ok=True)
    body, warn = _lint_body((body or "").strip())
    if warn:
        # write_page is often called from a detached background generator (repomap,
        # symbols code-map) with stdout discarded, so also leave a one-shot advisory
        # for recall-hook.py to surface on the next prompt (same pattern as mermaid).
        msg = f"⚠ wiki page '{slug}' ({scope}): {warn}"
        print(msg)
        try:
            with open("/home/claude/.claude-memory/.wiki_lint_advice", "w") as fh:
                fh.write(msg)
        except Exception:
            pass
    fm = {"title": title or slug, "scope": scope, "tags": tags or [],
          "summary": (summary or "")[:280], "related": related or [], "web": web or []}
    page_path = os.path.join(base, f"{slug}.md")
    os.makedirs(os.path.dirname(page_path), exist_ok=True)   # topic subdir for `topic/page` slugs
    with open(page_path, "w", encoding="utf-8") as f:
        f.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                + "\n---\n\n" + body + "\n")
    _index(base)
    result = {"ok": True, "slug": slug, "scope": scope, "path": page_path}
    # Guardrail: a page that defaulted to global *because cwd is not a repo* is very
    # likely an accidental global write (project notes leaking into fleet knowledge).
    # Surface a one-line hint (only when scope was NOT explicitly requested).
    if not explicit_scope and scope == "global" and repo_root(cwd) is None:
        result["hint"] = ("wrote to GLOBAL (shared fleet knowledge) — cwd is not a git repo, "
                          "so scope defaulted to global. For a PROJECT-local wiki, run from inside "
                          "the project's git repo (git init + cd into it), or pass scope=<name> with "
                          "a checkout. Pass scope='global' explicitly to silence this hint.")
    return result


def get_page(slug, cwd=None):
    """Resolve a page by slug. Resolution order (the slug-continuity guarantee):
      1. exact path match (`auth/tokens.md`) — flat pages always hit here, unchanged;
      2. unique BASENAME match anywhere in the tree, so `get tokens` still finds
         `auth/tokens` after a promote/move, and old flat `related:` slugs keep working;
         an ambiguous basename returns {"ambiguous": [slugs]} rather than guessing;
      3. the `.moved.json` rename ledger (Phase 4).
    Returns the page dict, an {"ambiguous": [...]} marker, or None."""
    slug = _slug(slug)
    # 1. exact path
    for _, base in read_bases(cwd):
        p = os.path.join(base, f"{slug}.md")
        if os.path.exists(p):
            fm, body = parse(p)
            return {"slug": slug, "path": p, **fm, "body": body}
    # 2. unique basename across the read trees (only reached when exact misses → cheap)
    leaf = slug.rsplit("/", 1)[-1]
    matches = []
    for _, base in read_bases(cwd):
        for s, p in _walk(base):
            if s.rsplit("/", 1)[-1] == leaf:
                matches.append((s, p))
    if len(matches) == 1:
        s, p = matches[0]
        fm, body = parse(p)
        return {"slug": s, "path": p, **fm, "body": body}
    if len(matches) > 1:
        return {"ambiguous": sorted(s for s, _ in matches), "slug": slug}
    # 3. moved ledger
    for _, base in read_bases(cwd):
        mv = _moved_map(base)
        if slug in mv and mv[slug] != slug:
            return get_page(mv[slug], cwd)
    return None


def rm_page(slug, cwd=None):
    """Delete a wiki page and keep the indexes consistent. Resolves the slug (exact path or
    unique basename), removes the .md, then rebuilds INDEX.json + INDEX.md for that root so no
    stale entry lingers (the failure mode that left orphaned `hinttest-tmp` rows). Returns
    {"ok", "slug", "scope"} or {"ok": False, "reason"/"ambiguous"}. Caller handles the recall
    memory hint (tag `wiki.<slug>`)."""
    pg = get_page(slug, cwd)
    if not pg:
        return {"ok": False, "reason": "not found", "slug": slug}
    if "ambiguous" in pg:
        return {"ok": False, "ambiguous": pg["ambiguous"], "slug": slug}
    path = pg.get("path")
    real_slug = pg.get("slug", slug)
    scope = pg.get("scope") or ""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as e:
        return {"ok": False, "reason": f"unlink failed: {e}", "slug": real_slug}
    base = os.path.dirname(path)
    # climb to the wiki root (INDEX.json lives at the root, pages may be in topic subdirs)
    for _, b in read_bases(cwd):
        if path.startswith(b):
            base = b
            break
    try:
        _index(base)
    except Exception:
        pass
    try:
        synopsis(scope=scope or None, cwd=cwd)
    except Exception:
        pass
    return {"ok": True, "slug": real_slug, "scope": scope}


def get_section(slug, anchor, cwd=None):
    """Return just one H2 section of a page: {slug, anchor, title, text, nav}.
    Lets an agent read ~30 lines instead of a whole page (context-lean)."""
    pg = get_page(slug, cwd)
    if not pg or "body" not in pg:
        return None
    lines = pg["body"].splitlines()
    h2 = [i for i, l in enumerate(lines) if _H2_RE.match(l)]
    for j, i in enumerate(h2):
        title = _H2_RE.match(lines[i]).group(1).strip()
        if _slug(title, allow_path=False) == anchor:
            end = h2[j + 1] if j + 1 < len(h2) else len(lines)
            return {"slug": pg["slug"], "anchor": anchor, "title": title,
                    "text": "\n".join(lines[i:end]).strip(), "nav": nav_of(pg["slug"], cwd)}
    return None


def nav_of(slug, cwd=None):
    """Sibling/child/parent navigation for a slug, derived from INDEX.json.
    Returns {parent, siblings:[{slug,summary}], children:[{slug,summary}]}."""
    slug = _slug(slug)
    parent = os.path.dirname(slug)
    idx = []
    for _, base in read_bases(cwd):
        idx += _load_index(base)
    seen, sib, ch = set(), [], []
    for it in idx:
        s = it.get("slug", "")
        if s in seen or s == slug:
            continue
        seen.add(s)
        d = os.path.dirname(s)
        if d == parent:
            sib.append({"slug": s, "summary": it.get("summary", "")})
        elif d == slug:
            ch.append({"slug": s, "summary": it.get("summary", "")})
    sib.sort(key=lambda x: x["slug"])
    ch.sort(key=lambda x: x["slug"])
    return {"parent": parent or None, "siblings": sib, "children": ch}


def list_pages(cwd=None):
    out = []
    for scope, base in read_bases(cwd):
        for slug, p in _walk(base):
            fm, _ = parse(p)
            out.append({"slug": slug,
                        "scope": fm.get("scope", scope), "title": fm.get("title", ""),
                        "tags": fm.get("tags", []), "summary": fm.get("summary", "")})
    return out


def search(query, cwd=None, limit=8):
    terms = [t.lower() for t in re.split(r"\W+", query) if len(t) > 2]
    if not terms:
        return []
    res = []
    for scope, base in read_bases(cwd):
        for slug, p in _walk(base):
            fm, body = parse(p)
            head = (fm.get("title", "") + " " + " ".join(map(str, fm.get("tags", []))) + " " + fm.get("summary", "")).lower()
            hay = head + " " + body.lower()
            score = sum(3 * head.count(t) + hay.count(t) for t in terms)
            if score:
                res.append({"slug": slug, "scope": fm.get("scope", scope),
                            "title": fm.get("title", ""), "summary": fm.get("summary", ""), "score": score})
    return sorted(res, key=lambda x: -x["score"])[:limit]


def index_hints(cwd=None):
    """Compact {slug,scope,title,tags,summary,age_days} for auto-hinting, repo + global.
    age_days (page file mtime) lets callers flag possibly-stale pages (freshness stamp)."""
    import time as _time
    out = []
    for scope, base in read_bases(cwd):
        items = _read_index(base)                    # never rebuilds on the prompt path
        now = _time.time()
        for it in items:
            # the index stores each page's mtime at build time; the old code did exists() and
            # getmtime() on every page on every prompt (~360 stat calls, half a warm prompt's IO)
            if it.get("mtime"):
                it["age_days"] = int((now - it["mtime"]) / 86400)
            out.append(it)
    return out


def _pages_in(base):
    pages = []
    for slug, p in _walk(base):
        fm, body = parse(p)
        if not fm.get("title"):
            continue  # not a managed page (no frontmatter)
        pages.append({"slug": slug, "path": p, "fm": fm, "body": body, "title": fm["title"]})
    return pages


def _write_fm_body(path, fm, body):
    with open(path, "w", encoding="utf-8") as f:
        f.write("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True).strip()
                + "\n---\n\n" + (body or "").strip() + "\n")


DIGEST_CAP = 2000     # global digests stay compact — a hint layer, not a doc site


def make_digest(slug, cwd=None):
    """Write/refresh a compact GLOBAL digest of a repo-scoped page: summary + section
    titles + a source pointer. The global tier stays FLAT and capped (~2KB) — a cross-repo
    hint layer, never auto-absorbing raw findings. Returns {ok, slug, source}."""
    pg = get_page(slug, cwd)
    if not pg or "body" not in pg:
        return {"ok": False, "error": f"no page '{slug}'"}
    src_scope = pg.get("scope") or current_scope(cwd)
    if src_scope == "global":
        return {"ok": False, "error": "page is already global"}
    secs = sections_of(pg.get("body", ""))
    leaf = slug.rsplit("/", 1)[-1]
    dslug = _slug(f"{src_scope}-{leaf}", allow_path=False)     # flat global slug
    lines = [pg.get("summary") or "", ""]
    if secs:
        lines.append("**Sections:** " + ", ".join(s["title"] for s in secs[:12]))
        lines.append("")
    lines.append(f"_Full detail: in the `{src_scope}` repo → `wiki.py get {slug}`._")
    body = "\n".join(lines).strip()[:DIGEST_CAP]
    base = os.path.join(CENTRAL, "global")
    os.makedirs(base, exist_ok=True)
    fm = {"title": f"{pg.get('title') or leaf} ({src_scope})", "scope": "global",
          "tags": list(pg.get("tags") or []) + ["digest"],
          "summary": (pg.get("summary") or "")[:280], "related": [], "web": [],
          "source": f"{src_scope}/{slug}"}
    _write_fm_body(os.path.join(base, f"{dslug}.md"), fm, body)
    _index(base)
    return {"ok": True, "slug": dslug, "scope": "global", "source": f"{src_scope}/{slug}"}


def refresh_digests(cwd=None):
    """Rebuild every global digest whose `source:` points at the CURRENT repo scope (so a
    bg-refresh in a repo keeps its promoted digests current). Deterministic. Returns count."""
    scope = current_scope(cwd)
    n = 0
    for _, p in _walk(os.path.join(CENTRAL, "global")):
        fm, _ = parse(p)
        src = fm.get("source") or ""
        if "/" in src and src.split("/", 1)[0] == scope:
            if make_digest(src.split("/", 1)[1], cwd).get("ok"):
                n += 1
    return {"ok": True, "refreshed": n}


def stats(cwd=None):
    """Adoption metrics for the natural-migration rollout (deterministic, no AI): per scope
    — pages, topics, %-in-topics, pages with sections, inbox throughput (absorbed/pending),
    digests, and moved-ledger size. This is how we measure adoption WITHOUT forcing it."""
    out = {"scopes": {}}
    for scope, base in read_bases(cwd):
        pages = list(_walk(base))
        total = len(pages)
        topics, intopic, withsec, digests = set(), 0, 0, 0
        for slug, p in pages:
            d = os.path.dirname(slug)
            if d:
                topics.add(d.split("/")[0])
                intopic += 1
            fm, body = parse(p)
            if sections_of(body):
                withsec += 1
            if fm.get("source"):
                digests += 1
        absorbed = 0
        try:
            absorbed = sum(1 for _ in open(os.path.join(base, ".inbox", "ledger.jsonl")))
        except Exception:
            pass
        pending = len(glob.glob(os.path.join(base, ".inbox", "*.md")))
        out["scopes"][scope] = {
            "pages": total, "topics": len(topics),
            "pct_in_topics": round(100 * intopic / total) if total else 0,
            "pages_with_sections": withsec, "digests": digests,
            "inbox_absorbed": absorbed, "inbox_pending": pending,
            "moved_slugs": len(_moved_map(base))}
    return out


def _base_of(path, cwd=None):
    """Which read-base (wiki root) a page path lives under."""
    for _, b in read_bases(cwd):
        if path.startswith(os.path.abspath(b) + os.sep) or path.startswith(b + os.sep):
            return b
    return None


def _record_move(base, old, new):
    """Append to the base's .moved.json rename ledger (old→new), so get_page keeps
    resolving the old slug. Also collapses chains (a→b, b→c ⇒ a→c)."""
    mvf = os.path.join(base, ".moved.json")
    mv = _moved_map(base)
    for k, v in list(mv.items()):
        if v == old:
            mv[k] = new
    mv[old] = new
    try:
        json.dump(mv, open(mvf, "w"), sort_keys=True, indent=0)
    except Exception:
        pass


def _repair_related(base, old, new):
    n = 0
    for _, p in _walk(base):
        fm, body = parse(p)
        rel = fm.get("related") or []
        if old in rel:
            fm["related"] = [new if r == old else r for r in rel]
            _write_fm_body(p, fm, body)
            n += 1
    return n


def move_page(old, new, cwd=None):
    """Move/rename a page within its wiki base. git-mv-aware (falls back to os.replace),
    records the rename in .moved.json (old slug keeps resolving), and repairs `related:`
    references in-scope. Returns {ok, from, to, related_repaired, git}."""
    import subprocess
    old, new = _slug(old), _slug(new)
    if old == new:
        return {"ok": False, "error": "old == new"}
    pg = get_page(old, cwd)
    if not pg or "path" not in pg:
        return {"ok": False, "error": f"no page '{old}'"}
    if pg.get("ambiguous"):
        return {"ok": False, "error": "ambiguous", "matches": pg["ambiguous"]}
    src = pg["path"]
    base = _base_of(src, cwd) or os.path.dirname(src)
    dst = os.path.join(base, f"{new}.md")
    if os.path.exists(dst):
        return {"ok": False, "error": f"target '{new}' already exists"}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    git_ok = False
    repo = os.path.dirname(os.path.dirname(base))   # <repo>/.claude/wiki → <repo>
    try:
        if os.path.isdir(os.path.join(repo, ".git")):
            r = subprocess.run(["git", "-C", repo, "mv", src, dst],
                               capture_output=True, timeout=15)
            git_ok = r.returncode == 0
    except Exception:
        git_ok = False
    if not git_ok:
        os.replace(src, dst)
    _record_move(base, old, new)
    reps = _repair_related(base, old, new)
    _index(base)
    return {"ok": True, "from": old, "to": new, "related_repaired": reps, "git": git_ok}


def promote_page(slug, cwd=None):
    """Turn a flat page into a TOPIC by moving `slug.md` → `slug/index.md`, so children
    can hang off it. Idempotent-ish: refuses if the page is already a topic index."""
    slug = _slug(slug)
    if slug.endswith("/index") or "/" in slug:
        return {"ok": False, "error": "already a topic path"}
    return move_page(slug, f"{slug}/index", cwd=cwd)


def split_page(slug, cwd=None, dry_run=True):
    """Split a large flat page into a topic: each H2 section becomes a child page
    `slug/<anchor>.md`, and the original becomes `slug/index.md` (intro + a child TOC).
    Deterministic, no AI. Dry-run by default — returns the plan without touching disk."""
    slug = _slug(slug)
    pg = get_page(slug, cwd)
    if not pg or "body" not in pg:
        return {"ok": False, "error": f"no page '{slug}'"}
    if "/" in slug:
        return {"ok": False, "error": "already inside a topic"}
    body = pg.get("body", "")
    lines = body.splitlines()
    h2 = [i for i, l in enumerate(lines) if _H2_RE.match(l)]
    if len(h2) < 2:
        return {"ok": False, "error": "needs ≥2 H2 sections to split"}
    intro = "\n".join(lines[:h2[0]]).strip()
    children = []
    for j, i in enumerate(h2):
        title = _H2_RE.match(lines[i]).group(1).strip()
        anchor = _slug(title, allow_path=False)
        end = h2[j + 1] if j + 1 < len(h2) else len(lines)
        children.append({"anchor": anchor, "title": title,
                         "text": "\n".join(lines[i:end]).strip()})
    plan = {"ok": True, "slug": slug, "dry_run": dry_run, "intro_chars": len(intro),
            "children": [{"slug": f"{slug}/{c['anchor']}", "title": c["title"]} for c in children]}
    if dry_run:
        return plan
    scope = pg.get("scope")
    tags = pg.get("tags") or []
    # 1. write each child page
    for c in children:
        write_page(f"{slug}/{c['anchor']}", c["title"], c["text"], cwd=cwd, scope=scope,
                   tags=tags, summary=c["text"].split("\n", 2)[-1][:180])
    # 2. rewrite the original into the topic index (intro + TOC), then move it under the topic
    toc = "\n".join(f"- [{c['title']}]({slug}/{c['anchor']}.md)" for c in children)
    idx_body = (intro + "\n\n" if intro else "") + "## Pages\n" + toc
    write_page(slug, pg.get("title") or slug, idx_body, cwd=cwd, scope=scope, tags=tags,
               summary=pg.get("summary") or "")
    move_page(slug, f"{slug}/index", cwd=cwd)
    return plan


def crosslink(cwd=None):
    """Deterministic, AI-free cross-linker: for every page, find mentions of OTHER
    pages' titles in its body and record them in the page's `related` frontmatter
    (dedup, never self). Keeps the wiki interconnected without spending any tokens.
    Returns {pages_updated, links_added}."""
    added = touched = 0
    active = current_scope(cwd)
    for scope, base in read_bases(cwd):
        if scope == "global" and active != "global":
            continue  # only auto-link within the active repo's own wiki
        pages = _pages_in(base)
        if len(pages) < 2:
            continue
        title_map = sorted(((p["title"], p["slug"]) for p in pages),
                           key=lambda t: -len(t[0]))   # longest title first
        for p in pages:
            body_l = p["body"].lower()
            p_topic = os.path.dirname(p["slug"])
            rel = list(p["fm"].get("related") or [])
            before = set(rel)
            for title, slug in title_map:
                if slug == p["slug"] or slug in rel or len(title) < 4:
                    continue
                # only link within the same topic, or to/from a root-level page —
                # keeps the graph meaningful and bounded as topics multiply.
                t_topic = os.path.dirname(slug)
                if t_topic and p_topic and t_topic != p_topic:
                    continue
                if re.search(r"\b" + re.escape(title.lower()) + r"\b", body_l):
                    rel.append(slug)
            if set(rel) != before:
                p["fm"]["related"] = rel
                _write_fm_body(p["path"], p["fm"], p["body"])
                added += len(set(rel) - before)
                touched += 1
        _index(base)
    return {"pages_updated": touched, "links_added": added}


def brief(cwd=None, max_chars=3500):
    """A compact, hand-to-a-subagent repo DIGEST assembled from what's already
    documented — so exploration can read the wiki instead of crawling the repo.

    Pulls (in priority order, capped): the repo-map's stack/layout summary, the
    code-map's core components, then every OTHER page's one-line summary. Returns
    a markdown string. Deterministic, no AI, no repo walk — just the wiki.
    """
    scope = current_scope(cwd)
    lines = [f"# Repo brief — {scope}", ""]
    lines.append("_Assembled from the wiki (repo-map + code-map + page summaries). "
                 "Read this before broad exploration; open a named page for detail._")
    lines.append("")

    def _page_section(slug, header, keep_headings=None):
        pg = get_page(slug, cwd)
        if not pg:
            return
        lines.append(f"## {header}")
        if pg.get("summary"):
            lines.append(pg["summary"])
        body = pg.get("body", "")
        if keep_headings:
            # keep only the requested subsections (e.g. code-map's core list) to stay lean
            block, grab = [], False
            for ln in body.splitlines():
                if ln.startswith("## "):
                    grab = any(h.lower() in ln.lower() for h in keep_headings)
                elif grab:
                    block.append(ln)
            kept = "\n".join(block).strip()
            if kept:
                lines.append(kept)
        lines.append("")

    _page_section("repo-map", "Stack & layout", keep_headings=["Stack", "Layout", "Entry"])
    _page_section("code-map", "Core components", keep_headings=["Core components"])

    # every other page: title + summary (pointer, not payload). Topic children are
    # grouped and indented under their topic so a subagent can descend one topic
    # instead of opening every page; flat/root pages list as before.
    roots, topics = [], {}
    for pg in list_pages(cwd):
        slug = pg["slug"]
        if slug in ("repo-map", "code-map", "INDEX"):
            continue
        summ = (pg.get("summary") or "").strip()
        label = f"**{pg.get('title') or slug}** (`{slug}`)" + (f" — {summ}" if summ else "")
        topic = os.path.dirname(slug)
        if topic:
            topics.setdefault(topic, []).append((slug, label))
        else:
            roots.append(f"- {label}")
    if roots or topics:
        lines.append("## Other documented topics (open by slug for detail)")
        lines.extend(sorted(set(roots)))
        for topic in sorted(topics):
            lines.append(f"- 📁 **{topic}/**")
            for _, label in sorted(topics[topic]):
                lines.append(f"  - {label}")
        lines.append("")

    out = "\n".join(lines).rstrip()
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n\n_(brief truncated — open individual pages for more)_"
    return out


def synopsis(cwd=None, scope=None):
    """Write a top-level INDEX.md for the active repo's wiki: a one-line summary plus
    per-section links to each page. Pages stay separate files, so deep reads remain
    on-demand while the index keeps context short. Sections group by each page's first
    tag. Deterministic, no AI. Returns {ok, path, pages, sections}."""
    scope = scope or current_scope(cwd)
    base = write_base(scope, cwd)
    if base is None or not os.path.isdir(base):
        return {"ok": False, "error": "no repo wiki base"}
    pages = _pages_in(base)
    if not pages:
        return {"ok": False, "error": "no pages"}
    # A page in a topic folder (`topic/page`) groups under that TOPIC; a root/flat page
    # groups by its first tag (unchanged behavior). Topic sections sort first.
    sections = {}
    for p in pages:
        topic = os.path.dirname(p["slug"])
        if topic:
            key = f"📁 {topic}"
        else:
            tags = p["fm"].get("tags") or []
            key = str(tags[0]) if tags else "misc"
        sections.setdefault(key, []).append(p)
    topic_keys = sorted(k for k in sections if k.startswith("📁 "))
    tag_keys = sorted(k for k in sections if not k.startswith("📁 "))
    lines = [f"# {scope} wiki", "",
             f"_{len(pages)} page(s) in {len(sections)} section(s). Top-level map — "
             "open a page for detail._", ""]
    for sec in topic_keys + tag_keys:
        lines.append(f"## {sec}")
        for p in sorted(sections[sec], key=lambda x: x["title"].lower()):
            summ = (p["fm"].get("summary") or "").strip()
            lines.append(f"- [{p['title']}]({p['slug']}.md)" + (f" — {summ}" if summ else ""))
        lines.append("")
    out = os.path.join(base, "INDEX.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    return {"ok": True, "path": out, "pages": len(pages), "sections": len(sections)}


def patchnotes(cwd=None, scope=None):
    """Append commits since the last recorded one to a deterministic 'patch-notes'
    wiki page (pure `git log`, no AI). Idempotent via a hidden last-hash marker.
    Returns {ok, added, head}."""
    import subprocess
    scope = scope or current_scope(cwd)
    base = write_base(scope, cwd)
    if base is None or not os.path.isdir(os.path.dirname(os.path.dirname(base))):
        return {"ok": False, "error": "no repo wiki base"}
    root = os.path.dirname(os.path.dirname(base))  # <repo>/.claude/wiki -> <repo>

    def git(*a):
        return subprocess.run(["git", "-C", root, *a], capture_output=True, text=True).stdout.strip()

    head = git("rev-parse", "HEAD")
    if not head:
        return {"ok": False, "error": "not a git repo"}
    page = get_page("patch-notes", cwd) or {}
    body = page.get("body", "") or ""
    m = re.search(r"<!--last:([0-9a-f]{7,40})-->", body)
    last = m.group(1) if m else ""
    if last == head[:len(last)] and last:
        return {"ok": True, "added": 0, "head": head[:8]}  # up to date
    rng = [f"{last}..HEAD"] if last else ["-30"]
    raw = git("log", "--no-merges", "--date=short", "--pretty=%h\t%ad\t%s", *rng)
    new = [ln for ln in raw.splitlines() if ln.strip()]
    if not new and last:
        return {"ok": True, "added": 0, "head": head[:8]}
    # Group new commits under a dated heading, newest first; prepend above prior notes.
    from time import strftime, gmtime
    block = [f"### {strftime('%Y-%m-%d', gmtime())}"]
    for ln in new:
        parts = ln.split("\t", 2)
        if len(parts) == 3:
            h, d, s = parts
            block.append(f"- `{h}` {s}")
    block.append("")
    # Strip the old marker line from the existing body, then rebuild.
    prior = re.sub(r"\n?<!--last:[0-9a-f]{7,40}-->\s*$", "", body).strip()
    new_body = "\n".join(block) + ("\n" + prior if prior else "") + f"\n\n<!--last:{head}-->"
    write_page("patch-notes", page.get("title") or (scope + " patch notes"),
               new_body, cwd=cwd, scope=scope,
               tags=page.get("tags") or ["changelog"],
               summary=page.get("summary") or f"Auto-generated commit log for {scope} (deterministic).")
    return {"ok": True, "added": len(new), "head": head[:8]}
