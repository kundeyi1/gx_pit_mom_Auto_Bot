"""Generate cloud-portable NAV files with the original signal-spliced method."""

import argparse
from pathlib import Path

import pandas as pd

from src.backtest import build_auto_updating_nav, build_time_spliced_nav
from src.data_provider import WindLocalProvider
from src.analysis import GXPitMomActions


LEGACY_REFERENCE = {
    "zx_yjhy": (17.045276112645773, 1.225478106637239, 14.411344072385397),
    "zx_ejhy": (18.308491495852177, 1.245589257047715, 15.209996496206802),
}


def _load_prices(price_path: Path):
    provider = WindLocalProvider(data_dir=str(price_path.parent), start_date="2013-01-01")
    return provider.get_wide_table(price_path.name)


def _generate(
    prices: pd.DataFrame,
    detail_path: Path,
    output_path: Path,
    n_groups: int,
    timing_signals: dict,
    verify_legacy_reference: bool,
    reference_key: str,
):
    details = pd.read_csv(detail_path, parse_dates=["date"])
    legacy_result = build_time_spliced_nav(prices, details, n_groups=n_groups, holding_days=20)
    if verify_legacy_reference:
        actual = (
            legacy_result.long.iloc[-1],
            legacy_result.short.iloc[-1],
            legacy_result.long_short.iloc[-1],
        )
        expected = LEGACY_REFERENCE[reference_key]
        max_error = max(abs(value - target) for value, target in zip(actual, expected))
        if max_error > 1e-9:
            raise RuntimeError(
                f"{reference_key} 未通过旧版净值核验，末值最大误差 {max_error:.12g}"
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    baseline_path = output_path.parent / f"backtest_signal_groups_{reference_key}.csv"
    details.loc[:, ["date", "asset_code", "group_id"]].to_csv(
        baseline_path,
        index=False,
        date_format="%Y-%m-%d",
    )
    result = build_auto_updating_nav(
        prices,
        details,
        timing_signals,
        n_groups=n_groups,
        holding_days=20,
    )
    frame = pd.concat([result.long, result.short, result.long_short], axis=1)
    frame.to_csv(output_path, index=True, date_format="%Y-%m-%d", float_format="%.12f")
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--l1-prices", type=Path, default=Path("data/ZX_YJHY.xlsx"))
    parser.add_argument("--l2-prices", type=Path, default=Path("data/ZX_EJHY.xlsx"))
    parser.add_argument("--benchmark", type=Path, default=Path("data/000985_prices.xlsx"))
    parser.add_argument("--l1-groups", type=Path, required=True)
    parser.add_argument("--l2-groups", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--verify-legacy-reference", action="store_true")
    args = parser.parse_args()

    l1_prices = _load_prices(args.l1_prices)
    l2_prices = _load_prices(args.l2_prices)
    benchmark = _load_prices(args.benchmark)
    analyzer = GXPitMomActions(data_dir=str(args.benchmark.parent), start_date="2017-01-01")
    timing_signals = {
        "breakout": analyzer.gx_pit_breakout(benchmark),
        "rebound": analyzer.gx_pit_rebound(benchmark),
        "rotation": analyzer.gx_pit_rotation(benchmark, l1_prices),
    }

    specs = (
        ("zx_yjhy", l1_prices, args.l1_groups, 5),
        ("zx_ejhy", l2_prices, args.l2_groups, 10),
    )
    for key, prices, detail_path, n_groups in specs:
        output_path = args.output_dir / f"backtest_nav_{key}.csv"
        result = _generate(
            prices,
            detail_path,
            output_path,
            n_groups,
            timing_signals,
            args.verify_legacy_reference,
            key,
        )
        final_values = (result.long.iloc[-1], result.short.iloc[-1], result.long_short.iloc[-1])
        print(
            f"{key}: {result.signal_count} signals ({result.new_signal_count} new), "
            f"{len(result.long)} daily points through {result.long.index[-1]:%Y-%m-%d}, "
            f"long={final_values[0]:.6f}, short={final_values[1]:.6f}, "
            f"long_short={final_values[2]:.6f}, endpoint_error={result.max_endpoint_error:.3g}"
        )


if __name__ == "__main__":
    main()
