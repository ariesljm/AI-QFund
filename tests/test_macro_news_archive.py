"""宏观文本归档累积测试：逐日留存、同日覆盖（ticket 04）。

前提修正（2026-09-10 实测）：`macro_news` 并非"仅 1 行"——数据基座每个交易日
写入一行（实测 2026-08-14~09-04 共 16 个交易日），news_summary / top_gainers /
top_losers / etf_net_flow / flow_json / context_json 全部逐日累积。
本测试锁定该累积行为，防止回归为"覆盖成单行"。

历史深度限制：新闻/资金流只能每日增量抓取（东财接口不提供历史新闻回填），
故回测素材深度只能随运行时间累积，非代码可解决。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from app.database import db_conn
from app.repo.decision import save_context, save_flow_data, save_macro_news


@pytest.fixture(autouse=True)
def _clean_macro_news():
    """每个用例前清空 macro_news（conftest 的业务库为 session 级共享）。"""
    with db_conn() as conn:
        conn.execute("DELETE FROM macro_news")
    yield


def _rows():
    with db_conn() as conn:
        return conn.execute(
            "SELECT date, news_summary, flow_json, context_json FROM macro_news ORDER BY date"
        ).fetchall()


def test_daily_archive_accumulates_rows():
    """不同决策日分别留存，不互相覆盖。"""
    save_macro_news("2026-09-01", "新闻A", "领涨A", "领跌A", "资金A", news_date="2026-09-01")
    save_macro_news("2026-09-02", "新闻B", "领涨B", "领跌B", "资金B", news_date="2026-09-02")

    rows = _rows()
    assert [r[0] for r in rows] == ["2026-09-01", "2026-09-02"]
    assert rows[0][1] == "新闻A"
    assert rows[1][1] == "新闻B"


def test_same_day_rewrite_overwrites_not_appends():
    """同一决策日重复写入 → 覆盖，不新增行（幂等）。"""
    save_macro_news("2026-09-03", "旧新闻", "g", "l", "f")
    save_macro_news("2026-09-03", "新新闻", "g2", "l2", "f2")

    rows = _rows()
    assert len(rows) == 1
    assert rows[0][1] == "新新闻"


def test_flow_and_context_archive_into_same_day_row():
    """资金流/上下文快照按日 upsert 到同一行（三个写入函数共用一个 date 行）。"""
    save_macro_news("2026-09-04", "新闻", "g", "l", "f")
    save_flow_data("2026-09-04", {"total_net": 123})
    save_context("2026-09-04", {"regime": "bull"})

    rows = _rows()
    assert len(rows) == 1
    assert '"total_net": 123' in rows[0][2]
    assert '"regime": "bull"' in rows[0][3]
