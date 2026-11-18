from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from nfs_fortaleza import extraction
from nfs_fortaleza.config import Settings
from nfs_fortaleza.extraction import ExtractionPayload, extract_and_load_nfse
from nfs_fortaleza.periods import MonthPeriod
from nfs_fortaleza.portal import (
    VISAO_PRESTADOR,
    VISAO_TOMADOR,
    ListedNfse,
    PortalOptions,
    _extract_listed_nfse,
)


def _header(jid: str, label: str) -> str:
    return (
        f'<th class="rich-table-subheadercell" scope="col" id="consultarnfseForm:dataTable:{jid}header">'
        f'<div id="consultarnfseForm:dataTable:{jid}header:sortDiv">{label}</div></th>'
    )


def _cell(row: int, jid: str, value: str) -> str:
    return f'<td class="rich-table-cell" id="consultarnfseForm:dataTable:{row}:{jid}">{value}</td>'


def _page(rows: list[tuple[str, str, str, str]], documento_label: str = "CPF/CNPJ do Prestador") -> str:
    # Colunas fora da ordem do portal: o parser deve usar o cabeçalho.
    head = "".join([
        _header("j_id333", ""),
        _header("j_id345", "Situa&ccedil;&atilde;o"),
        _header("j_id339", "Data de Emissão"),
        _header("j_id350", documento_label),
        _header("j_id342", "Número da NFS-e"),
        _header("j_id353", "Nome Prestador"),
    ])
    body = ""
    for i, (emissao, numero, situacao, documento) in enumerate(rows):
        body += (
            '<tr class="rich-table-row">'
            + _cell(i, "j_id333", '<a href="#" title="Exportar XML"><i></i></a>')
            + _cell(i, "j_id345", situacao)
            + _cell(i, "j_id339", emissao)
            + _cell(i, "j_id350", documento)
            + _cell(i, "j_id342", numero)
            + _cell(i, "j_id353", "FORNECEDOR LTDA")
            + "</tr>"
        )
    return (
        '<table class="rich-table" id="consultarnfseForm:dataTable"><thead><tr>'
        f"{head}</tr></thead><tbody id=\"consultarnfseForm:dataTable:tb\">{body}</tbody></table>"
    )


class ExtractListedNfseTests(unittest.TestCase):
    def test_le_situacao_numero_e_cnpj_pelo_cabecalho(self) -> None:
        html = _page([
            ("09/11/2026", "388581", "CANCELADA", "07.530.025/0001-46"),
            ("01/11/2026", "23275", "NORMAL", "09.618.107/0001-72"),
        ])

        listed = _extract_listed_nfse(html, "08711085000128")

        self.assertEqual(listed, [
            ListedNfse("388581", "07530025000146", "CANCELADA", date(2026, 11, 9), "08711085000128"),
            ListedNfse("23275", "09618107000172", "NORMAL", date(2026, 11, 1), "08711085000128"),
        ])

    def test_pagina_sem_coluna_situacao_nao_inventa_status(self) -> None:
        html = _page([("09/11/2026", "1", "NORMAL", "07.530.025/0001-46")]).replace("Situa&ccedil;&atilde;o", "Outra")

        self.assertEqual(_extract_listed_nfse(html), [])

    def test_data_invalida_vira_none(self) -> None:
        listed = _extract_listed_nfse(_page([("", "10", "normal", "07.530.025/0001-46")]))

        self.assertEqual(listed[0].situacao, "NORMAL")
        self.assertIsNone(listed[0].data_emissao)


def _settings() -> Settings:
    settings = mock.Mock(spec=Settings)
    settings.database_url = "postgresql://unused"
    settings.postgres_schema = "api_prontocardio"
    return settings


class ExtractionGravaSituacaoTests(unittest.TestCase):
    def _run(self, visao: str) -> tuple[mock.Mock, dict]:
        listed = [ListedNfse("388581", "07530025000146", "CANCELADA", date(2026, 11, 9), "08711085000128")]

        class FakeClient:
            def __init__(self, settings, options):
                self.listed_nfse = []

            def export_competencia(self, period):
                self.listed_nfse.extend(listed)
                return [Path("/tmp/nao-existe.xml")]

        payload = ExtractionPayload.from_mapping({"competencia": "11/2026"}, default_date=date(2026, 11, 18))
        with mock.patch.object(extraction, "PortalClient", FakeClient), \
                mock.patch.object(extraction, "load_nfse_xml"), \
                mock.patch.object(extraction, "upsert_nfse_situacao", return_value=1) as upsert:
            summary = extract_and_load_nfse(
                _settings(), payload,
                options=PortalOptions(query_date=date(2026, 11, 18), visao=visao),
                table_name="nfse_xml_tomador",
            )
        return upsert, summary.as_dict()

    def test_visao_tomador_grava_situacoes(self) -> None:
        upsert, resumo = self._run(VISAO_TOMADOR)

        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["table_name"], "nfse_xml_tomador")
        self.assertEqual(upsert.call_args.args[1][0].situacao, "CANCELADA")
        self.assertEqual(resumo["situacoes_atualizadas"], 1)

    def test_visao_prestador_nao_grava(self) -> None:
        upsert, resumo = self._run(VISAO_PRESTADOR)

        upsert.assert_not_called()
        self.assertEqual(resumo["situacoes_atualizadas"], 0)


if __name__ == "__main__":
    unittest.main()
