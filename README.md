# TS-DAF Slice 1

This repository is the production home for TS-DAF Slice 1. The active work plan is [WI-0001: Weak Article Router v1](docs/work/WI-0001-weak-article-router-v1.md).

The preserved experimental delivery is under [`tests/legacy_harness/`](tests/legacy_harness/).
The [CLI guide](docs/guides/CLI.md) documents the fixed `package validate`, `route run`,
`route resume`, `run inspect`, and `docs check` commands.

Run the legacy suite explicitly from PowerShell:

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests/legacy_harness').Path
python -m pytest -c tests/legacy_harness/pyproject.toml tests/legacy_harness/tests
Remove-Item Env:PYTHONPATH
```
