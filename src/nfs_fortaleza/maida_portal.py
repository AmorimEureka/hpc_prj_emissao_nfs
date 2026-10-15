from __future__ import annotations

import base64
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping
from urllib.parse import parse_qs, urlsplit

from dlt.sources.helpers.requests import Session
from requests import HTTPError

from nfs_fortaleza.maida_config import MaidaSettings


LOGGER = logging.getLogger(__name__)


class MaidaPortalError(RuntimeError):
    """Raised when a Maida endpoint returns an unexpected response."""


@dataclass(frozen=True, order=True)
class MaidaCompetency:
    year: int
    month: int

    @classmethod
    def parse(cls, value: str) -> MaidaCompetency:
        raw = value.strip()
        for pattern in ("%m/%Y", "%Y-%m", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, pattern)
                return cls(parsed.year, parsed.month)
            except ValueError:
                continue
        raise ValueError(
            f"Competencia invalida: {value!r}. Use MM/AAAA ou AAAA-MM."
        )

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def api_value(self) -> str:
        return self.first_day.isoformat()

    @property
    def label(self) -> str:
        return f"{self.month:02d}/{self.year:04d}"


@dataclass(frozen=True)
class MaidaDocument:
    competency: MaidaCompetency
    source: str
    document_id: str
    file_name: str
    remote_path: str
    lot_id: str | None = None
    lot_type: str | None = None
    sent_at: str | None = None
    processed_at: str | None = None
    appeal_sent_at: str | None = None

    @property
    def is_xlsx(self) -> bool:
        return self.file_name.lower().endswith((".xlsx", ".xlsm"))


@dataclass(frozen=True)
class DownloadedMaidaDocument:
    document: MaidaDocument
    path: Path


@dataclass(frozen=True)
class MaidaCompetencyResult:
    competency: MaidaCompetency
    summary_documents: tuple[MaidaDocument, ...]
    lot_documents: tuple[MaidaDocument, ...]
    lots: tuple[dict[str, Any], ...]
    guides: tuple[dict[str, Any], ...] = ()

    @property
    def selected_documents(self) -> tuple[MaidaDocument, ...]:
        summary_xlsx = tuple(
            document for document in self.summary_documents if document.is_xlsx
        )
        if summary_xlsx:
            return summary_xlsx
        return tuple(document for document in self.lot_documents if document.is_xlsx)


class MaidaPortalClient:
    def __init__(
        self,
        settings: MaidaSettings,
        *,
        downloads_dir: Path,
        timeout_seconds: float = 60,
    ) -> None:
        self.settings = settings
        self.downloads_dir = downloads_dir
        self.timeout_seconds = timeout_seconds
        self.session = Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "receita-certa-maida-dlt/1.0",
            }
        )
        self._provider_id: str | None = settings.provider_id
        self._authenticated = False

    def authenticate(self) -> str:
        if self._authenticated:
            authorization = self.session.headers.get("Authorization", "")
            return authorization.removeprefix("Bearer ")
        token = self._authenticate_onepass()
        if not token:
            raise MaidaPortalError("A autenticacao OnePass nao retornou o token Maida.")
        self.session.headers["Authorization"] = f"Bearer {token}"
        self._authenticated = True
        return str(token)

    def _authenticate_onepass(self) -> str:
        auth_url_response = self.session.get(
            f"{self.settings.accounts_api_url}/login/onepass/get-auth-url",
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(auth_url_response, "URL de autenticacao OnePass")
        auth_url = _response_text_value(auth_url_response)
        auth_query = parse_qs(urlsplit(auth_url).query)
        client_id = _required_query_value(auth_query, "client_id")
        redirect_uri = _required_query_value(auth_query, "redirect_uri")
        response_type = auth_query.get("response_type", ["code"])[0]

        response = self.session.post(
            f"{self.settings.onepass_url}/api/token/authorize",
            json={
                "client_id": client_id,
                "environment": "PRD",
                "response_type": response_type,
                "redirect_uri": redirect_uri,
                "type": "oauth2",
            },
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "autorizacao do cliente OnePass")

        credentials = {
            "username": self.settings.login,
            "password": self.settings.password,
            "recaptchaClientResult": "",
        }
        response = self.session.post(
            f"{self.settings.onepass_url}/api/auth/presignin",
            json=credentials,
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "pre-autenticacao OnePass")
        pre_signin = self._json(response, "pre-autenticacao OnePass")
        if not isinstance(pre_signin, dict):
            raise MaidaPortalError("A pre-autenticacao OnePass retornou dados invalidos.")

        response = self.session.post(
            f"{self.settings.onepass_url}/api/auth/signin/mfa",
            json={**pre_signin, **credentials, "clientId": client_id},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "autenticacao OnePass")
        signin = self._json(response, "autenticacao OnePass")
        if not isinstance(signin, dict):
            raise MaidaPortalError("A autenticacao OnePass retornou dados invalidos.")
        access_token = _first_value(signin, "token", "accessToken")
        user_id = _first_value(signin, "uuidUserData")
        if not access_token or not user_id:
            raise MaidaPortalError(
                "A autenticacao OnePass nao retornou token e usuario."
            )

        response = self.session.post(
            f"{self.settings.onepass_url}/api/auth/refresh_token",
            params={"client_id": client_id},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "renovacao do token OnePass")
        refreshed_token = response.headers.get("Authorization", "").removeprefix(
            "Bearer "
        )
        if not refreshed_token:
            raise MaidaPortalError("O OnePass nao retornou o token renovado.")

        response = self.session.get(
            f"{self.settings.onepass_url}/api/token/getAuthorizationCode/{user_id}",
            headers={"Authorization": f"Bearer {refreshed_token}"},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "codigo de autorizacao OnePass")
        authorization_code = _response_text_value(response)

        response = self.session.get(
            f"{self.settings.accounts_api_url}/login/one-pass/auth/token",
            params={"code": authorization_code},
            allow_redirects=False,
            timeout=self.timeout_seconds,
        )
        if response.status_code not in (301, 302, 303, 307, 308):
            self._raise_for_status(response, "troca do token OnePass")
        location = response.headers.get("Location", "")
        return parse_qs(urlsplit(location).query).get("token", [""])[0]

    def resolve_provider_id(self) -> str:
        token = self.authenticate()
        if self._provider_id:
            return self._provider_id
        subject = _jwt_subject(token)
        response = self.session.get(
            f"{self.settings.provider_api_url}/prestador/sso/{subject}",
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, "prestador vinculado")
        payload = self._json(response, "prestador vinculado")
        provider_id = _first_value(payload, "uuid", "id", "prestadorId")
        if not provider_id:
            raise MaidaPortalError(
                "O Maida nao retornou o identificador do prestador vinculado."
            )
        self._provider_id = str(provider_id)
        return self._provider_id

    def collect_competency(
        self,
        competency: MaidaCompetency,
    ) -> MaidaCompetencyResult:
        provider_id = self.resolve_provider_id()
        summary_documents = tuple(self._summary_documents(competency))
        require_lots = not any(document.is_xlsx for document in summary_documents)
        lots: list[dict[str, Any]] = []
        guides: list[dict[str, Any]] = []
        lot_documents: list[MaidaDocument] = []
        for lot_filter, source in (
            ("lotesGlosasNaoRecursadas", "lote_glosa_nao_recursado"),
            ("lotesGlosasRecursadas", "lote_glosa_recursado"),
        ):
            for lot in self._analyzed_lots(
                competency, provider_id, lot_filter, required=require_lots,
            ):
                details = self._lot_details(lot)
                metadata = _lot_metadata(lot, details, source)
                lots.append(metadata)
                status = (
                    "RECURSADA"
                    if source == "lote_glosa_recursado"
                    else "AGUARDANDO_RECURSO"
                )
                guides.extend(self._eligible_guides(metadata, status))
                lot_documents.extend(
                    self._documents_from_payload(
                        details.get("demonstrativos", []),
                        competency=competency,
                        source=source,
                        lot=metadata,
                    )
                )

        return MaidaCompetencyResult(
            competency=competency,
            summary_documents=summary_documents,
            lot_documents=tuple(_deduplicate_documents(lot_documents)),
            lots=tuple(lots),
            guides=tuple(guides),
        )

    def _eligible_guides(
        self,
        lot: Mapping[str, Any],
        status: str,
    ) -> Iterator[dict[str, Any]]:
        lot_id = lot.get("lote_id")
        if not lot_id:
            return
        page = 0
        while True:
            response = self.session.get(
                f"{self.settings.billing_api_url}/lote-recurso-glosa/"
                f"lote-elegivel/{lot_id}/guias",
                params={
                    "loteId": lot_id,
                    "situacaoGuiaElegivel": status,
                    "page": page,
                    "size": 100,
                },
                timeout=self.timeout_seconds,
            )
            self._raise_for_status(response, f"guias elegiveis do lote {lot_id}")
            payload = self._json(response, f"guias elegiveis do lote {lot_id}")
            content = payload.get("content", []) if isinstance(payload, dict) else []
            if not isinstance(content, list):
                raise MaidaPortalError("A lista de guias elegiveis e invalida.")
            for guide in content:
                if not isinstance(guide, dict):
                    continue
                yield {
                    "lote_id": str(lot_id),
                    "identificador_lote": lot.get("identificador_lote"),
                    "guia_id": _first_value(guide, "id", "guiaId"),
                    "numero_guia": _first_value(
                        guide, "numeroGuiaOperadora", "numeroGuia"
                    ),
                    "numero_carteira": _first_value(
                        guide, "carteiraBeneficiario", "numeroCartao"
                    ),
                }
            total_pages = int(payload.get("totalPages", 0) or 0)
            if not content or page + 1 >= total_pages:
                break
            page += 1

    def download(self, document: MaidaDocument) -> DownloadedMaidaDocument:
        response = self.session.get(
            f"{self.settings.billing_api_url}/download-arquivo",
            params={
                "pathArquivo": document.remote_path,
                "nomeArquivo": document.file_name,
            },
            headers={"Accept": "application/octet-stream, */*"},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, f"download {document.file_name}")
        content = response.content
        if not content.startswith(b"PK"):
            raise MaidaPortalError(
                f"O arquivo {document.file_name!r} nao e um XLSX valido."
            )
        destination_dir = (
            self.downloads_dir
            / f"{document.competency.year:04d}"
            / f"{document.competency.month:02d}"
        )
        destination_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(document.file_name).name
        destination = destination_dir / f"{document.document_id}_{safe_name}"
        temporary = destination.with_suffix(destination.suffix + ".part")
        temporary.write_bytes(content)
        temporary.replace(destination)
        return DownloadedMaidaDocument(document=document, path=destination)

    def _summary_documents(
        self,
        competency: MaidaCompetency,
    ) -> Iterator[MaidaDocument]:
        response = self.session.get(
            f"{self.settings.billing_api_url}/demonstrativo-prestador/visao-prestador",
            params={"competencia": competency.api_value},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, f"resumo {competency.label}")
        payload = self._json(response, f"resumo {competency.label}")
        yield from self._documents_from_payload(
            payload,
            competency=competency,
            source="resumo",
        )

    def _analyzed_lots(
        self,
        competency: MaidaCompetency,
        provider_id: str,
        selected_filter: str,
        *,
        required: bool = False,
    ) -> Iterator[dict[str, Any]]:
        page = 0
        while True:
            params: dict[str, Any] = {
                "identificadorLote": "",
                "competencia": competency.api_value,
                "prestadorId": provider_id,
                "modulo": "MEDICO",
                "lotesGlosasNaoRecursadas": False,
                "lotesGlosasRecursadas": False,
                "lotesRecursoGlosa": False,
                "lotesSemGlosa": False,
                "page": page,
                "size": 100,
            }
            params[selected_filter] = True
            try:
                response = self.session.get(
                    f"{self.settings.billing_api_url}/lote-recurso-glosa/"
                    "lotes-analisados/visao-prestador",
                    params=params,
                    timeout=self.timeout_seconds,
                )
            except HTTPError as exc:
                status = getattr(exc.response, "status_code", 0)
                if 500 <= status < 600:
                    if required:
                        raise MaidaPortalError(
                            f"Sem planilha no Resumo e lotes indisponiveis "
                            f"em {competency.label}; repetir a extracao."
                        ) from exc
                    LOGGER.warning(
                        "Lotes Maida temporariamente indisponiveis: "
                        "competencia=%s filtro=%s pagina=%s HTTP=%s. "
                        "A extracao seguira com os demais dados.",
                        competency.label,
                        selected_filter,
                        page,
                        status,
                    )
                    return
                raise
            self._raise_for_status(
                response,
                f"lotes {selected_filter} {competency.label}",
            )
            payload = self._json(response, "lotes analisados")
            content = payload.get("content", []) if isinstance(payload, dict) else []
            if not isinstance(content, list):
                raise MaidaPortalError("A lista de lotes analisados e invalida.")
            for lot in content:
                if isinstance(lot, dict):
                    yield lot
            total_pages = int(payload.get("totalPages", 0) or 0)
            if not content or page + 1 >= total_pages:
                break
            page += 1

    def _lot_details(self, lot: Mapping[str, Any]) -> dict[str, Any]:
        lot_id = _first_value(lot, "loteId", "id", "uuid")
        lot_type = _first_value(lot, "tipoLote", "tipo")
        if not lot_id or not lot_type:
            raise MaidaPortalError("Lote analisado sem loteId ou tipoLote.")
        response = self.session.get(
            f"{self.settings.billing_api_url}/lote-recurso-glosa/lote/"
            f"{lot_id}/detalhes-lote",
            params={"tipoLote": lot_type},
            timeout=self.timeout_seconds,
        )
        self._raise_for_status(response, f"detalhes do lote {lot_id}")
        payload = self._json(response, f"detalhes do lote {lot_id}")
        if not isinstance(payload, dict):
            raise MaidaPortalError("Os detalhes do lote possuem formato invalido.")
        return payload

    def _documents_from_payload(
        self,
        payload: object,
        *,
        competency: MaidaCompetency,
        source: str,
        lot: Mapping[str, Any] | None = None,
    ) -> Iterator[MaidaDocument]:
        for raw in _walk_document_candidates(payload):
            file_name = _first_value(raw, "nomeArquivo", "fileName", "nome")
            remote_path = _first_value(raw, "pathArquivo", "path", "caminho")
            if not file_name or not remote_path:
                continue
            document_id = _first_value(raw, "documentoId", "id", "uuid")
            if not document_id:
                document_id = hashlib.sha256(
                    f"{remote_path}:{file_name}".encode("utf-8")
                ).hexdigest()
            yield MaidaDocument(
                competency=competency,
                source=source,
                document_id=str(document_id),
                file_name=str(file_name),
                remote_path=str(remote_path),
                lot_id=str(lot.get("lote_id")) if lot and lot.get("lote_id") else None,
                lot_type=str(lot.get("tipo_lote")) if lot and lot.get("tipo_lote") else None,
                sent_at=str(lot.get("data_envio")) if lot and lot.get("data_envio") else None,
                processed_at=(
                    str(lot.get("data_processamento"))
                    if lot and lot.get("data_processamento")
                    else None
                ),
                appeal_sent_at=(
                    str(lot.get("data_envio_recurso"))
                    if lot and lot.get("data_envio_recurso")
                    else None
                ),
            )

    @staticmethod
    def _json(response, operation: str) -> object:
        try:
            return response.json()
        except ValueError as exc:
            raise MaidaPortalError(
                f"O Maida retornou resposta nao JSON em {operation}."
            ) from exc

    @staticmethod
    def _raise_for_status(response, operation: str) -> None:
        try:
            response.raise_for_status()
        except Exception as exc:
            status = getattr(response, "status_code", "desconhecido")
            raise MaidaPortalError(
                f"Falha no Maida em {operation} (HTTP {status})."
            ) from exc


def _jwt_subject(token: str) -> str:
    try:
        encoded = token.split(".")[1]
        encoded += "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
        subject = payload.get("sub")
    except (IndexError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MaidaPortalError("O token Maida retornado e invalido.") from exc
    if not subject:
        raise MaidaPortalError("O token Maida nao contem o identificador sub.")
    return str(subject)


def _response_text_value(response) -> str:
    try:
        payload = response.json()
    except ValueError:
        payload = getattr(response, "text", "")
    if isinstance(payload, str):
        value = payload.strip().strip('"')
    else:
        value = _find_first_string(payload)
    if not value:
        raise MaidaPortalError("O endpoint de autenticacao retornou valor vazio.")
    return value


def _find_first_string(payload: object) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, Mapping):
        for value in payload.values():
            found = _find_first_string(value)
            if found:
                return found
    if isinstance(payload, list):
        for value in payload:
            found = _find_first_string(value)
            if found:
                return found
    return ""


def _required_query_value(query: Mapping[str, list[str]], name: str) -> str:
    values = query.get(name, [])
    if not values or not values[0]:
        raise MaidaPortalError(
            f"A URL de autenticacao OnePass nao contem o parametro {name}."
        )
    return values[0]


def _first_value(payload: Mapping[str, Any], *names: str) -> Any | None:
    for name in names:
        value = payload.get(name)
        if value not in (None, ""):
            return value
    return None


def _walk_document_candidates(payload: object) -> Iterator[Mapping[str, Any]]:
    if isinstance(payload, list):
        for item in payload:
            yield from _walk_document_candidates(item)
    elif isinstance(payload, dict):
        if _first_value(payload, "nomeArquivo", "fileName", "nome") and _first_value(
            payload, "pathArquivo", "path", "caminho"
        ):
            yield payload
            return
        for value in payload.values():
            if isinstance(value, (dict, list)):
                yield from _walk_document_candidates(value)


def _lot_metadata(
    lot: Mapping[str, Any],
    details: Mapping[str, Any],
    source: str,
) -> dict[str, Any]:
    return {
        "lote_id": _first_value(lot, "loteId", "id", "uuid"),
        "tipo_lote": _first_value(lot, "tipoLote", "tipo"),
        "identificador_lote": _first_value(
            lot, "identificador", "identificadorLote", "numeroLote"
        ),
        "origem": source,
        "data_envio": _first_value(details, "dataEnvio"),
        "data_processamento": _first_value(details, "dataProcessamento"),
        "data_envio_recurso": _first_value(
            details,
            "dataEnvioRecurso",
            "dataRecurso",
        ),
        "possui_glosa": details.get("possuiGlosa"),
        "possui_demonstrativo": details.get("possuiDemonstrativo"),
    }


def _deduplicate_documents(
    documents: Iterable[MaidaDocument],
) -> Iterator[MaidaDocument]:
    seen: set[tuple[str, str]] = set()
    for document in documents:
        key = (document.remote_path, document.file_name)
        if key in seen:
            continue
        seen.add(key)
        yield document
