from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

from nfs_fortaleza.config import Settings
from nfs_fortaleza.extraction import (
    ExtractionConfigurationError,
    ExtractionPayload,
    extract_and_load_nfse,
)
from nfs_fortaleza.periods import MonthPeriod
from nfs_fortaleza.portal import (
    TOMADOR_CONSULTAR_EVENT,
    TOMADOR_PERIODO_TAB,
    VISAO_TOMADOR,
    InscricaoRow,
    PortalClient,
    PortalOptions,
    _download_prefix,
    _validate_exported_inscricao,
)


SETTINGS = Settings(
    portal_url="https://example.test/grpfor/home.seam",
    cpf_login="00000000000",
    senha="secret",
    database_url="postgresql://example.test/db",
    postgres_schema="test",
)


def _nota(prestador: str, tomador: str) -> str:
    return f"""
    <NFSE>
      <Nfse>
        <InfNfse>
          <PrestadorServico>
            <IdentificacaoPrestador><Cnpj>{prestador}</Cnpj></IdentificacaoPrestador>
          </PrestadorServico>
          <TomadorServico>
            <IdentificacaoTomador><CpfCnpj><Cnpj>{tomador}</Cnpj></CpfCnpj></IdentificacaoTomador>
          </TomadorServico>
        </InfNfse>
      </Nfse>
    </NFSE>
    """


class PrestadorPayloadUnchangedTests(unittest.TestCase):
    """A visão padrão (DAG extracao_nfse em produção) não pode mudar."""

    def test_prestador_steps_are_the_original_sequence(self) -> None:
        steps = PortalClient(SETTINGS)._period_tab_steps()

        self.assertEqual(
            steps,
            [
                {
                    "consultarnfseForm:opTipoRelatorio": "1",
                    "consultarnfseForm:numNfse": "",
                    "consultarnfseForm:j_id170": "consultarnfseForm:j_id170",
                },
                {
                    "consultarnfseForm:opTipoRelatorio": "1",
                    "consultarnfseForm:numNfse": "",
                    "ajaxSingle": "consultarnfseForm:periodo_prestador_tab",
                    "consultarnfseForm:j_id191": "consultarnfseForm:j_id191",
                },
                {
                    "consultarnfseForm:opTipoRelatorio": "1",
                    "consultarnfseForm:numNfse": "",
                    "consultarnfseForm:periodo_prestador_tab": "consultarnfseForm:periodo_prestador_tab",
                },
            ],
        )

    def test_prestador_query_payload_keeps_original_button(self) -> None:
        payload = PortalClient(SETTINGS)._query_payload(MonthPeriod(2026, 11), "state")

        self.assertEqual(payload["consultarnfseForm:opTipoRelatorio"], "1")
        self.assertIn("consultarnfseForm:j_id237", payload)
        self.assertIn("consultarnfseForm:dataInicialInputDate", payload)

    def test_prestador_download_prefix_is_unchanged(self) -> None:
        self.assertEqual(
            _download_prefix(MonthPeriod(2026, 11), None),
            "NF-consulta-servicos-prestados-202611",
        )


class TomadorPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = PortalClient(
            SETTINGS,
            PortalOptions(query_date=date(2026, 11, 16), visao=VISAO_TOMADOR),
        )

    def test_steps_select_servicos_tomados_and_period_tab(self) -> None:
        steps = self.client._period_tab_steps()

        self.assertTrue(all(s["consultarnfseForm:opTipoRelatorio"] == "2" for s in steps))
        self.assertEqual(
            steps[0]["consultarnfseForm:paramSelectedTabTomador"],
            "abaPorPeriodoTomador_tab",
        )
        self.assertEqual(steps[1]["ajaxSingle"], TOMADOR_PERIODO_TAB)
        self.assertIn(TOMADOR_PERIODO_TAB, steps[2])

    def test_query_payload_uses_tomador_dates_and_consultar_button(self) -> None:
        payload = self.client._query_payload(MonthPeriod(2026, 11), "state")

        self.assertEqual(payload["consultarnfseForm:opTipoRelatorio"], "2")
        self.assertEqual(payload["consultarnfseForm:dataInicialTomadorInputDate"], "01/11/2026")
        self.assertEqual(payload["consultarnfseForm:dataFinalTomadorInputDate"], "16/11/2026")
        self.assertEqual(payload["consultarnfseForm:opPrestador"], "2")
        self.assertIn(TOMADOR_CONSULTAR_EVENT, payload)
        self.assertNotIn("consultarnfseForm:dataInicialInputDate", payload)

    def test_form_payload_for_download_has_no_action(self) -> None:
        payload = self.client._form_payload(MonthPeriod(2026, 11), "state")

        self.assertEqual(payload["consultarnfseForm:opTipoRelatorio"], "2")
        self.assertFalse(any(k.startswith("consultarnfseForm:j_id") for k in payload))

    def test_download_prefix_marks_tomados(self) -> None:
        row = InscricaoRow("0", "05.613.278/0001-58", "123", "Empresa")
        self.assertEqual(
            _download_prefix(MonthPeriod(2026, 11), row, VISAO_TOMADOR),
            "NF-consulta-servicos-tomados-202611-insc-123-doc-05613278000158",
        )

    def test_invalid_visao_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            PortalOptions(visao="intermediario")


class TomadorValidationTests(unittest.TestCase):
    row = InscricaoRow("0", "11.111.111/0001-11", "100", "Hospital")

    def _write(self, directory: str, content: str) -> Path:
        path = Path(directory) / "nota.xml"
        path.write_text(content, encoding="utf-8")
        return path

    def test_accepts_received_note_whose_tomador_is_the_inscricao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, _nota("99999999000199", "11111111000111"))
            _validate_exported_inscricao(path, self.row, VISAO_TOMADOR)

    def test_rejects_received_note_of_another_tomador(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, _nota("11111111000111", "22222222000122"))
            with self.assertRaisesRegex(RuntimeError, "nao pertence a inscricao"):
                _validate_exported_inscricao(path, self.row, VISAO_TOMADOR)

    def test_prestador_validation_still_checks_prestador(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._write(directory, _nota("11111111000111", "22222222000122"))
            _validate_exported_inscricao(path, self.row)


class TomadorExtractionTests(unittest.TestCase):
    def test_loads_into_given_table(self) -> None:
        options = PortalOptions(visao=VISAO_TOMADOR)
        files = [Path("/tmp/a.xml"), Path("/tmp/b.xml")]
        with patch("nfs_fortaleza.extraction.PortalClient") as client_cls, patch(
            "nfs_fortaleza.extraction.load_nfse_xml"
        ) as load:
            client_cls.return_value.export_competencia.return_value = files
            summary = extract_and_load_nfse(
                SETTINGS,
                ExtractionPayload(periods=(MonthPeriod(2026, 11),)),
                options=options,
                table_name="nfse_xml_tomador",
            )

        self.assertEqual(summary.loaded_files, 2)
        for call_args in load.call_args_list:
            self.assertEqual(call_args.kwargs["table_name"], "nfse_xml_tomador")

    def test_number_query_is_rejected_for_tomador(self) -> None:
        with patch("nfs_fortaleza.extraction.PortalClient", Mock()):
            with self.assertRaises(ExtractionConfigurationError):
                extract_and_load_nfse(
                    SETTINGS,
                    ExtractionPayload(cnpj="11111111000111", numero_nfse="1"),
                    options=PortalOptions(visao=VISAO_TOMADOR),
                )


if __name__ == "__main__":
    unittest.main()
