"""2.0 数据地基回填入口（票 04/05/06/07 的运维动作）。

用法（可断点续传，中断后重跑只补缺失）：
    python scripts/backfill_2_0_data.py --val    # 05：个股估值（重仓股全集）
    python scripts/backfill_2_0_data.py --daily  # 07：个股日线（重仓股全集）
    python scripts/backfill_2_0_data.py --fund   # 06：基金 AUM/申赎状态
    python scripts/backfill_2_0_data.py --holdings  # 04：多期持仓历史（按年，慢）

股票集合来源：fund_holdings 去重（当前快照）；基金集合：fund_basic is_buyable=1。
所有回填函数幂等（INSERT OR REPLACE / ON CONFLICT），中断续传安全。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import database as db_mod
from app.utils.log import get_logger

logger = get_logger("backfill")


def stock_universe() -> list[str]:
    """重仓股全集（fund_holdings 去重），回填 05/07 的对象。"""
    with db_mod.db_conn() as conn:
        rows = conn.execute("SELECT DISTINCT stock_code FROM fund_holdings").fetchall()
    return [r[0] for r in rows]


def fund_universe() -> list[str]:
    """基金全集（is_buyable=1），回填 06 的对象。"""
    with db_mod.db_conn() as conn:
        rows = conn.execute("SELECT code FROM fund_basic WHERE is_buyable = 1").fetchall()
    return [r[0] for r in rows]


def backfill_valuation() -> None:
    from app.data.valuation import backfill_valuations
    codes = stock_universe()
    logger.info("票 05 估值回填：%d 只股票", len(codes))
    res = backfill_valuations(codes)
    logger.info("完成: %s", res)


def backfill_daily() -> None:
    from app.data.stock_daily import stock_daily
    from app.data.store import save_stock_daily
    codes = stock_universe()
    logger.info("票 07 日线回填：%d 只股票", len(codes))
    done = failed = empty = 0
    for code in codes:
        try:
            closes = stock_daily(code, days=600)
        except Exception as e:
            failed += 1
            logger.warning("日线失败 %s: %s", code, str(e)[:100])
            continue
        if closes:
            save_stock_daily(code, closes)
            done += 1
        else:
            empty += 1
    logger.info("完成: done=%d empty=%d failed=%d", done, empty, failed)


def backfill_fund_info() -> None:
    from app.data.restriction import backfill_fund_info as _b
    codes = fund_universe()
    logger.info("票 06 AUM/申赎回填：%d 只基金", len(codes))
    res = _b(codes)
    logger.info("完成: %s", res)


def backfill_holdings(years: int) -> None:
    from app.data.holdings import backfill_holdings_history
    logger.info("票 04 多期持仓回填（近 %d 年）：请求量大，断点续传", years)
    n = backfill_holdings_history(years=years)
    logger.info("完成: 新增 %d 行", n)


def main() -> None:
    ap = argparse.ArgumentParser(description="2.0 数据地基回填（断点续传）")
    ap.add_argument("--val", action="store_true", help="票 05 个股估值")
    ap.add_argument("--daily", action="store_true", help="票 07 个股日线")
    ap.add_argument("--fund", action="store_true", help="票 06 AUM/申赎状态")
    ap.add_argument("--holdings", type=int, metavar="YEARS",
                    help="票 04 多期持仓历史（近 N 年，请求量大）")
    args = ap.parse_args()
    if not any([args.val, args.daily, args.fund, args.holdings]):
        ap.print_help()
        return
    if args.val:
        backfill_valuation()
    if args.daily:
        backfill_daily()
    if args.fund:
        backfill_fund_info()
    if args.holdings:
        backfill_holdings(args.holdings)


if __name__ == "__main__":
    main()
