#!/usr/bin/env python3
"""cli_util — one consistent contract for every .claude-memory CLI, added with a
single line so we normalize behavior WITHOUT rewriting each script's argparse
(which would risk the live hooks that call them).

Adopt in a script's main() BEFORE argparse runs:

    import cli_util
    cli_util.pre("symbols", COMMANDS)   # COMMANDS: {name: {"help":..,"eg":..,"power":..}}

That one call gives the script:
  * `--registry`  → prints its command metadata as JSON (what `dev` reads).
  * unknown first subcommand → difflib "did you mean 'find'?" + "run `dev <prog>`"
    on stderr, exit 2 (instead of argparse's opaque error / silent mis-dispatch).
  * a uniform `dev`-discoverable surface, so the sprawl gets one map.

It deliberately does NOT touch the script's own argparse/flags — existing
invocations (and the hooks that depend on them) keep working byte-for-byte.

Convention for NEW code (not enforced on old): standard global flags are
`--cwd` / `--json` / `--scope`; `make_parser()` pre-adds them.
"""
import sys
import json
import difflib
import argparse

MANIFEST = {
    "role": "lib",
    "provides": "one consistent contract for every .claude-memory CLI, added with a single "
                 "line so we normalize behavior WITHOUT rewriting each script's argparse…",
}


STD_GLOBAL = ("--cwd", "--json", "--scope")


def pre(prog, commands):
    """Handle --registry and unknown-subcommand hinting. Call at top of main().
    `commands` = {name: {"help": str, "eg": str (example), "power": str}}."""
    argv = sys.argv[1:]
    if "--registry" in argv:
        print(json.dumps(registry(prog, commands)))
        raise SystemExit(0)
    # first non-flag token is the subcommand; if it's clearly wrong, help early.
    first = next((a for a in argv if not a.startswith("-")), None)
    if first is not None and commands and first not in commands:
        near = difflib.get_close_matches(first, list(commands), n=1)
        hint = f" Did you mean `{near[0]}`?" if near else ""
        names = sorted(commands)
        listed = ", ".join(names[:30]) + (f" … (+{len(names) - 30})" if len(names) > 30 else "")
        sys.stderr.write(
            f"{prog}: unknown command '{first}'.{hint}\n  commands: {listed}\n"
            f"  Run `dev {prog}` for usage (or `dev` for the full tool index).\n")
        raise SystemExit(2)


def registry(prog, commands):
    """Structured metadata for `dev` / the devbox skill."""
    return {
        "prog": prog,
        "commands": [
            {"name": n,
             "help": (m or {}).get("help", ""),
             "eg": (m or {}).get("eg", ""),
             "power": (m or {}).get("power", "script")}
            for n, m in commands.items()
        ],
    }


def make_parser(prog, desc):
    """argparse parser with the standard global flags pre-added (for new/migrated
    scripts). Returns (parser, subparsers)."""
    p = argparse.ArgumentParser(prog=prog, description=desc)
    p.add_argument("--cwd", default=None, help="target directory (repo scope)")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--scope", default=None, help="memory/wiki scope override")
    sub = p.add_subparsers(dest="cmd")
    return p, sub


def read_registry(script_path, timeout=6):
    """Best-effort: run `<script> --registry` and parse it. Used by dev.py to
    enrich the curated index with a script's real command list. Returns None on any
    failure (script not migrated / errored) so callers fall back gracefully."""
    import subprocess
    try:
        r = subprocess.run(["python3", script_path, "--registry"],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0 and r.stdout.strip().startswith("{"):
            return json.loads(r.stdout)
    except Exception:
        pass
    return None


def run_selftest(test_path, args=()):
    """Run a self-test file in a FRESH interpreter and exit with its code. Never in-process: a module that imported
    store/vault at load time is already bound to the real store, so the test's own redirect would come too late
    (t-ebe5db: `vault_migrate.py self-test` overwrote real secrets that way). PAWKIT_SELFTEST=1 makes store.py
    refuse the default database if the test forgot its redirect."""
    import os
    import subprocess
    env = dict(os.environ, PAWKIT_SELFTEST="1")
    raise SystemExit(subprocess.run([sys.executable, test_path, *args], env=env).returncode)
