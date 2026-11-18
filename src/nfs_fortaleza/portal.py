from __future__ import annotations

import re
import unicodedata
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

from dlt.sources.helpers.requests import Response, Session

from nfs_fortaleza.config import DEFAULT_ARTIFACTS_DIR, DEFAULT_DOWNLOADS_DIR, Settings
from nfs_fortaleza.periods import DateRangePeriod, MonthPeriod


CONSULTA_NFSE_PAGE = "pages/nfse/consultaNfsePorNumero.seam"
QueryPeriod = MonthPeriod | DateRangePeriod

# "Serviços Prestados" (opTipoRelatorio=1): notas emitidas pelas inscrições.
# "Serviços Tomados" (opTipoRelatorio=2): notas recebidas pelas inscrições.
VISAO_PRESTADOR = "prestador"
VISAO_TOMADOR = "tomador"
VISOES = (VISAO_PRESTADOR, VISAO_TOMADOR)

# Ids dos componentes RichFaces da aba "Período de Emissão/Tomador",
# levantados no portal em 17/11/2026 (consultaNfsePorNumero.seam).
TOMADOR_TROCA_TIPO_EVENT = "consultarnfseForm:j_id178"
TOMADOR_PERIODO_TAB = "consultarnfseForm:abaPorPeriodoTomador_tab"
TOMADOR_PERIODO_TAB_EVENT = "consultarnfseForm:j_id250"
TOMADOR_CONSULTAR_EVENT = "consultarnfseForm:j_id317"


class PeriodWithoutInvoicesError(RuntimeError):
    """Raised when the NFS-e query has no result for the requested period."""


class InscricaoNotFoundError(RuntimeError):
    """Raised when the requested CNPJ is not available to the logged-in user."""


@dataclass(frozen=True)
class PortalOptions:
    downloads_dir: Path = DEFAULT_DOWNLOADS_DIR
    artifacts_dir: Path = DEFAULT_ARTIFACTS_DIR
    timeout_ms: int = 45_000
    query_date: date | None = None
    ca_bundle: Path | None = None
    visao: str = VISAO_PRESTADOR

    def __post_init__(self) -> None:
        if self.visao not in VISOES:
            raise ValueError(f"visao deve ser uma de {VISOES}: {self.visao!r}")


@dataclass(frozen=True)
class InscricaoRow:
    index: str
    documento: str
    inscricao: str
    nome: str

    @property
    def label(self) -> str:
        return f"{self.inscricao} - {self.documento} - {self.nome}"


@dataclass(frozen=True)
class ListedNfse:
    """Linha da lista de resultados do portal (o XML não traz a situação)."""

    numero_nfse: str
    documento: str
    situacao: str
    data_emissao: date | None
    inscricao_documento: str | None = None


class PortalClient:
    def __init__(self, settings: Settings, options: PortalOptions | None = None) -> None:
        self.settings = settings
        self.options = options or PortalOptions()
        # Situação de cada NFS-e vista nas páginas de resultado (NORMAL,
        # CANCELADA, SUBSTITUIDA...). Preenchida por export_competencia.
        self.listed_nfse: list[ListedNfse] = []
        self.query_date = self.options.query_date or datetime.now(
            ZoneInfo("America/Fortaleza")
        ).date()

    def export_competencia(self, competencia: QueryPeriod) -> list[Path]:
        self.options.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.options.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if competencia.first_day > self.query_date:
            raise PeriodWithoutInvoicesError(
                f"Competencia {competencia.mm_yyyy} esta no futuro; a data inicial seria maior que hoje."
            )

        session = self._login_dlt_session()
        rows = self._available_inscricoes(session)
        if not rows:
            return [self._export_competencia_with_requests(session, competencia)]

        downloaded: list[Path] = []
        ignored: list[str] = []
        for position, discovered_row in enumerate(rows):
            # Selecting an inscription replaces the selection table with the
            # current-inscription view. A new session is therefore required
            # before selecting the next row; reusing its old table index keeps
            # the first inscription active and exports the same notes again.
            if position == 0:
                current_session = session
                row = discovered_row
            else:
                current_session = self._login_dlt_session()
                current_rows = self._available_inscricoes(current_session)
                if not current_rows:
                    raise RuntimeError(
                        "Portal nao apresentou a lista de inscricoes em uma nova sessao."
                    )
                row = _find_inscricao_by_cnpj(
                    current_rows,
                    discovered_row.documento,
                )

            home = self._request_get(
                current_session,
                self.settings.portal_page("home.seam"),
            )
            self._select_inscricao(current_session, home, row)
            try:
                exported = self._export_competencia_with_requests(
                    current_session,
                    competencia,
                    row,
                )
                _validate_exported_inscricao(exported, row, self.options.visao)
                downloaded.append(exported)
            except PeriodWithoutInvoicesError:
                ignored.append(row.label)

        if downloaded:
            return downloaded
        raise PeriodWithoutInvoicesError(
            "Nenhuma NFS-e encontrada para o periodo consultado nas inscricoes: " + "; ".join(ignored)
        )

    def export_nfse(self, cnpj: str, numero_nfse: str) -> list[Path]:
        """Export one NFS-e selected by issuer CNPJ and invoice number."""
        self.options.downloads_dir.mkdir(parents=True, exist_ok=True)
        self.options.artifacts_dir.mkdir(parents=True, exist_ok=True)

        session = self._login_dlt_session()
        rows = self._available_inscricoes(session)
        selected: InscricaoRow | None = None
        if rows:
            selected = _find_inscricao_by_cnpj(rows, cnpj)
            home = self._request_get(session, self.settings.portal_page("home.seam"))
            self._select_inscricao(session, home, selected)

        return [self._export_nfse_with_requests(session, cnpj, numero_nfse, selected)]

    def _login_dlt_session(self) -> Session:
        session = self._new_session()

        initial = self._request_get(session, self.settings.portal_url)
        if _looks_authenticated_html(initial.text):
            self._open_home_after_login(session, initial)
            return session

        login_url = _extract_login_url(initial.url, initial.text)
        login_page = self._request_get(session, login_url)
        login_form = _extract_login_form(login_page.text)
        action_url = urljoin(login_page.url, unescape(login_form["action"]))
        payload = _login_payload(login_form, self.settings.cpf_login, self.settings.senha)

        result = session.post(
            action_url,
            data=payload,
            headers={
                "Referer": login_page.url,
                "Origin": _origin(action_url),
            },
            timeout=self.options.timeout_ms / 1000,
        )
        result.raise_for_status()

        if _looks_login_failed(result.text):
            self._save_http_artifact("falha_login_dlt", result.text)
            raise RuntimeError("Login por dlt requests recusado pelo portal. Verifique CPF/senha ou bloqueios do IdP.")

        self._open_home_after_login(session, result)
        return session

    def _new_session(self) -> Session:
        session = Session()
        if self.options.ca_bundle is not None:
            ca_bundle = self.options.ca_bundle.resolve()
            if not ca_bundle.is_file():
                raise RuntimeError(
                    f"Bundle TLS do ISS Fortaleza nao encontrado: {ca_bundle}"
                )
            session.verify = str(ca_bundle)
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7",
            }
        )

        return session

    def _open_home_after_login(self, session: Session, response: Response) -> Response:
        home_url = _extract_home_url(response.url, response.text) or self.settings.portal_page("home.seam")
        return self._request_get(session, home_url)

    def _available_inscricoes(self, session: Session) -> list[InscricaoRow]:
        home = self._request_get(session, self.settings.portal_page("home.seam"))
        return _extract_inscricao_rows(home.text)

    def _ensure_inscricao_selected(self, session: Session, home: Response) -> None:
        if _has_current_inscricao(home.text):
            return

        rows = _extract_inscricao_rows(home.text)
        if not rows:
            return

        selected = _select_inscricao_row(
            rows,
            cnpj=self.settings.inscricao_cnpj,
            inscricao=self.settings.inscricao_municipal,
            nome=self.settings.inscricao_nome,
        )
        self._select_inscricao(session, home, selected)

    def _select_inscricao(self, session: Session, home: Response, selected: InscricaoRow) -> None:
        view_state = _extract_view_state(home.text)
        payload = {
            "AJAXREQUEST": "_viewRoot",
            "alteraInscricaoForm": "alteraInscricaoForm",
            "alteraInscricaoForm:cpfPesquisa": "",
            "alteraInscricaoForm:sugestaoPesquisa_selection": "",
            "alteraInscricaoForm:tipoPesquisa": "CPF",
            "javax.faces.ViewState": view_state,
            "conversationPropagation": "none",
            "AJAX:EVENTS_COUNT": "1",
        }
        field = f"alteraInscricaoForm:empresaDataTable:{selected.index}:linkInscricao"
        payload[field] = field
        self._request_post(session, home.url, payload, ajax=True)

    def _open_nfse_query(self, session: Session) -> Response:
        query = self._request_get(session, self.settings.portal_page(CONSULTA_NFSE_PAGE))
        if "Consultar NFS-e" in query.text:
            return query

        query_with_cid = self._request_get(
            session,
            _with_cid(self.settings.portal_page(CONSULTA_NFSE_PAGE), query.url),
        )
        if "Consultar NFS-e" in query_with_cid.text:
            return query_with_cid

        home = self._request_get(session, self.settings.portal_page("home.seam"))
        self._ensure_inscricao_selected(session, home)
        return self._request_get(session, self.settings.portal_page(CONSULTA_NFSE_PAGE))

    def _export_competencia_with_requests(
        self,
        session: Session,
        competencia: QueryPeriod,
        inscricao: InscricaoRow | None = None,
    ) -> Path:
        query_response = self._open_nfse_query(session)
        query_url = query_response.url
        html = query_response.text
        if "Consultar NFS-e" not in html:
            debug = self._save_http_artifact("consulta_nao_abriu", html)
            raise RuntimeError(f"Tela Consultar NFS-e nao abriu. Debug salvo em {debug}")
        view_state = _extract_view_state(html)

        for step in self._period_tab_steps():
            payload = {
                "AJAXREQUEST": "_viewRoot",
                "consultarnfseForm": "consultarnfseForm",
                **step,
                "javax.faces.ViewState": view_state,
                "AJAX:EVENTS_COUNT": "1",
            }
            view_state = self._ajax_post(session, query_url, payload, view_state)

        response = self._request_post(session, query_url, self._query_payload(competencia, view_state), ajax=True)
        current_text = unescape(response.text)
        self._raise_for_portal_messages(current_text)
        current_view_state = _extract_view_state(current_text, default=view_state)

        downloaded: list[Path] = []
        seen_pages: set[str] = set()
        page_index = 1

        while True:
            row_command_ids = _extract_xml_row_command_ids(current_text)
            if not row_command_ids:
                break

            page_key = _result_page_fingerprint(
                current_text,
                current_view_state,
            )
            if page_key in seen_pages:
                raise RuntimeError(
                    "Portal repetiu uma pagina da consulta de NFS-e; "
                    "carga cancelada para evitar resultado parcial."
                )
            seen_pages.add(page_key)
            self.listed_nfse.extend(
                _extract_listed_nfse(
                    current_text,
                    _digits(inscricao.documento) if inscricao else None,
                )
            )

            for row_position, row_command_id in enumerate(
                row_command_ids,
                start=1,
            ):
                prefix = _download_prefix(competencia, inscricao, self.options.visao)
                file_name = (
                    f"{prefix}"
                    f"-p{page_index:03d}-n{row_position:03d}.xml"
                )
                downloaded.append(
                    self._download_xml_with_requests(
                        session,
                        query_url,
                        current_view_state,
                        row_command_id,
                        file_name,
                        self._form_payload(competencia, current_view_state),
                    )
                )

            next_response = self._request_next_page(session, query_url, competencia, current_view_state, current_text)
            if next_response is None:
                break
            current_text = unescape(next_response.text)
            self._raise_for_portal_messages(current_text)
            current_view_state = _extract_view_state(current_text, default=current_view_state)
            page_index += 1

        if not downloaded:
            debug = self._save_http_artifact("consulta_sem_xml", current_text)
            raise PeriodWithoutInvoicesError(
                "Nenhuma NFS-e encontrada para o periodo consultado. "
                f"Debug salvo em {debug}"
            )
        if len(downloaded) == 1:
            return downloaded[0]
        return self._zip_downloads(
            downloaded,
            f"{_download_prefix(competencia, inscricao, self.options.visao)}.zip",
        )

    def _export_nfse_with_requests(
        self,
        session: Session,
        cnpj: str,
        numero_nfse: str,
        inscricao: InscricaoRow | None = None,
    ) -> Path:
        query_response = self._open_nfse_query(session)
        query_url = query_response.url
        html = query_response.text
        if "Consultar NFS-e" not in html:
            debug = self._save_http_artifact("consulta_numero_nao_abriu", html)
            raise RuntimeError(f"Tela Consultar NFS-e nao abriu. Debug salvo em {debug}")
        view_state = _extract_view_state(html)

        view_state = self._ajax_post(
            session,
            query_url,
            {
                "AJAXREQUEST": "_viewRoot",
                "consultarnfseForm": "consultarnfseForm",
                "consultarnfseForm:opTipoRelatorio": "1",
                "consultarnfseForm:numNfse": "",
                "javax.faces.ViewState": view_state,
                "consultarnfseForm:j_id170": "consultarnfseForm:j_id170",
                "AJAX:EVENTS_COUNT": "1",
            },
            view_state,
        )
        view_state = self._ajax_post(
            session,
            query_url,
            {
                "AJAXREQUEST": "_viewRoot",
                "consultarnfseForm": "consultarnfseForm",
                "consultarnfseForm:opTipoRelatorio": "1",
                "consultarnfseForm:numNfse": "",
                "javax.faces.ViewState": view_state,
                "ajaxSingle": "consultarnfseForm:numero_doc_tab_id",
                "consultarnfseForm:j_id184": "consultarnfseForm:j_id184",
                "AJAX:EVENTS_COUNT": "1",
            },
            view_state,
        )
        view_state = self._ajax_post(
            session,
            query_url,
            {
                "AJAXREQUEST": "_viewRoot",
                "consultarnfseForm": "consultarnfseForm",
                "consultarnfseForm:opTipoRelatorio": "1",
                "consultarnfseForm:numNfse": "",
                "javax.faces.ViewState": view_state,
                "consultarnfseForm:numero_doc_tab_id": "consultarnfseForm:numero_doc_tab_id",
                "AJAX:EVENTS_COUNT": "1",
            },
            view_state,
        )

        response = self._request_post(
            session,
            query_url,
            self._number_query_payload(numero_nfse, view_state),
            ajax=True,
        )
        current_text = unescape(response.text)
        self._raise_for_portal_messages(current_text)
        current_view_state = _extract_view_state(current_text, default=view_state)
        row_command_ids = _extract_xml_row_command_ids(current_text)
        if not row_command_ids:
            debug = self._save_http_artifact("consulta_numero_sem_xml", current_text)
            raise PeriodWithoutInvoicesError(
                f"NFS-e {numero_nfse} nao encontrada para o CNPJ {cnpj}. Debug salvo em {debug}"
            )

        downloaded: list[Path] = []
        prefix = _nfse_download_prefix(numero_nfse, cnpj, inscricao)
        for position, row_command_id in enumerate(row_command_ids, start=1):
            suffix = "" if len(row_command_ids) == 1 else f"-n{position:03d}"
            downloaded.append(
                self._download_xml_with_requests(
                    session,
                    query_url,
                    current_view_state,
                    row_command_id,
                    f"{prefix}{suffix}.xml",
                    self._number_form_payload(numero_nfse, current_view_state),
                )
            )

        if len(downloaded) == 1:
            return downloaded[0]
        return self._zip_downloads(downloaded, f"{prefix}.zip")

    def _ajax_post(
        self,
        session: Session,
        url: str,
        payload: dict[str, str],
        current_view_state: str,
    ) -> str:
        response = self._request_post(session, url, payload, ajax=True)
        return _extract_view_state(response.text, default=current_view_state)

    def _period_tab_steps(self) -> list[dict[str, str]]:
        """Eventos AJAX que deixam a aba de período da visão ativa no servidor."""
        if self.options.visao == VISAO_TOMADOR:
            base = {"consultarnfseForm:opTipoRelatorio": "2"}
            return [
                {
                    **base,
                    TOMADOR_TROCA_TIPO_EVENT: TOMADOR_TROCA_TIPO_EVENT,
                    "consultarnfseForm:paramSelectedTabTomador": "abaPorPeriodoTomador_tab",
                    "consultarnfseForm:paramSelectedTabPrestador": "numero_doc_tab_id",
                },
                {
                    **base,
                    "ajaxSingle": TOMADOR_PERIODO_TAB,
                    TOMADOR_PERIODO_TAB_EVENT: TOMADOR_PERIODO_TAB_EVENT,
                },
                {**base, TOMADOR_PERIODO_TAB: TOMADOR_PERIODO_TAB},
            ]
        base = {
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:numNfse": "",
        }
        return [
            {**base, "consultarnfseForm:j_id170": "consultarnfseForm:j_id170"},
            {
                **base,
                "ajaxSingle": "consultarnfseForm:periodo_prestador_tab",
                "consultarnfseForm:j_id191": "consultarnfseForm:j_id191",
            },
            {
                **base,
                "consultarnfseForm:periodo_prestador_tab": "consultarnfseForm:periodo_prestador_tab",
            },
        ]

    def _tomador_period_fields(self, competencia: QueryPeriod) -> dict[str, str]:
        query_end_day = competencia.query_end_day(self.query_date)
        return {
            "consultarnfseForm:opTipoRelatorio": "2",
            "consultarnfseForm:dataInicialTomadorInputDate": competencia.first_day_br,
            "consultarnfseForm:dataInicialTomadorInputCurrentDate": competencia.start_month_year_br,
            "consultarnfseForm:dataFinalTomadorInputDate": query_end_day.strftime("%d/%m/%Y"),
            "consultarnfseForm:dataFinalTomadorInputCurrentDate": query_end_day.strftime("%m/%Y"),
            # 2 = "Todos" (prestadores pessoa física e jurídica)
            "consultarnfseForm:opPrestador": "2",
        }

    def _query_payload(self, competencia: QueryPeriod, view_state: str) -> dict[str, str]:
        if self.options.visao == VISAO_TOMADOR:
            return {
                "AJAXREQUEST": "_viewRoot",
                "consultarnfseForm": "consultarnfseForm",
                **self._tomador_period_fields(competencia),
                "javax.faces.ViewState": view_state,
                TOMADOR_CONSULTAR_EVENT: TOMADOR_CONSULTAR_EVENT,
                "AJAX:EVENTS_COUNT": "1",
            }
        query_end_day = competencia.query_end_day(self.query_date)
        end_date = query_end_day.strftime("%d/%m/%Y")
        return {
            "AJAXREQUEST": "_viewRoot",
            "consultarnfseForm": "consultarnfseForm",
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:dataInicialInputDate": competencia.first_day_br,
            "consultarnfseForm:dataInicialInputCurrentDate": competencia.start_month_year_br,
            "consultarnfseForm:dataFinalInputDate": end_date,
            "consultarnfseForm:dataFinalInputCurrentDate": query_end_day.strftime("%m/%Y"),
            "consultarnfseForm:opTomadorPeriodoEmissao": "2",
            "javax.faces.ViewState": view_state,
            "consultarnfseForm:j_id237": "consultarnfseForm:j_id237",
            "AJAX:EVENTS_COUNT": "1",
        }

    def _form_payload(self, competencia: QueryPeriod, view_state: str) -> dict[str, str]:
        if self.options.visao == VISAO_TOMADOR:
            return {
                "consultarnfseForm": "consultarnfseForm",
                **self._tomador_period_fields(competencia),
                "javax.faces.ViewState": view_state,
            }
        query_end_day = competencia.query_end_day(self.query_date)
        end_date = query_end_day.strftime("%d/%m/%Y")
        return {
            "consultarnfseForm": "consultarnfseForm",
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:dataInicialInputDate": competencia.first_day_br,
            "consultarnfseForm:dataInicialInputCurrentDate": competencia.start_month_year_br,
            "consultarnfseForm:dataFinalInputDate": end_date,
            "consultarnfseForm:dataFinalInputCurrentDate": query_end_day.strftime("%m/%Y"),
            "consultarnfseForm:opTomadorPeriodoEmissao": "2",
            "javax.faces.ViewState": view_state,
        }

    def _number_query_payload(self, numero_nfse: str, view_state: str) -> dict[str, str]:
        return {
            "AJAXREQUEST": "_viewRoot",
            "consultarnfseForm": "consultarnfseForm",
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:numNfse": numero_nfse,
            "javax.faces.ViewState": view_state,
            "consultarnfseForm:j_id237": "consultarnfseForm:j_id237",
            "AJAX:EVENTS_COUNT": "1",
        }

    def _number_form_payload(self, numero_nfse: str, view_state: str) -> dict[str, str]:
        return {
            "consultarnfseForm": "consultarnfseForm",
            "consultarnfseForm:opTipoRelatorio": "1",
            "consultarnfseForm:numNfse": numero_nfse,
            "javax.faces.ViewState": view_state,
        }

    def _download_xml_with_requests(
        self,
        session: Session,
        url: str,
        view_state: str,
        row_command_id: str,
        fallback_name: str,
        form_payload: dict[str, str],
    ) -> Path:
        payload = dict(form_payload)
        payload["javax.faces.ViewState"] = view_state
        payload[row_command_id] = row_command_id
        response = self._request_post(session, url, payload, ajax=False)
        content = response.content

        if not response.ok or not content or _looks_like_html(content):
            debug = self.options.artifacts_dir / f"xml_requests_falha_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
            debug.write_bytes(content)
            raise RuntimeError(
                f"Exportacao XML por dlt requests falhou: status={response.status_code}, "
                f"bytes={len(content)}, debug={debug}"
            )

        target = self.options.downloads_dir / fallback_name
        if target.exists():
            target = self.options.downloads_dir / fallback_name
        target.write_bytes(content)
        return target

    def _request_next_page(
        self,
        session: Session,
        url: str,
        competencia: QueryPeriod,
        view_state: str,
        current_text: str,
    ) -> Response | None:
        next_page_command_id = _extract_enabled_next_page_command_id(current_text)
        if next_page_command_id is None:
            return None

        payload = self._form_payload(competencia, view_state)
        payload.update(
            {
                "AJAXREQUEST": "_viewRoot",
                "ajaxSingle": next_page_command_id,
                next_page_command_id: "next",
                "AJAX:EVENTS_COUNT": "1",
            }
        )
        response = self._request_post(session, url, payload, ajax=True)
        text = unescape(response.text)
        if "consultarnfseForm:dataTable" not in text:
            return None
        return response

    def _request_get(self, session: Session, url: str) -> Response:
        response = session.get(url, timeout=self.options.timeout_ms / 1000)
        response.raise_for_status()
        return response

    def _request_post(self, session: Session, url: str, data: dict[str, str], *, ajax: bool) -> Response:
        headers = {
            "Referer": url,
            "Origin": self.settings.portal_origin,
        }
        if ajax:
            headers.update(
                {
                    "Faces-Request": "partial/ajax",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "*/*",
                }
            )
        response = session.post(
            url,
            data=data,
            headers=headers,
            timeout=self.options.timeout_ms / 1000,
        )
        response.raise_for_status()
        return response

    def _raise_for_portal_messages(self, body_text: str) -> None:
        if re.search(r"Data final da pesquisa.*superior.*data de hoje", body_text, flags=re.I):
            raise RuntimeError(
                "Portal recusou a consulta porque a data final ficou superior a hoje. "
                "A aplicacao limita automaticamente a data final para competencias em andamento."
            )
        if re.search(r"Per[ií]odo escolhido para a consulta tem mais de um m[eê]s|m[aá]ximo de 31 dias", body_text, flags=re.I):
            raise RuntimeError(
                "Portal recusou a consulta porque o periodo excede 31 dias. "
                "Execute por competencia mensal ou use --inicio/--fim para a CLI dividir mes a mes."
            )
        if re.search(r"Nenhum|nenhuma|não encontrou|nao encontrou|Nenhum registro foi encontrado", body_text, flags=re.I):
            raise PeriodWithoutInvoicesError("Nenhuma NFS-e encontrada para o periodo consultado.")

    def _save_http_artifact(self, prefix: str, html: str) -> Path:
        target = self.options.artifacts_dir / f"{prefix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        target.write_text(html, encoding="utf-8")
        return target

    def _zip_downloads(self, files: list[Path], file_name: str) -> Path:
        target = self.options.downloads_dir / file_name
        with zipfile.ZipFile(target, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
            for file in files:
                archive.write(file, arcname=file.name)
        return target


class _LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._text).strip()))
            self._href = None
            self._text = []


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, object]] = []
        self._form: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {key: value or "" for key, value in attrs}
        if tag.lower() == "form":
            self._form = {
                "action": attrs_dict.get("action", ""),
                "method": attrs_dict.get("method", "get"),
                "id": attrs_dict.get("id", ""),
                "inputs": [],
            }
        elif tag.lower() == "input" and self._form is not None:
            inputs = self._form["inputs"]
            assert isinstance(inputs, list)
            inputs.append(attrs_dict)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "form" and self._form is not None:
            self.forms.append(self._form)
            self._form = None


def _extract_login_url(current_url: str, html: str) -> str:
    links = _LinkParser()
    links.feed(html)
    for href, label in links.links:
        haystack = f"{label} {href}"
        if re.search(r"Fazer login|Entrar|Acessar|oauth|openid|idp|login", haystack, flags=re.I):
            return urljoin(current_url, unescape(href))
    raise RuntimeError("Link de login nao encontrado na pagina inicial do portal.")


def _extract_home_url(current_url: str, html: str) -> str | None:
    links = _LinkParser()
    links.feed(html)
    for href, label in links.links:
        if "home.seam" in href and re.search(r"P[aá]gina Inicial", label, flags=re.I):
            return urljoin(current_url, unescape(href))
    for href, _label in links.links:
        if "home.seam" in href:
            return urljoin(current_url, unescape(href))
    return None


def _extract_login_form(html: str) -> dict[str, object]:
    forms = _FormParser()
    forms.feed(html)
    for form in forms.forms:
        if form.get("id") == "kc-form-login":
            return form
    for form in forms.forms:
        inputs = form.get("inputs", [])
        if isinstance(inputs, list) and any((field.get("type") or "").lower() == "password" for field in inputs):
            return form
    raise RuntimeError("Formulario de login com senha nao encontrado na pagina do IdP.")


def _login_payload(form: dict[str, object], username: str, password: str) -> dict[str, str]:
    payload: dict[str, str] = {}
    inputs = form.get("inputs", [])
    if not isinstance(inputs, list):
        return payload

    for field in inputs:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "")
        if not name:
            continue
        input_type = str(field.get("type") or "").lower()
        field_id = str(field.get("id") or "").lower()
        value = str(field.get("value") or "")

        lowered = f"{name} {field_id}".lower()
        if input_type == "password" or "password" in lowered or "senha" in lowered:
            value = password
        elif "username" in lowered or "usuario" in lowered or "login" in lowered or "cpf" in lowered:
            value = username
        payload[name] = value

    return payload


def _looks_authenticated_html(html: str) -> bool:
    return any(marker in html for marker in ("Página Inicial", "Inscrição Atual", "Caixa de Entrada", "Sair"))


def _looks_login_failed(html: str) -> bool:
    return bool(re.search(r"Dados inv[aá]lidos|Usu[aá]rio ou senha|Invalid username|Invalid password", html, flags=re.I))


def _has_current_inscricao(html: str) -> bool:
    decoded = unescape(html)
    return "Inscrição Atual" in decoded and "Selecione uma inscrição" not in decoded


def _extract_inscricao_rows(html: str) -> list[InscricaoRow]:
    decoded = unescape(html)
    pattern = re.compile(
        r"empresaDataTable:(?P<index>\d+):linkDocumento[^>]*>(?P<documento>.*?)</a>.*?"
        r"empresaDataTable:(?P=index):linkInscricao[^>]*>(?P<inscricao>.*?)</a>.*?"
        r"empresaDataTable:(?P=index):linkNome[^>]*>(?P<nome>.*?)</a>",
        flags=re.I | re.S,
    )
    rows: list[InscricaoRow] = []
    for match in pattern.finditer(decoded):
        rows.append(
            InscricaoRow(
                index=match.group("index"),
                documento=_strip_tags(match.group("documento")),
                inscricao=_strip_tags(match.group("inscricao")),
                nome=_strip_tags(match.group("nome")),
            )
        )
    return rows


def _select_inscricao_row(
    rows: list[InscricaoRow],
    *,
    cnpj: str | None,
    inscricao: str | None,
    nome: str | None,
) -> InscricaoRow:
    if cnpj:
        expected = _digits(cnpj)
        for row in rows:
            if _digits(row.documento) == expected:
                return row

    if inscricao:
        expected = _digits(inscricao)
        for row in rows:
            if _digits(row.inscricao) == expected:
                return row

    if nome:
        expected_name = _normalize_text(nome)
        for row in rows:
            if expected_name in _normalize_text(row.nome):
                return row

    return rows[0]


def _find_inscricao_by_cnpj(rows: list[InscricaoRow], cnpj: str) -> InscricaoRow:
    expected = _digits(cnpj)
    for row in rows:
        if _digits(row.documento) == expected:
            return row
    raise InscricaoNotFoundError(
        f"CNPJ {cnpj} nao encontrado entre as inscricoes disponiveis para o usuario autenticado."
    )


def _validate_exported_inscricao(
    path: Path,
    inscricao: InscricaoRow,
    visao: str = VISAO_PRESTADOR,
) -> None:
    """Confere que o XML pertence à inscrição: prestador (emitidas) ou tomador (recebidas)."""
    expected = _digits(inscricao.documento)
    returned: set[str] = set()
    identification_tag = (
        "IdentificacaoTomador" if visao == VISAO_TOMADOR else "IdentificacaoPrestador"
    )

    for xml_content in _iter_exported_xml(path):
        root = ET.fromstring(xml_content)
        for identification in root.iter():
            if _xml_local_name(identification.tag) != identification_tag:
                continue
            for element in identification.iter():
                if _xml_local_name(element.tag) == "Cnpj" and element.text:
                    value = _digits(element.text)
                    if value:
                        returned.add(value)

    if not returned:
        raise RuntimeError(
            f"XML exportado nao informa o CNPJ do {visao}; carga cancelada."
        )
    if returned != {expected}:
        raise RuntimeError(
            "XML exportado nao pertence a inscricao selecionada; carga cancelada."
        )


def _iter_exported_xml(path: Path) -> Iterator[bytes]:
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                if member.lower().endswith(".xml"):
                    yield archive.read(member)
        return
    yield path.read_bytes()


def _xml_local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _strip_tags(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def _download_prefix(
    competencia: QueryPeriod,
    inscricao: InscricaoRow | None,
    visao: str = VISAO_PRESTADOR,
) -> str:
    tipo = "tomados" if visao == VISAO_TOMADOR else "prestados"
    prefix = f"NF-consulta-servicos-{tipo}-{competencia.yyyymm}"
    if inscricao is None:
        return prefix
    inscricao_id = _digits(inscricao.inscricao) or inscricao.index
    cnpj_id = _digits(inscricao.documento)
    return f"{prefix}-insc-{inscricao_id}-doc-{cnpj_id}"


def _nfse_download_prefix(numero_nfse: str, cnpj: str, inscricao: InscricaoRow | None) -> str:
    prefix = f"NF-consulta-numero-{_digits(numero_nfse)}-doc-{_digits(cnpj)}"
    if inscricao is None:
        return prefix
    inscricao_id = _digits(inscricao.inscricao) or inscricao.index
    return f"{prefix}-insc-{inscricao_id}"


def _with_cid(url: str, source_url: str) -> str:
    cid = parse_qs(urlsplit(source_url).query).get("cid", [None])[0]
    if not cid:
        return url
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    query["cid"] = [cid]
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query, doseq=True), parsed.fragment))


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _extract_view_state(text: str, default: str | None = None) -> str:
    decoded = unescape(text)
    patterns = (
        r'name=["\']javax\.faces\.ViewState["\'][^>]*value=["\']([^"\']+)["\']',
        r'id=["\']javax\.faces\.ViewState["\'][^>]*value=["\']([^"\']+)["\']',
        r"<update[^>]+id=[\"']javax\.faces\.ViewState[\"'][^>]*><!\[CDATA\[(.*?)\]\]></update>",
        r"<update[^>]+id=[\"']javax\.faces\.ViewState[\"'][^>]*>(.*?)</update>",
    )
    for pattern in patterns:
        match = re.search(pattern, decoded, flags=re.I | re.S)
        if match and match.group(1).strip():
            return unescape(match.group(1).strip())
    if default is not None:
        return default
    raise RuntimeError("javax.faces.ViewState nao encontrado na resposta do portal.")


def _extract_xml_row_command_ids(text: str) -> list[str]:
    decoded = unescape(text)
    commands: dict[int, str] = {}
    pattern = re.compile(
        r"jsfcljs\s*\(\s*document\.getElementById\(['\"]consultarnfseForm['\"]\)\s*,\s*"
        r"\{\s*['\"](?P<command>consultarnfseForm:dataTable:(?P<row>\d+):j_id\d+)['\"]"
        r"\s*:\s*['\"](?P=command)['\"]\s*\}",
        flags=re.I,
    )
    anchors = re.findall(r"<a\b[^>]*>", decoded, flags=re.I | re.S)
    for anchor in anchors:
        if not re.search(
            r"\btitle=['\"]Exportar XML['\"]",
            anchor,
            flags=re.I,
        ):
            continue
        match = pattern.search(anchor)
        if match is None:
            continue
        commands[int(match.group("row"))] = match.group("command")
    return [commands[row] for row in sorted(commands)]


_LISTED_COLUMNS = {
    "data de emissao": "data_emissao",
    "numero da nfs-e": "numero_nfse",
    "situacao": "situacao",
    "cpf/cnpj do prestador": "documento",
    "cpf/cnpj do tomador": "documento",
}


def _fold(value: str) -> str:
    text = unicodedata.normalize("NFKD", value)
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def _extract_listed_nfse(text: str, inscricao_documento: str | None = None) -> list[ListedNfse]:
    """Lê número, situação e CPF/CNPJ de cada linha da tabela de resultados.

    As colunas são localizadas pelo título do cabeçalho
    (``dataTable:j_idNNNheader``), não pela posição.
    """
    decoded = unescape(text)
    columns: dict[str, str] = {}
    for jid, label in re.findall(
        r"id=['\"]consultarnfseForm:dataTable:(j_id\d+)header:sortDiv['\"][^>]*>(.*?)</div>",
        decoded,
        flags=re.I | re.S,
    ):
        field = _LISTED_COLUMNS.get(_fold(_strip_tags(label)))
        if field:
            columns[jid] = field
    if "numero_nfse" not in columns.values() or "situacao" not in columns.values():
        return []

    rows: dict[int, dict[str, str]] = {}
    for row, jid, value in re.findall(
        r"<td\b[^>]*\bid=['\"]consultarnfseForm:dataTable:(\d+):(j_id\d+)['\"][^>]*>(.*?)</td>",
        decoded,
        flags=re.I | re.S,
    ):
        field = columns.get(jid)
        if field:
            rows.setdefault(int(row), {})[field] = " ".join(_strip_tags(value).split())

    listed: list[ListedNfse] = []
    for _, cells in sorted(rows.items()):
        numero = cells.get("numero_nfse", "").strip()
        situacao = cells.get("situacao", "").strip().upper()
        if not numero or not situacao:
            continue
        try:
            emissao = datetime.strptime(cells.get("data_emissao", ""), "%d/%m/%Y").date()
        except ValueError:
            emissao = None
        listed.append(
            ListedNfse(
                numero_nfse=numero,
                documento=_digits(cells.get("documento", "")),
                situacao=situacao,
                data_emissao=emissao,
                inscricao_documento=inscricao_documento,
            )
        )
    return listed


def _result_page_fingerprint(text: str, view_state: str) -> str:
    decoded = unescape(text)
    if view_state:
        decoded = decoded.replace(view_state, "")
    return sha256(decoded.encode("utf-8")).hexdigest()


def _extract_enabled_next_page_command_id(text: str) -> str | None:
    decoded = unescape(text)
    match = re.search(
        r"new\s+Richfaces\.Datascroller\(\s*['\"]"
        r"(consultarnfseForm:dataTable:j_id\d+)['\"]",
        decoded,
        flags=re.I,
    )
    if match is None:
        return None
    command_id = match.group(1)
    table = re.search(
        rf"<table\b[^>]*\bid=['\"]{re.escape(command_id)}_table['\"][^>]*>.*?</table>",
        decoded,
        flags=re.I | re.S,
    )
    scroller_html = table.group(0) if table is not None else decoded
    if re.search(
        r"rich-datascr-button-dsbld[^>]*>\s*(?:&raquo;|»|>>)",
        scroller_html,
        flags=re.I,
    ):
        return None
    if not re.search(r"(?:&raquo;|»|>>)", scroller_html):
        return None
    return command_id


def _looks_like_html(content: bytes) -> bool:
    return content.lstrip()[:100].lower().startswith((b"<!doctype html", b"<html"))


def _filename_from_content_disposition(value: str) -> str | None:
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', value, flags=re.I)
    if not match:
        return None
    return Path(match.group(1)).name
