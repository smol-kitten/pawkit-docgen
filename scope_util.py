"""Resolve the current memory scope: git-repo basename of the cwd, else 'global'."""
import os, re, subprocess


def _run(args):
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=3)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def current_scope(cwd=None):
    """Stable scope for the given dir: the git REMOTE repo name (so a clone named
    differently than its repo still resolves correctly, e.g. /home/claude/repo ->
    'catboyindustries-arg'). Falls back to the git toplevel basename, else 'global'."""
    cwd = cwd or os.getcwd()
    url = _run(["git", "-C", cwd, "remote", "get-url", "origin"])
    if url:
        name = re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1])
        if name:
            return name
    top = _run(["git", "-C", cwd, "rev-parse", "--show-toplevel"])
    if top:
        return os.path.basename(top)
    return "global"


def repo_root(cwd=None):
    """Git toplevel for the cwd (where an in-repo .claude/wiki lives), or None."""
    top = _run(["git", "-C", cwd or os.getcwd(), "rev-parse", "--show-toplevel"])
    return top or None
