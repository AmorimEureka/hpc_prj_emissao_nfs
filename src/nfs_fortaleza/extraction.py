from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Mapping

from nfs_fortaleza.config import Settings
from nfs_fortaleza.load import load_nfse_xml, upsert_nfse_situacao
from nfs_fortaleza.nfse_xml import infer_nfse_period
from nfs_fortaleza.periods import (
    DateRangePeriod,
    MonthPeriod,
    iter_date_windows,
    iter_months,
    looks_like_date,
    parse_date,
    parse_month_period,
)
from nfs_fortaleza.portal import (
    VISAO_PRESTADOR,
    VISAO_TOMADOR,
    PeriodWithoutInvoicesError,
    PortalClient,
    PortalOptions,
)


class ExtractionConfigurationError(ValueError):
    """Raised when dag_run.conf has incompatible extraction filters."""


@dataclass(frozen=True)
class ExtractionPayload:
    cnpj: str | None = None
    numero_nfse: str | None = None
    periods: tuple[MonthPeriod | DateRangePeriod, ...] = ()

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        default_date: date,
    ) -> ExtractionPayload:
        payload = value or {}
        cnpj = _optional_text(payload.get("cnpj"))
        numero_nfse = _optional_text(payload.get("numero_nfse"))
        competencia = _optional_text(payload.get("competencia"))
        inicio = _optional_text(payload.get("inicio"))
        fim = _optional_text(payload.get("fim"))

        if cnpj or numero_nfse:
            if not cnpj or not numero_nfse:
                raise ExtractionConfigurationError(
                    "Informe cnpj e numero_nfse juntos."
                )
            if competencia or inicio or fim:
                raise ExtractionConfigurationError(
                    "Nao combine cnpj/numero_nfse com competencia/inicio/fim."
                )
            cnpj_digits = re.sub(r"\D", "", cnpj)
            if len(cnpj_digits) != 14:
                raise ExtractionConfigurationError("CNPJ deve conter 14 digitos.")
            if not numero_nfse.isdigit() or len(numero_nfse) > 15:
                raise ExtractionConfigurationError(
                    "numero_nfse deve conter de 1 a 15 digitos."
                )
            return cls(cnpj=cnpj_digits, numero_nfse=numero_nfse)

        if competencia:
            if inicio or fim:
                raise ExtractionConfigurationError(
                    "Use competencia ou inicio/fim, nao ambos."
                )
            return cls(periods=(parse_month_period(competencia),))

        if inicio or fim:
            if not inicio or not fim:
                raise ExtractionConfigurationError(
                    "Informe inicio e fim juntos."
                )
            if looks_like_date(inicio) or looks_like_date(fim):
                if not (looks_like_date(inicio) and looks_like_date(fim)):
                    raise ExtractionConfigurationError(
                        "inicio e fim devem usar o mesmo formato de data completa."
                    )
                start_date = parse_date(inicio)
                end_date = parse_date(fim)
                if end_date < start_date:
                    raise ExtractionConfigurationError(
                        "fim deve ser maior ou igual a inicio."
                    )
                return cls(
                    periods=tuple(
                        iter_date_windows(
                            start_date,
                            end_date,
                            today=default_date,
                        )
                    )
                )

            start_period = parse_month_period(inicio)
            end_period = parse_month_period(fim)
            if end_period < start_period:
                raise ExtractionConfigurationError(
                    "fim deve ser maior ou igual a inicio."
                )
            return cls(periods=tuple(iter_months(start_period, end_period)))

        return cls(
            periods=(
                MonthPeriod(
                    year=default_date.year,
                    month=default_date.month,
                ),
            )
        )


@dataclass(frozen=True)
class ExtractionSummary:
    files: tuple[Path, ...]
    loaded_files: int
    empty_periods: tuple[str, ...]
    situacoes_atualizadas: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "arquivos": [str(path) for path in self.files],
            "arquivos_carregados": self.loaded_files,
            "periodos_sem_notas": list(self.empty_periods),
            "situacoes_atualizadas": self.situacoes_atualizadas,
        }


def extract_and_load_nfse(
    settings: Settings,
    payload: ExtractionPayload,
    *,
    options: PortalOptions,
    table_name: str = "nfse_xml",
) -> ExtractionSummary:
    client = PortalClient(settings, options)
    downloaded: list[Path] = []
    empty_periods: list[str] = []

    if payload.cnpj and payload.numero_nfse:
        if options.visao != VISAO_PRESTADOR:
            raise ExtractionConfigurationError(
                "Consulta por cnpj/numero_nfse so existe para notas emitidas (visao prestador)."
            )
        try:
            files = client.export_nfse(payload.cnpj, payload.numero_nfse)
        except PeriodWithoutInvoicesError:
            files = []
            empty_periods.append(
                f"CNPJ {payload.cnpj}, NFS-e {payload.numero_nfse}"
            )
        for file_path in files:
            load_nfse_xml(settings, file_path, infer_nfse_period(file_path))
            downloaded.append(file_path)
        return ExtractionSummary(
            files=tuple(downloaded),
            loaded_files=len(downloaded),
            empty_periods=tuple(empty_periods),
        )

    for period in payload.periods:
        try:
            files = client.export_competencia(period)
        except PeriodWithoutInvoicesError:
            empty_periods.append(period.label)
            continue
        for file_path in files:
            load_nfse_xml(settings, file_path, period, table_name=table_name)
            downloaded.append(file_path)

    situacoes = 0
    if options.visao == VISAO_TOMADOR and client.listed_nfse:
        # Inclui as páginas de períodos/inscrições sem XML novo: a situação
        # de notas antigas (ex.: cancelamento posterior) também é atualizada.
        situacoes = upsert_nfse_situacao(settings, client.listed_nfse, table_name=table_name)

    return ExtractionSummary(
        files=tuple(downloaded),
        loaded_files=len(downloaded),
        empty_periods=tuple(empty_periods),
        situacoes_atualizadas=situacoes,
    )


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None
