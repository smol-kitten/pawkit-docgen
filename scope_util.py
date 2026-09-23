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
    return _run(["git", "-C", cwd, *args])


def current_scope(cwd=None):
    """Stable scope for the given dir: the git REMOTE repo name (so a clone named
    differently than its repo still resolves correctly, e.g. /home/claude/repo ->
    'catboyindustries-arg'). Falls back to the git toplevel basename, else 'global'."""
    cwd = os.path.abspath(cwd or os.getcwd())
    url = _git_out(cwd, "remote", "get-url", "origin")
    if url:
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
        if name:
            return name
    top = _git_out(cwd, "rev-parse", "--show-toplevel")
    if top:
        return os.path.basename(top)
    return "global"


def repo_root(cwd=None):
    """Git toplevel for the cwd (where an in-repo .claude/wiki lives), or None."""
    top = _git_out(os.path.abspath(cwd or os.getcwd()), "rev-parse", "--show-toplevel")
    return top or None
