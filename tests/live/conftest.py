"""Explicit safety gate for paid five-paper live routing tests."""

from __future__ import annotations

from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("live article-routing gold")
    group.addoption(
        "--run-live-gold",
        action="store_true",
        default=False,
        help="explicitly enable paid five-paper live gold validation",
    )
    group.addoption(
        "--live-gold-workdir",
        metavar="ABSOLUTE_PATH",
        help="persistent absolute work directory outside the repository",
    )


def pytest_configure(config: pytest.Config) -> None:
    if not config.getoption("run_live_gold"):
        return
    raw_workdir = config.getoption("live_gold_workdir")
    if not isinstance(raw_workdir, str) or not raw_workdir:
        raise pytest.UsageError("--run-live-gold requires --live-gold-workdir")
    candidate = Path(raw_workdir)
    if not candidate.is_absolute():
        raise pytest.UsageError("--live-gold-workdir must be an absolute path")
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        resolved = candidate.resolve(strict=True)
    except OSError as error:
        raise pytest.UsageError("--live-gold-workdir could not be created or resolved") from error
    repository = Path(__file__).resolve().parents[2]
    if resolved == repository or resolved.is_relative_to(repository):
        raise pytest.UsageError("--live-gold-workdir must resolve outside the repository")
    config.option.live_gold_workdir = str(resolved)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("run_live_gold"):
        return
    live_root = Path(__file__).resolve().parent
    skip_live = pytest.mark.skip(reason="requires explicit --run-live-gold")
    for item in items:
        if Path(str(item.path)).resolve().is_relative_to(live_root):
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def live_gold_workdir(pytestconfig: pytest.Config) -> Path:
    value = pytestconfig.getoption("live_gold_workdir")
    assert isinstance(value, str)
    return Path(value)
