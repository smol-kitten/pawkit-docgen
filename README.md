# pawkit-docgen

Deterministic code-index + wiki generators extracted from the **pawkit** agent toolkit.

> **GENERATED MIRROR — do not edit here.** The source of truth is the private `pawkit`
> repo; edits here are overwritten by `docgen_sync.py`. Open PRs against pawkit instead.

## What this is for
CI in other repos runs these generators to refresh their committed `.claude/**` discovery
copy (symbol index, repo-map, wiki) without a token into private pawkit. Only these
non-sensitive, deterministic files are published; everything sensitive is excluded.

## Files
- `symbols.py`
- `repomap.py`
- `wiki.py`
- `wiki_lib.py`
- `agentsmd.py`
- `scope_util.py`
- `cli_util.py`
- `php_symbols.php`

## Use in CI
Copy these files to `/home/claude/.claude-memory` (the scripts' expected home), then run
`SYMBOLS_PUBLISH=1 python3 symbols.py gen --publish --cwd <repo>` (+ repomap/wiki/agentsmd).
See the pawkit `deploy/ci-docs-publish.sh` wrapper.

## Requirements
`pip install -r requirements.txt` (tree-sitter grammar pack + pyyaml).
