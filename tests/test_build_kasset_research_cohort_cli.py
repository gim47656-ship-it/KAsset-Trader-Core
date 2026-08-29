from __future__ import annotations

from scripts import build_kasset_research_cohort as cli


def test_cohort_cli_is_dry_run_first_with_explicit_source_and_size_100() -> None:
    args = cli.parse_args(
        [
            "--market",
            "kr",
            "--valuation-source",
            "naver_finance",
            "--force-symbol",
            "005930",
        ]
    )

    assert args.market == "kr"
    assert args.valuation_source == "naver_finance"
    assert args.size == 100
    assert args.force_symbol == ["005930"]
    assert args.commit is False
