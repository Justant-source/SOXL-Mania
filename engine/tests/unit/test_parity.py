from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
import unittest

from soxl_mania.backtest.parity import check_data_parity, load_reference_fixture
from soxl_mania.data.providers.csv_provider import CsvMarketDataProvider
from soxl_mania.domain.models import MarketBar


FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "mentor_reference_2011_2024.json"
CSV_FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "sample_soxl.csv"


class ParityTest(unittest.TestCase):
    def test_reference_fixture_loads(self) -> None:
        payload = load_reference_fixture(FIXTURE)
        self.assertEqual(payload["source_image_sha256"], "d26f8c4c954f18f7f59eb721410d2224a58bf4be778f0941222d4c22f113c928")

    def test_sample_data_mismatch_is_explicit(self) -> None:
        bars = CsvMarketDataProvider(CSV_FIXTURE).load_bars("SOXL")
        result = check_data_parity(bars, load_reference_fixture(FIXTURE))
        self.assertEqual(result.status, "DATA_MISMATCH")

    def test_data_parity_tolerates_display_precision_noise(self) -> None:
        bars = [
            MarketBar(
                symbol="SOXL",
                session_date=date(2024, 1, 2),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("28.04000091552734"),
                adj_close=Decimal("28.04000091552734"),
            ),
            MarketBar(
                symbol="SOXL",
                session_date=date(2024, 12, 31),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("27.30999946594238"),
                adj_close=Decimal("27.30999946594238"),
            ),
        ]
        reference = {"annual_soxl_boundaries": [{"year": 2024, "start": "28.04", "end": "27.31"}]}
        result = check_data_parity(bars, reference)
        self.assertEqual(result.status, "PASS")
        self.assertIsNone(result.first_mismatch)

    def test_data_parity_reports_first_meaningful_boundary_mismatch(self) -> None:
        bars = [
            MarketBar(
                symbol="SOXL",
                session_date=date(2022, 1, 3),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("72.09999847412109"),
                adj_close=Decimal("72.09999847412109"),
            ),
            MarketBar(
                symbol="SOXL",
                session_date=date(2022, 12, 30),
                open=Decimal("1"),
                high=Decimal("1"),
                low=Decimal("1"),
                close=Decimal("9.67000007629395"),
                adj_close=Decimal("9.67000007629395"),
            ),
        ]
        reference = {"annual_soxl_boundaries": [{"year": 2022, "start": "72.10", "end": "9.36"}]}
        result = check_data_parity(bars, reference)
        self.assertEqual(result.status, "DATA_MISMATCH")
        self.assertEqual(result.first_mismatch["year"], "2022")
        self.assertEqual(result.first_mismatch["boundary"], "end")
        self.assertEqual(result.first_mismatch["session_date"], "2022-12-30")


if __name__ == "__main__":
    unittest.main()
