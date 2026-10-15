from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from requests import HTTPError

from nfs_fortaleza.maida_config import MaidaSettings
from nfs_fortaleza.maida_portal import (
    MaidaCompetency,
    MaidaDocument,
    MaidaCompetencyResult,
    _jwt_subject,
    MaidaPortalClient,
)


class _Response:
    def __init__(
        self,
        payload: object,
        status_code: int = 200,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.content = b""
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload)

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _Session:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs))
        return _Response({"maidaToken": "token-sem-expor-credencial"})

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/demonstrativo-prestador/visao-prestador"):
            return _Response([])
        if url.endswith("/lotes-analisados/visao-prestador"):
            selected = kwargs["params"]
            lot_id = (
                "nao-recursado"
                if selected["lotesGlosasNaoRecursadas"]
                else "recursado"
            )
            return _Response(
                {
                    "content": [{"loteId": lot_id, "tipoLote": "COBRANCA"}],
                    "totalPages": 1,
                }
            )
        if "/detalhes-lote" in url:
            lot_id = url.split("/")[-2]
            return _Response(
                {
                    "dataEnvio": "2026-01-10T10:00:00",
                    "dataProcessamento": "2026-01-11T10:00:00",
                    "dataEnvioRecurso": "2026-01-12T10:00:00",
                    "demonstrativos": [
                        {
                            "documentoId": f"doc-{lot_id}",
                            "nomeArquivo": f"{lot_id}.xlsx",
                            "pathArquivo": f"/arquivos/{lot_id}.xlsx",
                        }
                    ],
                }
            )
        if "/lote-elegivel/" in url and url.endswith("/guias"):
            lot_id = url.split("/")[-2]
            return _Response(
                {
                    "content": [
                        {
                            "id": f"guia-{lot_id}",
                            "numeroGuiaOperadora": "521882",
                            "carteiraBeneficiario": "000123456",
                        }
                    ],
                    "totalPages": 1,
                }
            )
        raise AssertionError(f"Endpoint inesperado: {url}")


def test_jwt_subject_is_read_without_logging_token() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "user-123"}).encode())
    token = "header." + payload.decode().rstrip("=") + ".signature"
    assert _jwt_subject(token) == "user-123"


def test_summary_xlsx_has_precedence_over_lot_documents() -> None:
    competency = MaidaCompetency(2026, 1)
    summary = MaidaDocument(
        competency, "resumo", "summary", "resumo.xlsx", "/resumo.xlsx"
    )
    lot = MaidaDocument(
        competency, "lote_glosa", "lot", "lote.xlsx", "/lote.xlsx"
    )
    result = MaidaCompetencyResult(competency, (summary,), (lot,), ())
    assert result.selected_documents == (summary,)


def test_lot_xlsx_is_fallback_when_summary_has_no_xlsx() -> None:
    competency = MaidaCompetency(2026, 1)
    summary_pdf = MaidaDocument(
        competency, "resumo", "pdf", "resumo.pdf", "/resumo.pdf"
    )
    lot = MaidaDocument(
        competency, "lote_glosa", "lot", "lote.xlsx", "/lote.xlsx"
    )
    result = MaidaCompetencyResult(competency, (summary_pdf,), (lot,), ())
    assert result.selected_documents == (lot,)


def test_collect_competency_uses_both_analyzed_lot_filters(tmp_path: Path) -> None:
    settings = MaidaSettings(
        login="login",
        password="password",
        database_url="postgresql://example",
        provider_id="provider-1",
    )
    client = MaidaPortalClient(settings, downloads_dir=tmp_path)
    session = _Session()
    client.session = session  # type: ignore[assignment]
    client._authenticated = True
    session.headers["Authorization"] = "Bearer token-sem-expor-credencial"

    result = client.collect_competency(MaidaCompetency(2026, 1))

    assert len(result.lots) == 2
    assert len(result.guides) == 2
    assert {item["numero_carteira"] for item in result.guides} == {"000123456"}
    assert len(result.selected_documents) == 2
    assert {item.source for item in result.selected_documents} == {
        "lote_glosa_nao_recursado",
        "lote_glosa_recursado",
    }
    list_calls = [
        call for call in session.calls if call[1].endswith("visao-prestador")
        and "lotes-analisados" in call[1]
    ]
    assert list_calls[0][2]["params"]["lotesGlosasNaoRecursadas"] is True
    assert list_calls[1][2]["params"]["lotesGlosasRecursadas"] is True
    guide_calls = [call for call in session.calls if call[1].endswith("/guias")]
    assert {call[2]["params"]["situacaoGuiaElegivel"] for call in guide_calls} == {
        "AGUARDANDO_RECURSO",
        "RECURSADA",
    }
    assert session.headers["Authorization"].startswith("Bearer ")


class _OnePassSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/login/onepass/get-auth-url"):
            return _Response(
                "https://onepass.mv.com.br/oauth2/auth?"
                "client_id=client-123&redirect_uri=https%3A%2F%2Faccounts.example%2Fcallback&"
                "response_type=code"
            )
        if "/api/token/getAuthorizationCode/" in url:
            return _Response("authorization-code")
        if url.endswith("/login/one-pass/auth/token"):
            return _Response(
                "",
                302,
                headers={"Location": "https://issec.maida.health/sso/login?token=maida-token"},
            )
        raise AssertionError(f"Endpoint GET inesperado: {url}")

    def post(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(("POST", url, kwargs))
        if url.endswith("/api/token/authorize"):
            return _Response("ok")
        if url.endswith("/api/auth/presignin"):
            return _Response({"uuidUserData": "user-123", "email": "masked"})
        if url.endswith("/api/auth/signin/mfa"):
            return _Response({"token": "access-token", "uuidUserData": "user-123"})
        if url.endswith("/api/auth/refresh_token"):
            return _Response("", 204, headers={"Authorization": "Bearer refreshed-token"})
        raise AssertionError(f"Endpoint POST inesperado: {url}")


def test_authenticate_uses_onepass_oauth_flow(tmp_path: Path) -> None:
    settings = MaidaSettings(
        login="login",
        password="password",
        database_url="postgresql://example",
    )
    client = MaidaPortalClient(settings, downloads_dir=tmp_path)
    session = _OnePassSession()
    client.session = session  # type: ignore[assignment]

    assert client.authenticate() == "maida-token"
    assert session.headers["Authorization"] == "Bearer maida-token"
    paths = [url for _, url, _ in session.calls]
    assert any(path.endswith("/api/auth/presignin") for path in paths)
    assert any(path.endswith("/api/auth/signin/mfa") for path in paths)
    assert any(path.endswith("/login/one-pass/auth/token") for path in paths)
    signin_call = next(
        call for call in session.calls if call[1].endswith("/api/auth/signin/mfa")
    )
    assert signin_call[2]["json"]["clientId"] == "client-123"


class _UnavailableLotsSession:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}

    def get(self, url: str, **kwargs: Any) -> _Response:
        response = _Response({"message": "indisponivel"}, 500)
        error = HTTPError("server error")
        error.response = response  # type: ignore[assignment]
        raise error


def test_server_error_in_lots_is_treated_as_temporarily_unavailable(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = MaidaSettings(
        login="login",
        password="password",
        database_url="postgresql://example",
        provider_id="provider-1",
    )
    client = MaidaPortalClient(settings, downloads_dir=tmp_path)
    client.session = _UnavailableLotsSession()  # type: ignore[assignment]

    lots = list(
        client._analyzed_lots(
            MaidaCompetency(2026, 6),
            "provider-1",
            "lotesGlosasRecursadas",
        )
    )

    assert lots == []
    assert "temporariamente indisponiveis" in caplog.text


def test_missing_summary_requires_successful_lot_listing(tmp_path):
    from nfs_fortaleza.maida_portal import MaidaPortalError
    settings = MaidaSettings(login="test", password="test", database_url="postgresql://example")
    client = MaidaPortalClient(settings, downloads_dir=tmp_path)
    client.session = _UnavailableLotsSession()
    with pytest.raises(MaidaPortalError, match="repetir a extracao"):
        list(client._analyzed_lots(MaidaCompetency(2026, 9), "provider", "lotesGlosasRecursadas", required=True))


def test_fallback_traverses_all_lot_pages(tmp_path):
    class Pages(_Session):
        def get(self, url, **kwargs):
            if url.endswith('/lotes-analisados/visao-prestador'):
                self.calls.append(('GET', url, kwargs))
                page = kwargs['params']['page']
                return _Response({'content': [{'loteId': str(page), 'tipoLote': 'COBRANCA'}], 'totalPages': 3})
            return super().get(url, **kwargs)
    client = MaidaPortalClient(MaidaSettings(login='test', password='test', database_url='postgresql://example'), downloads_dir=tmp_path)
    client.session = Pages()
    lots = list(client._analyzed_lots(MaidaCompetency(2026, 9), 'provider', 'lotesGlosasNaoRecursadas', required=True))
    assert [lot['loteId'] for lot in lots] == ['0', '1', '2']
