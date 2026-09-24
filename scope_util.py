"""Resolve the current memory scope: git-repo basename of the cwd, else 'global'."""
import functools
import os, re, subprocess

MANIFEST = {
    "role": "lib",
    "provides": "Resolve the current memory scope: git-repo basename of the cwd, else 'global'",
}



def _run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


# A repo's root and remote do not change during one process's life, yet both helpers spawned
# `git` on every call: ONE prompt-hook run launched git 20 times for the same repo (12 rev-parse,
# 8 remote; measured 2026-09-23). A per-process cache keeps exactly one of each. (An earlier try
# measured "no effect" in bg-refresh — true there, because those calls come from separate
# processes, each with its own cache. The prompt hook is one long process.)
@functools.lru_cache(maxsize=256)
def _git_out(cwd, *args):
    try:
        import pawkit_idx
        git = pawkit_idx.bin("git")
    except Exception:
        git = "git"
    return _run([git, "-C", cwd, *args])


@functools.lru_cache(maxsize=256)
def _git_facts(cwd):
    """(toplevel, origin_url) read from the .git files directly — no process. pawkit 2.0 phase 2:
    the prompt hook still spawned git 3-4 times per prompt for the SESSION's repo (rev-parse
    --show-toplevel, remote get-url), which a pawkit-only index cannot cache. Handles a plain
    repo, a worktree or submodule (a .git FILE: 'gitdir: <path>', config in its commondir).
    Returns None when the layout is anything else, so the caller falls back to git itself."""
    d = cwd
    while True:
        dot = os.path.join(d, ".git")
        if os.path.isdir(dot):
            common = dot
            break
        if os.path.isfile(dot):
            try:
                line = open(dot).read().strip()
            except OSError:
                return None
            if not line.startswith("gitdir:"):
                return None
            gd = line[7:].strip()
            gd = gd if os.path.isabs(gd) else os.path.normpath(os.path.join(d, gd))
            common = gd
            try:
                cd = open(os.path.join(gd, "commondir")).read().strip()
                common = cd if os.path.isabs(cd) else os.path.normpath(os.path.join(gd, cd))
            except OSError:
                pass
            break
        parent = os.path.dirname(d)
        if parent == d:
            return ("", "")                          # not in a repo: same answer git gives
        d = parent
    try:
        cfg = open(os.path.join(common, "config")).read()
    except OSError:
        return None
    if re.search(r"(?mi)^\s*\[(include|includeif)\b|^\s*\[url\b", cfg):
        return None                                  # includes / insteadOf: let git resolve it
    url, section = "", None
    for raw in cfg.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line
            continue
        if section and re.fullmatch(r'\[remote\s+"origin"\]', section) and "=" in line:
            k, v = line.split("=", 1)
            if k.strip().lower() == "url":
                url = v.strip()
                break
    return (d, url)


def current_scope(cwd=None):
    """Stable scope for the given dir: the git REMOTE repo name (so a clone named
    differently than its repo still resolves correctly, e.g. /home/claude/repo ->
    'catboyindustries-arg'). Falls back to the git toplevel basename, else 'global'."""
    cwd = os.path.abspath(cwd or os.getcwd())
    facts = _git_facts(cwd)
    url = facts[1] if facts is not None else _git_out(cwd, "remote", "get-url", "origin")
    if url:
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
        if name:
            return name
    top = facts[0] if facts is not None else _git_out(cwd, "rev-parse", "--show-toplevel")
    if top:
        return os.path.basename(top)
    return "global"


def origin_url(cwd=None):
    """`git remote get-url origin` for the cwd's repo, from the .git files when possible; '' if none."""
    cwd = os.path.abspath(cwd or os.getcwd())
    facts = _git_facts(cwd)
    return facts[1] if facts is not None else _git_out(cwd, "remote", "get-url", "origin")


def repo_root(cwd=None):
    """Git toplevel for the cwd (where an in-repo .claude/wiki lives), or None."""
    cwd = os.path.abspath(cwd or os.getcwd())
    facts = _git_facts(cwd)
    top = facts[0] if facts is not None else _git_out(cwd, "rev-parse", "--show-toplevel")
    return top or None
