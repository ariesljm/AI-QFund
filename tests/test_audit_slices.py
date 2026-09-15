"""票 12：审计切片一（持仓异动）装配快照。

holdings_change_snapshot 是纯函数（注入两期持仓直测）：留存率/新进/集中度
计算 + 文本格式 + 缺失必须显式可见（不静默填空）。
切片二/三/四（风险雷达/管理团队/舆情）数据源未定，不在此实现。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.llm.context import holdings_change_snapshot


def _h(code, name, weight, industry=""):
    return {"stock_code": code, "stock_name": name, "weight": weight, "industry": industry}


class TestHoldingsChangeSnapshot:
    PREV = [_h("600519", "贵州茅台", 30.0, "白酒"),
            _h("000858", "五粮液", 20.0, "白酒"),
            _h("601318", "平安", 10.0, "保险")]

    CUR = [_h("600519", "贵州茅台", 30.0, "白酒"),
           _h("000858", "五粮液", 20.0, "白酒"),
           _h("300750", "宁德时代", 10.0, "电池")]

    def test_retention_and_new_entry(self):
        got = holdings_change_snapshot(self.CUR, self.PREV)
        assert "留存率 67%（前 3 进 2）" in got          # 2/3
        assert "新进重仓：宁德时代(电池)" in got
        assert "集中度 60.0% → 60.0%（+0.0pp）" in got

    def test_no_industry_defaults(self):
        """industry 缺失 → '其他'（显式标注而非空串）。"""
        cur = [_h("600519", "贵州茅台", 50.0)]
        prev = [_h("000001", "平安银行", 50.0, "银行")]
        got = holdings_change_snapshot(cur, prev)
        assert "新进重仓：贵州茅台(其他)" in got

    def test_previous_missing_visible(self):
        got = holdings_change_snapshot(self.CUR, [])
        assert "上期持仓缺失" in got

    def test_current_missing_visible(self):
        got = holdings_change_snapshot([], self.PREV)
        assert "本期持仓缺失" in got

    def test_total_turnover(self):
        """完全换仓 → 留存率 0%，无新进空串兜底。"""
        cur = [_h("000001", "平安银行", 40.0, "银行")]
        prev = [_h("600519", "贵州茅台", 40.0, "白酒")]
        got = holdings_change_snapshot(cur, prev)
        assert "留存率 0%" in got and "新进重仓：平安银行(银行)" in got
