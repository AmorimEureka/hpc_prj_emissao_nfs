from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import Mock, call, patch

from nfs_fortaleza.config import Settings
from nfs_fortaleza.periods import MonthPeriod
from nfs_fortaleza.portal import (
    InscricaoRow,
    PeriodWithoutInvoicesError,
    PortalClient,
    PortalOptions,
    _extract_enabled_next_page_command_id,
    _extract_xml_row_command_ids,
    _result_page_fingerprint,
    _validate_exported_inscricao,
)


class ExportCompetenciaInscricoesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            portal_url="https://example.test/grpfor/home.seam",
            cpf_login="00000000000",
            senha="secret",
            database_url="postgresql://example.test/db",
            postgres_schema="test",
        )

    def test_uses_a_fresh_session_for_each_additional_inscricao(self) -> None:
        first = InscricaoRow("0", "11.111.111/0001-11", "100", "Empresa A")
        second = InscricaoRow("1", "22.222.222/0001-22", "200", "Empresa B")
        first_session = Mock(name="first_session")
        second_session = Mock(name="second_session")
        home = Mock(url="https://example.test/grpfor/home.seam")

        with tempfile.TemporaryDirectory() as directory:
            client = PortalClient(
                self.settings,
                PortalOptions(
                    downloads_dir=Path(directory),
                    artifacts_dir=Path(directory),
                ),
            )
            first_export = Path(directory) / "first.xml"
            second_export = Path(directory) / "second.xml"

            with (
                patch.object(
                    client,
                    "_login_dlt_session",
                    side_effect=(first_session, second_session),
                ) as login,
                patch.object(
                    client,
                    "_available_inscricoes",
                    side_effect=([first, second], [first, second]),
                ) as available,
                patch.object(client, "_request_get", return_value=home),
                patch.object(client, "_select_inscricao") as select,
                patch.object(
                    client,
                    "_export_competencia_with_requests",
                    side_effect=(first_export, second_export),
                ) as export,
                patch(
                    "nfs_fortaleza.portal._validate_exported_inscricao"
                ) as validate,
            ):
                result = client.export_competencia(
                    MonthPeriod(year=2026, month=1)
                )

        self.assertEqual(result, [first_export, second_export])
        self.assertEqual(login.call_count, 2)
        self.assertEqual(available.call_count, 2)
        select.assert_has_calls(
            [call(first_session, home, first), call(second_session, home, second)]
        )
        export.assert_has_calls(
            [
                call(first_session, MonthPeriod(year=2026, month=1), first),
                call(second_session, MonthPeriod(year=2026, month=1), second),
            ]
        )
        validate.assert_has_calls(
            [
                call(first_export, first, "prestador"),
                call(second_export, second, "prestador"),
            ]
        )


    def test_processes_next_page_when_html_row_indexes_repeat(self) -> None:
        row = InscricaoRow("0", "11.111.111/0001-11", "100", "Empresa A")
        client = PortalClient(self.settings)
        session = Mock(name="session")
        query = Mock(
            url="https://example.test/grpfor/pages/nfse/consulta.seam",
            text="Consultar NFS-e",
        )
        first_page = _xml_export_link("Nota A", "j_id374")
        second_page = _xml_export_link("Nota B", "j_id376")
        query_result = Mock(text=first_page)
        next_result = Mock(text=second_page)

        def fake_download(
            _session,
            _url,
            _view_state,
            _row_index,
            fallback_name,
            _form_payload,
        ):
            return Path(fallback_name)

        with (
            patch.object(client, "_open_nfse_query", return_value=query),
            patch.object(client, "_ajax_post", return_value="state"),
            patch.object(client, "_request_post", return_value=query_result),
            patch.object(
                client,
                "_request_next_page",
                side_effect=(next_result, None),
            ) as next_page,
            patch.object(
                client,
                "_download_xml_with_requests",
                side_effect=fake_download,
            ) as download,
            patch.object(
                client,
                "_zip_downloads",
                return_value=Path("resultado.zip"),
            ),
            patch(
                "nfs_fortaleza.portal._extract_view_state",
                return_value="state",
            ),
        ):
            result = client._export_competencia_with_requests(
                session,
                MonthPeriod(year=2026, month=2),
                row,
            )

        self.assertEqual(result, Path("resultado.zip"))
        self.assertEqual(download.call_count, 2)
        self.assertEqual(next_page.call_count, 2)

    def test_treats_result_without_xml_as_period_without_invoices(self) -> None:
        response = Mock(text="<span id='ajax-view-state'></span>")

        with tempfile.TemporaryDirectory() as directory:
            client = PortalClient(
                self.settings,
                PortalOptions(
                    downloads_dir=Path(directory),
                    artifacts_dir=Path(directory),
                ),
            )

            with (
                patch.object(
                    client,
                    "_open_nfse_query",
                    return_value=Mock(
                        url="https://example.test/grpfor/consulta.seam",
                        text="Consultar NFS-e state",
                    ),
                ),
                patch.object(client, "_ajax_post", return_value="state"),
                patch.object(client, "_request_post", return_value=response),
                patch(
                    "nfs_fortaleza.portal._extract_view_state",
                    return_value="state",
                ),
                self.assertRaises(PeriodWithoutInvoicesError),
            ):
                client._export_competencia_with_requests(
                    Mock(),
                    MonthPeriod(year=2026, month=9),
                )

            artifacts = list(Path(directory).glob("consulta_sem_xml_*.html"))

        self.assertEqual(len(artifacts), 1)


class PortalSessionTlsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = Settings(
            portal_url="https://example.test/grpfor/home.seam",
            cpf_login="00000000000",
            senha="secret",
            database_url="postgresql://example.test/db",
            postgres_schema="test",
        )

    def test_configures_custom_ca_bundle_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            ca_bundle = Path(directory) / "iss-ca-bundle.pem"
            ca_bundle.write_text("test bundle", encoding="utf-8")
            session = Mock()

            with patch("nfs_fortaleza.portal.Session", return_value=session):
                result = PortalClient(
                    self.settings,
                    PortalOptions(ca_bundle=ca_bundle),
                )._new_session()

        self.assertIs(result, session)
        self.assertEqual(session.verify, str(ca_bundle.resolve()))

    def test_keeps_default_tls_configuration_without_custom_bundle(self) -> None:
        session = Mock()

        with patch("nfs_fortaleza.portal.Session", return_value=session):
            result = PortalClient(self.settings)._new_session()

        self.assertIs(result, session)
        self.assertNotIn("verify", session.__dict__)

    def test_rejects_missing_custom_ca_bundle(self) -> None:
        missing = Path("/tmp/missing-iss-ca-bundle.pem")

        with (
            patch("nfs_fortaleza.portal.Session"),
            self.assertRaisesRegex(RuntimeError, "Bundle TLS.*nao encontrado"),
        ):
            PortalClient(
                self.settings,
                PortalOptions(ca_bundle=missing),
            )._new_session()


class ValidateExportedInscricaoTests(unittest.TestCase):
    def test_accepts_xml_from_selected_inscricao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nota.xml"
            path.write_text(_xml("11111111000111"), encoding="utf-8")

            _validate_exported_inscricao(
                path,
                InscricaoRow("0", "11.111.111/0001-11", "100", "Empresa A"),
            )

    def test_rejects_xml_from_another_inscricao(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nota.xml"
            path.write_text(_xml("22222222000122"), encoding="utf-8")

            with self.assertRaisesRegex(
                RuntimeError,
                "nao pertence a inscricao selecionada",
            ):
                _validate_exported_inscricao(
                    path,
                    InscricaoRow(
                        "0",
                        "11.111.111/0001-11",
                        "100",
                        "Empresa A",
                    ),
                )


class ResultPageFingerprintTests(unittest.TestCase):
    def test_ignores_view_state_but_not_page_content(self) -> None:
        first = _result_page_fingerprint("nota A state-1", "state-1")
        same_page = _result_page_fingerprint("nota A state-2", "state-2")
        next_page = _result_page_fingerprint("nota B state-3", "state-3")

        self.assertEqual(first, same_page)
        self.assertNotEqual(first, next_page)


class DynamicCommandIdTests(unittest.TestCase):
    def test_extracts_xml_command_from_jsf_link_instead_of_table_cell(self) -> None:
        html = """
        <td id="consultarnfseForm:dataTable:0:j_id374">
          <a title="Exportar XML" onclick="if(typeof jsfcljs == 'function'){
            jsfcljs(document.getElementById('consultarnfseForm'),
            {'consultarnfseForm:dataTable:0:j_id376':
             'consultarnfseForm:dataTable:0:j_id376'},'');
          }return false"></a>
        </td>
        <a title="Visualizar" onclick="jsfcljs(
          document.getElementById('consultarnfseForm'),
          {'consultarnfseForm:dataTable:0:j_id380':
           'consultarnfseForm:dataTable:0:j_id380'},'');"></a>
        """

        self.assertEqual(
            _extract_xml_row_command_ids(html),
            ["consultarnfseForm:dataTable:0:j_id376"],
        )

    def test_extracts_current_enabled_datascroller_command(self) -> None:
        html = """
        <td class="rich-datascr-button"><a>&raquo;</a></td>
        <script>
          new Richfaces.Datascroller('consultarnfseForm:dataTable:j_id378',
            function(event) {});
        </script>
        """

        self.assertEqual(
            _extract_enabled_next_page_command_id(html),
            "consultarnfseForm:dataTable:j_id378",
        )

    def test_ignores_disabled_next_page_command(self) -> None:
        html = """
        <td class="rich-datascr-button-dsbld rich-datascr-button">&raquo;</td>
        <script>
          new Richfaces.Datascroller('consultarnfseForm:dataTable:j_id378',
            function(event) {});
        </script>
        """

        self.assertIsNone(_extract_enabled_next_page_command_id(html))


class ExportFormPayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        settings = Settings(
            portal_url="https://example.test/grpfor/home.seam",
            cpf_login="00000000000",
            senha="secret",
            database_url="postgresql://example.test/db",
            postgres_schema="test",
        )
        self.client = PortalClient(settings)

    def test_period_payload_does_not_submit_unrelated_actions(self) -> None:
        payload = self.client._form_payload(
            MonthPeriod(year=2026, month=8),
            "state",
        )

        self.assertFalse(
            any(key.startswith("consultarnfseForm:j_id") for key in payload)
        )

    def test_number_payload_does_not_submit_unrelated_actions(self) -> None:
        payload = self.client._number_form_payload("123", "state")

        self.assertFalse(
            any(key.startswith("consultarnfseForm:j_id") for key in payload)
        )

    def test_period_payload_uses_fortaleza_query_date(self) -> None:
        client = PortalClient(
            self.client.settings,
            PortalOptions(query_date=date(2026, 8, 8)),
        )

        payload = client._form_payload(
            MonthPeriod(year=2026, month=8),
            "state",
        )

        self.assertEqual(
            payload["consultarnfseForm:dataFinalInputDate"],
            "08/08/2026",
        )
        self.assertEqual(
            payload["consultarnfseForm:dataFinalInputCurrentDate"],
            "08/2026",
        )


def _xml(cnpj: str) -> str:
    return f"""
    <CompNfse>
      <Nfse>
        <InfNfse>
          <PrestadorServico>
            <IdentificacaoPrestador><Cnpj>{cnpj}</Cnpj></IdentificacaoPrestador>
          </PrestadorServico>
        </InfNfse>
      </Nfse>
    </CompNfse>
    """


def _xml_export_link(label: str, component_id: str) -> str:
    command_id = f"consultarnfseForm:dataTable:0:{component_id}"
    return f"""
    <a title="Exportar XML" onclick="if(typeof jsfcljs == 'function'){{
      jsfcljs(document.getElementById('consultarnfseForm'),
      {{'{command_id}':'{command_id}'}},'');
    }}return false">{label}</a>
    """


if __name__ == "__main__":
    unittest.main()
