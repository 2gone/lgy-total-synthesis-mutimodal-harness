# TS-DAF Harness

This repository is the production home for TS-DAF. It currently contains the Task1–12 weak article
Router foundation and image processing v1 (`MolYOLO -> crop -> Gemini OCSR`). Start with the
[Chinese collaborator code guide](docs/guides/CODEBASE_GUIDE_ZH.md), then read the current
[work item](docs/work/WI-0001-weak-article-router-v1.md) for the detailed decision history.

The preserved experimental delivery is under [`tests/legacy_harness/`](tests/legacy_harness/).
It is an isolated historical test project, not a production dependency. The [CLI guide](docs/guides/CLI.md)
documents `package validate`, `route run`, `route resume`, `image run`, `run inspect`, and
`docs check`.

Run the active offline suite:

```powershell
python -m pytest -q
python -m tsdaf docs check
```

Run the legacy suite explicitly from PowerShell:

```powershell
$env:PYTHONPATH = (Resolve-Path 'tests/legacy_harness').Path
python -m pytest -c tests/legacy_harness/pyproject.toml tests/legacy_harness/tests
Remove-Item Env:PYTHONPATH
```
