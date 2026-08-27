from __future__ import annotations

from pathlib import Path

import pytest

from tsdaf.apps import cli as cli_app


def test_image_cli_passes_fixed_defaults_to_thin_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_image(
        job_path: Path,
        source_package_dir: Path,
        *,
        bundle_root: Path,
        workspace: Path,
        device: str,
        workers: int,
        retries: int,
        timeout: float,
        execution_id: str | None,
        live: bool,
    ) -> int:
        captured.update(locals())
        return 0

    monkeypatch.setattr(cli_app, "run_image", fake_run_image)
    workspace = tmp_path / "workspace"
    code = cli_app.main(
        [
            "image",
            "run",
            str(tmp_path / "job.json"),
            str(tmp_path / "package"),
            "--molyolo-bundle",
            str(tmp_path / "bundle"),
            "--workspace",
            str(workspace),
        ]
    )

    assert code == 0
    assert captured["workers"] == 4
    assert captured["retries"] == 2
    assert captured["timeout"] == 120.0
    assert captured["device"] == "auto"
    assert captured["workspace"] == workspace
    assert captured["live"] is False


@pytest.mark.parametrize(
    "option",
    (("--workers", "0"), ("--retries", "-1"), ("--timeout", "0")),
)
def test_image_cli_rejects_invalid_execution_limits(
    tmp_path: Path,
    option: tuple[str, str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        cli_app.main(
            [
                "image",
                "run",
                str(tmp_path / "job.json"),
                str(tmp_path / "package"),
                "--molyolo-bundle",
                str(tmp_path / "bundle"),
                *option,
            ]
        )

    assert caught.value.code == 2
