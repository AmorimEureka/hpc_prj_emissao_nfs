"""Página "Notas Fiscais" do portal do credenciado ISSEC (APEX 22.2, página 44).

Duas regiões:

- "Notas Fiscais - Pendentes" (relatório clássico): NUP, processo, valor e a
  coluna "Enviar". O portal explica: "Na coluna enviar haverá um texto com
  ENVIAR que levará a onde enviar a nota" — com o texto ENVIAR o convênio já
  pediu a NF e ela ainda não foi anexada; sem o texto a NF já foi enviada.
  Os processos agrupados têm o link ANALISAR (Detalhes da Nota).
- "Notas Fiscais - Pagas" (relatório interativo, 50 por página): processo,
  NUP, valor pago (líquido) e tipo (AGRUPADOR/NORMAL). O portal não informa a
  data do pagamento.

"Detalhes da Nota" (detalhes-da-nota?nu_grupo=NUP, relatório clássico de 15
por página) lista os processos de um grupo. Só leitura: nada é enviado ao
portal além da navegação e da paginação.
"""

from __future__ import annotations

import html as html_lib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from nfs_fortaleza.issec_portal import (
    IssecPortalError,
    extract_dialog_path,
    normalize_label,
    parse_brl,
    parse_html,
)

NOTAS_FISCAIS_PATH = "r/aplicacoes/portal-credenciado/44-notas-fiscais"
DETALHE_PAGE_SIZE = 15
MAX_PAGINAS = 500

_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r'<td[^>]*headers="([^"]+)"[^>]*>(.*?)</td>', re.S)
_TAG = re.compile(r"<[^>]+>")
_IR_HEADER = re.compile(r'<th class="a-IRR-header[^"]*" id="([^"]+)"\s*>(.*?)</th>', re.S)
_IR_NEXT = re.compile(r'title="Next"[^>]*data-pagination="([^"]+)"')
_IR_INIT = re.compile(r'interactiveReport\(\{"regionId":"(R\d+)".*?"ajaxIdentifier":"([^"]+)"', re.S)
_REPORT_INIT = re.compile(r'apex\.widget\.report\.init\("(R\d+)","([^"]+)"')


def _texto(fragmento: str) -> str:
    return " ".join(html_lib.unescape(_TAG.sub(" ", fragmento)).split())


def _digitos(valor: str) -> str:
    return re.sub(r"\D", "", valor or "")


def _celulas(linha: str) -> dict[str, str]:
    return {cabecalho: conteudo for cabecalho, conteudo in _TD.findall(linha)}


def _caminho_detalhe(fragmento: str) -> str | None:
    fragmento = html_lib.unescape(fragmento)
    if "detalhes-da-nota" not in fragmento:
        return None
    return extract_dialog_path(fragmento, "detalhes-da-nota")


def parse_notas_pendentes(html: str) -> list[dict]:
    """Linhas de "Notas Fiscais - Pendentes" (cabeçalhos NU_NUP/NU_ORDEM/NU_VALOR)."""
    linhas = []
    for linha in _TR.findall(html):
        celulas = _celulas(linha)
        if "NU_NUP" not in celulas or "NU_ORDEM" not in celulas:
            continue
        enviar = _texto(celulas.get("DERIVED$01", ""))
        # O portal deixa o link do detalhe na célula mesmo sem processos
        # agrupados; agrupado é só a linha com o texto ANALISAR (27/11/2026:
        # 2600044480 e 2600041559 vinham marcados como agrupados).
        coluna_grupo = celulas.get("DERIVED$03", "")
        detalhe = (
            _caminho_detalhe(coluna_grupo)
            if "ANALISAR" in _texto(coluna_grupo).upper()
            else None
        )
        linhas.append({
            "nup": _digitos(_texto(celulas["NU_NUP"])),
            "processo": _digitos(_texto(celulas["NU_ORDEM"])),
            "valor": parse_brl(_texto(celulas.get("NU_VALOR", ""))),
            "nf_a_enviar": "ENVIAR" in enviar.upper(),
            "agrupado": detalhe is not None,
            "detalhe_path": detalhe,
        })
    return linhas


_COLUNAS_PAGAS = {
    "processo": "processo",
    "nup": "nup",
    "valor pago": "valor_pago",
    "tipo processo": "tipo",
}


def colunas_das_pagas(html: str) -> dict[str, str]:
    """id do cabeçalho do relatório interativo -> campo (pelo rótulo da coluna)."""
    colunas = {}
    for coluna_id, rotulo in _IR_HEADER.findall(html):
        campo = _COLUNAS_PAGAS.get(normalize_label(_texto(rotulo)))
        if campo:
            colunas[coluna_id] = campo
    return colunas


def parse_notas_pagas(html: str, colunas: dict[str, str]) -> list[dict]:
    linhas = []
    for linha in _TR.findall(html):
        dados = {}
        detalhe = None
        for cabecalho, conteudo in _TD.findall(linha):
            campo = colunas.get(cabecalho)
            if campo:
                dados[campo] = _texto(conteudo)
                if campo == "tipo":
                    detalhe = _caminho_detalhe(conteudo)
        if not dados.get("processo") or not dados.get("nup"):
            continue
        tipo = (dados.get("tipo") or "").upper()
        linhas.append({
            "processo": _digitos(dados["processo"]),
            "nup": _digitos(dados["nup"]),
            "valor_pago": parse_brl(dados.get("valor_pago", "")),
            "tipo": tipo,
            "detalhe_path": detalhe if tipo == "AGRUPADOR" else None,
        })
    return linhas


def parse_detalhe_nota(html: str) -> list[dict]:
    """Processos de um grupo em "Detalhes da Nota" (NU_ORDEM/NU_NUP/NU_VALOR)."""
    membros = []
    for linha in _TR.findall(html):
        celulas = _celulas(linha)
        processo = _digitos(_texto(celulas.get("NU_ORDEM", "")))
        if not processo:
            continue
        membros.append({
            "processo": processo,
            "nup": _digitos(_texto(celulas.get("NU_NUP", ""))),
            "valor": parse_brl(_texto(celulas.get("NU_VALOR", ""))),
        })
    return membros


def _nup_do_caminho(caminho: str) -> str:
    encontrado = re.search(r"[?&]nu_grupo=(\d+)", caminho)
    return encontrado.group(1) if encontrado else ""


class NotasFiscaisIssec:
    """Lê a página Notas Fiscais com a sessão já autenticada do cliente."""

    def __init__(self, client: Any, *, max_workers: int = 4) -> None:
        self.client = client
        self.max_workers = max_workers

    # ------------------------------------------------------------ apoio
    def _url(self, caminho: str) -> str:
        return urljoin(self.client.base_url, caminho)

    def _ajax(self, contexto: str, referer: str, data: dict) -> str:
        response = self.client.session.post(
            self._url(f"wwv_flow.ajax?p_context={contexto}"),
            data=data,
            headers={"Referer": referer, "X-Requested-With": "XMLHttpRequest"},
            timeout=self.client.timeout,
        )
        response.raise_for_status()
        return response.text

    @staticmethod
    def _base(page) -> dict:
        return {
            "p_flow_id": page.values_by_id["pFlowId"],
            "p_flow_step_id": page.values_by_id["pFlowStepId"],
            "p_instance": page.values_by_id["pInstance"],
            "p_debug": "",
        }

    # ------------------------------------------------------------ página 44
    def ler(self, grupos_pagos_conhecidos: set[str] = frozenset()) -> dict:
        """Pendentes, pagas e processos dos grupos (pendentes + pagos novos)."""
        if not self.client.session_id:
            raise IssecPortalError("Sessão do portal ISSEC não autenticada.")
        caminho = f"{NOTAS_FISCAIS_PATH}?session={self.client.session_id}"
        url = self._url(caminho)
        html = self.client._get(caminho).text
        if "NU_NUP" not in html and "a-IRR-table" not in html:
            raise IssecPortalError("Página Notas Fiscais sem os relatórios esperados.")
        pendentes = parse_notas_pendentes(html)
        pagas = self._todas_as_pagas(html, url)
        grupos = {
            _nup_do_caminho(p["detalhe_path"]): p["detalhe_path"]
            for p in pendentes if p["detalhe_path"]
        }
        for paga in pagas:
            if paga["detalhe_path"] and paga["nup"] not in grupos_pagos_conhecidos:
                grupos.setdefault(_nup_do_caminho(paga["detalhe_path"]) or paga["nup"], paga["detalhe_path"])
        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            membros_por_grupo = dict(zip(grupos, executor.map(self._membros, grupos.values())))
        pagos = {p["nup"] for p in pagas}
        extraido_em = datetime.now().astimezone().isoformat()
        processos = [
            {**membro, "nup_grupo": nup_grupo,
             "situacao": "paga" if nup_grupo in pagos else "pendente",
             "extraido_em": extraido_em}
            for nup_grupo, membros in membros_por_grupo.items()
            for membro in membros
        ]
        for linha in pendentes + pagas:
            linha.pop("detalhe_path", None)
            linha["extraido_em"] = extraido_em
        return {"pendentes": pendentes, "pagas": pagas, "processos": processos}

    def _todas_as_pagas(self, html: str, url: str) -> list[dict]:
        colunas = colunas_das_pagas(html)
        if set(colunas.values()) != set(_COLUNAS_PAGAS.values()):
            raise IssecPortalError("Colunas de 'Notas Fiscais - Pagas' mudaram no portal.")
        pagas = parse_notas_pagas(html, colunas)
        init = _IR_INIT.search(html)
        proxima = _IR_NEXT.search(html)
        if not init or not proxima:
            return pagas
        regiao, ajax_id = init.group(1), init.group(2).replace("\\u002F", "/")
        page = parse_html(html)
        worksheet = page.values_by_id[f"{regiao}_worksheet_id"]
        relatorio = page.values_by_id[f"{regiao}_report_id"]
        vistos = {(p["processo"], p["nup"]) for p in pagas}
        for _ in range(MAX_PAGINAS):
            fragmento = self._ajax(
                f"portal-credenciado/44-notas-fiscais/{self.client.session_id}",
                url,
                {
                    **self._base(page),
                    "p_request": f"PLUGIN={ajax_id}",
                    "p_widget_name": "worksheet",
                    "p_widget_mod": "ACTION",
                    "p_widget_action": "PAGE",
                    "p_widget_action_mod": html_lib.unescape(proxima.group(1)),
                    "p_widget_num_return": "50",
                    "x01": worksheet,
                    "x02": relatorio,
                    "p_json": json.dumps({"pageItems": None, "salt": page.values_by_id["pSalt"]}),
                },
            )
            novas = [p for p in parse_notas_pagas(fragmento, colunas) if (p["processo"], p["nup"]) not in vistos]
            pagas.extend(novas)
            vistos.update((p["processo"], p["nup"]) for p in novas)
            proxima = _IR_NEXT.search(fragmento)
            if not proxima or not novas:
                break
        return pagas

    def _membros(self, caminho: str) -> list[dict]:
        """Processos do grupo, percorrendo as páginas de 15 do relatório clássico."""
        caminho = caminho.lstrip("/").removeprefix("ords/")
        url = self._url(caminho)
        html = self.client._get(caminho).text
        membros = parse_detalhe_nota(html)
        init = _REPORT_INIT.search(html)
        if not init or len(membros) < DETALHE_PAGE_SIZE:
            return membros
        regiao, ajax_id = init.group(1), init.group(2).replace("\\u002F", "/")
        page = parse_html(html)
        vistos = {m["processo"] for m in membros}
        primeira = DETALHE_PAGE_SIZE + 1
        for _ in range(MAX_PAGINAS):
            fragmento = self._ajax(
                f"portal-credenciado/detalhes-da-nota/{self.client.session_id}",
                url,
                {
                    **self._base(page),
                    "p_request": f"PLUGIN={ajax_id}",
                    "p_widget_action": "paginate",
                    "x01": regiao[1:],
                    "p_pg_min_row": str(primeira),
                    "p_pg_max_rows": str(DETALHE_PAGE_SIZE),
                    "p_pg_rows_fetched": str(DETALHE_PAGE_SIZE),
                    "p_json": json.dumps({"salt": page.values_by_id["pSalt"]}, separators=(",", ":")),
                },
            )
            novos = [m for m in parse_detalhe_nota(fragmento) if m["processo"] not in vistos]
            membros.extend(novos)
            vistos.update(m["processo"] for m in novos)
            if len(novos) < DETALHE_PAGE_SIZE:
                break
            primeira += DETALHE_PAGE_SIZE
        return membros
