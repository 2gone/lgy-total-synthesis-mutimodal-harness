from __future__ import annotations

import pytest

from tsdaf.apps.web.app import _mock_route_response, _route_mode_from_environment


def test_web_route_defaults_to_contract_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TSDAF_WEB_ROUTE_MODE", raising=False)

    assert _route_mode_from_environment() == "contract-mock"


def test_contract_mock_never_claims_a_model_execution() -> None:
    result = _mock_route_response("paper-001")

    assert result["ok"] is True
    assert result["exit_code"] == 0
    assert result["payload"] == {
        "schema_version": "tsdaf.web-route-interface.v1",
        "paper_id": "paper-001",
        "status": "INTERFACE_READY",
        "code": "MOCK_ONLY",
        "mode": "contract-mock",
        "provider_protocol": "openai-compatible-chat-completions",
        "model_policy": "backend-selected",
        "model_candidates": ["Gemini-4-argon", "Gemini-3.8-flash"],
        "real_model_called": False,
        "credentials_required": False,
        "message": "接口字段与状态展示已对齐；本次未调用真实模型。",
    }
    assert "execution_id" not in result["payload"]
    assert "route_groups" not in result["payload"]


def test_web_route_mode_rejects_unknown_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TSDAF_WEB_ROUTE_MODE", "surprise")

    with pytest.raises(ValueError, match="TSDAF_WEB_ROUTE_MODE"):
        _route_mode_from_environment()
