from __future__ import annotations

import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

from nfs_fortaleza.ipm_config import IpmSettings
from nfs_fortaleza.ipm_demonstrativo import parse_ipm_demonstrative
from nfs_fortaleza.ipm_extraction import (
    IpmExtractionConfigurationError,
    IpmExtractionPayload,
    _only_new_references,
    _reference_from_database,
    _references_to_process,
    _select_references,
    delete_ipm_references,
    extract_and_load_ipm_demonstratives,
    list_loaded_ipm_references,
)
from nfs_fortaleza.ipm_portal import IpmReference


SAMPLE = """DEMONSTRATIVO DE ANÁLISE DA CONTA MÉDICA - REF. 01/05/2026
REGISTRO ANS#OPERADORA#CNPJ OPERADORA#NRO DEMONSTRATIVO#DATA EMISSAO#CODIGO PRESTADOR#NOME#CNES
999999#IPM SAUDE#07965184000173##04/08/2026#0000123#Prestador#-
FATURA#LOTE#DATA ENVIO LOTE#NRO PROTOCOLO#VALOR PROTOCOLO#VALOR GLOSA PROTOCOLO#COD GLOSA PROTOCOLO#NRO GUIA/SENHA#NRO BENEFICIÁRIO#BENEFICIÁRIO#DATA REALIZAÇÃO#CODIGO TABELA#CODIGO SERVIÇO#DESCRIÇÃO#GRAU PART.#QTD EXECUTADA#VALOR PROCESSADO#VALOR LIBERADO#VALOR GLOSA#CODIGO GLOSA ITEM#CODIGO GLOSA CONTA
#5460643#15/05/2026#TISS_0000123_4207#4.881,60#3,16##00567003#01051742004#JOÃO DA SILVA#31/03/2026#22#10104020#ATENDIMENTO MÉDICO#00#2#,83#,82#,01#1714#
""".encode("cp1252")


class IpmDemonstrativeParserTests(unittest.TestCase):
    def test_parses_requested_fields_and_corrects_portal_label_inversion(self) -> None:
        records = list(
            parse_ipm_demonstrative(SAMPLE, IpmReference.parse("05/2026"))
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record["referencia"], date(2026, 5, 1))
        self.assertEqual(record["cnpj_operadora"], "07965184000173")
        self.assertEqual(record["numero_lote"], "TISS_0000123_4207")
        self.assertEqual(record["numero_protocolo"], "5460643")
        self.assertEqual(record["numero_guia_senha"], "00567003")
        self.assertEqual(record["codigo_beneficiario"], "01051742004")
        self.assertEqual(record["data_realizacao"], date(2026, 3, 31))
        self.assertEqual(record["descricao_servico"], "ATENDIMENTO MÉDICO")
        self.assertEqual(record["quantidade_executada"], Decimal("2"))
        self.assertEqual(record["valor_protocolo"], Decimal("4881.60"))
        self.assertEqual(record["valor_processado"], Decimal("0.83"))
        self.assertEqual(record["valor_liberado"], Decimal("0.82"))
        self.assertEqual(record["valor_glosa"], Decimal("0.01"))
        self.assertEqual(record["codigo_glosa"], "1714")
        self.assertEqual(len(record["id_registro"]), 64)

    def test_preserves_duplicate_lines_with_stable_distinct_ids(self) -> None:
        duplicated = SAMPLE + SAMPLE.splitlines()[-1] + b"\n"

        records = list(
            parse_ipm_demonstrative(duplicated, IpmReference.parse("05/2026"))
        )

        self.assertEqual(len(records), 2)
        self.assertNotEqual(records[0]["id_registro"], records[1]["id_registro"])


class IpmExtractionPayloadTests(unittest.TestCase):
    @staticmethod
    def _settings() -> IpmSettings:
        return IpmSettings(
            portal_url="https://example.test/PortalCredenciado",
            login="login",
            password="secret",
            database_url="postgresql://example.test/database",
            postgres_schema="test",
            provider_code="provider",
            operator_code="1",
        )

    def test_empty_payload_selects_all_available_references(self) -> None:
        payload = IpmExtractionPayload.from_mapping({})
        available = (
            IpmReference.parse("12/2025"),
            IpmReference.parse("01/2026"),
        )

        self.assertEqual(_select_references(available, payload.references), available)

    def test_accepts_and_orders_reference_list(self) -> None:
        payload = IpmExtractionPayload.from_mapping(
            {"referencias": ["05/2026", "12/2025", "05/2026"]}
        )

        self.assertEqual(
            [reference.label for reference in payload.references],
            ["12/2025", "05/2026"],
        )

    def test_rejects_unavailable_reference(self) -> None:
        with self.assertRaises(IpmExtractionConfigurationError):
            _select_references(
                (IpmReference.parse("05/2026"),),
                (IpmReference.parse("06/2026"),),
            )

    def test_selects_only_references_not_loaded_in_database(self) -> None:
        candidates = (
            IpmReference.parse("12/2025"),
            IpmReference.parse("01/2026"),
            IpmReference.parse("02/2026"),
        )
        loaded = (
            IpmReference.parse("12/2025"),
            IpmReference.parse("01/2026"),
        )

        self.assertEqual(
            _only_new_references(candidates, loaded),
            (IpmReference.parse("02/2026"),),
        )

    def test_normalizes_database_reference_types(self) -> None:
        expected = IpmReference.parse("05/2026")

        self.assertEqual(_reference_from_database(date(2026, 5, 1)), expected)
        self.assertEqual(
            _reference_from_database(datetime(2026, 5, 1, 12, 30)),
            expected,
        )
        self.assertEqual(_reference_from_database("2026-05-01"), expected)

    @patch("nfs_fortaleza.ipm_extraction.psycopg2.connect")
    def test_missing_table_is_treated_as_empty_history(self, connect) -> None:
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.return_value = (False,)

        references = list_loaded_ipm_references(self._settings())

        self.assertEqual(references, ())
        cursor.fetchall.assert_not_called()

    def test_reference_is_not_a_merge_key_anymore(self) -> None:
        # A stored merge_key hint survives unless explicitly switched off;
        # it deleted the Maida rows of the reloaded month.
        from nfs_fortaleza.ipm_demonstrativo import _column_hints

        self.assertIs(_column_hints()["referencia"]["merge_key"], False)

    def test_refreshes_recent_references_even_when_loaded(self) -> None:
        refs = tuple(
            IpmReference.parse(label)
            for label in ("06/2026", "07/2026", "08/2026", "09/2026")
        )
        # 09/2026 already "loaded": its demonstratives keep being issued.
        with patch("nfs_fortaleza.ipm_extraction.RECENT_REFERENCES_TO_REFRESH", 3):
            self.assertEqual(
                _references_to_process(refs, refs),
                refs[1:],
            )
            # A reference never loaded is processed even outside the window.
            self.assertEqual(
                _references_to_process(refs, refs[1:]),
                refs,
            )
        with patch("nfs_fortaleza.ipm_extraction.RECENT_REFERENCES_TO_REFRESH", 0):
            self.assertEqual(_references_to_process(refs, refs), ())
            # References named in dag_run.conf are always processed again.
            self.assertEqual(
                _references_to_process(refs[:1], refs, requested=True),
                refs[:1],
            )

    @patch("nfs_fortaleza.ipm_extraction.psycopg2.connect")
    def test_maida_rows_do_not_count_as_loaded_ipm_reference(self, connect) -> None:
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(True,), (True,)]
        cursor.fetchall.return_value = [(date(2026, 8, 1),)]

        references = list_loaded_ipm_references(self._settings())

        self.assertEqual(references, (IpmReference.parse("08/2026"),))
        query = cursor.execute.call_args_list[-1].args[0]
        self.assertIn("IS NULL", repr(query))
        self.assertIn("origem_maida", repr(query))

    @patch("nfs_fortaleza.ipm_extraction.psycopg2.connect")
    def test_reload_deletes_only_ipm_rows_of_the_references(self, connect) -> None:
        connection = connect.return_value.__enter__.return_value
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchone.side_effect = [(True,), (True,)]

        delete_ipm_references(
            self._settings(), (IpmReference.parse("09/2026"),)
        )

        query, params = cursor.execute.call_args_list[-1].args
        self.assertIn("DELETE FROM", repr(query))
        self.assertIn("origem_maida", repr(query))
        self.assertIn("IS NULL", repr(query))
        self.assertEqual(params, ([date(2026, 9, 1)],))

    @patch("nfs_fortaleza.ipm_extraction.delete_ipm_references")
    @patch("nfs_fortaleza.ipm_extraction.dlt.pipeline")
    @patch("nfs_fortaleza.ipm_extraction.IpmPortalClient")
    @patch("nfs_fortaleza.ipm_extraction.list_loaded_ipm_references")
    def test_does_not_download_or_run_dlt_when_reference_is_already_loaded(
        self,
        list_loaded,
        portal_client_class,
        pipeline,
        delete,
    ) -> None:
        reference = IpmReference.parse("05/2026")
        list_loaded.return_value = (reference,)
        portal_client = MagicMock()
        portal_client.list_references.return_value = (reference,)
        portal_client_class.return_value = portal_client
        with patch("nfs_fortaleza.ipm_extraction.RECENT_REFERENCES_TO_REFRESH", 0):
            summary = extract_and_load_ipm_demonstratives(
                self._settings(),
                IpmExtractionPayload(),
                downloads_dir=Path("/tmp/ipm-test"),
            )

        self.assertEqual(summary.processed_references, ())
        self.assertEqual(summary.skipped_references, (reference,))
        portal_client.download_demonstrative.assert_not_called()
        pipeline.assert_not_called()
        delete.assert_not_called()

    @patch("nfs_fortaleza.ipm_extraction.demonstrativo_conta_ipm_resource")
    @patch("nfs_fortaleza.ipm_extraction.delete_ipm_references")
    @patch("nfs_fortaleza.ipm_extraction.dlt.pipeline")
    @patch("nfs_fortaleza.ipm_extraction.IpmPortalClient")
    @patch("nfs_fortaleza.ipm_extraction.list_loaded_ipm_references")
    def test_recent_loaded_reference_is_downloaded_and_replaced(
        self,
        list_loaded,
        portal_client_class,
        pipeline,
        delete,
        _resource,
    ) -> None:
        reference = IpmReference.parse("09/2026")
        list_loaded.return_value = (reference,)
        portal_client = MagicMock()
        portal_client.list_references.return_value = (reference,)
        portal_client_class.return_value = portal_client
        events = []
        delete.side_effect = lambda *a: events.append("delete")
        pipeline.return_value.run.side_effect = lambda *a: events.append("load")

        summary = extract_and_load_ipm_demonstratives(
            self._settings(),
            IpmExtractionPayload(),
            downloads_dir=Path("/tmp/ipm-test"),
        )

        self.assertEqual(summary.processed_references, (reference,))
        portal_client.download_demonstrative.assert_called_once_with(reference)
        self.assertEqual(delete.call_args.args[1], (reference,))
        self.assertEqual(events, ["delete", "load"])


if __name__ == "__main__":
    unittest.main()
