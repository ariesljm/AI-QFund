"""rankhandler 榜单快照的解析契约（ticket 26 快路径换源）。

为什么是这条路径：`fundmobapi` 批量接口已确定性失效（`ErrCode=61136403`，
换 deviceid/单只/多只同一错误码），生产日志连续多日"净值批量增量完成: 成功 0 只"，
导致每天上万只退入逐只 lsjz（69 分钟）。

为什么不用 `pingzhongdata`：实测两者同被限到 ~3 req/s，**瓶颈是"每请求只取一只"**，
换同维度端点无收益（pingzhongdata 还要多传约 400 倍数据）。rankhandler 一次 200 只，
实测 102 页 36.8 秒覆盖 20,356 只 / 本股票池 91.3%。
"""

from app.data.nav import _RANKHANDLER_URL, parse_rankhandler_page

# 2026-09-14 实测响应原文（截取首行；字段位 0=代码 3=日期 5=累计净值）
REAL_PAGE = (
    "var rankData = {datas:["
    "'014841,东方阿尔法医疗健康混合发起A,DFAEFYLJKHHFQA,2026-09-14,1.1435,1.1435,"
    "6.52,6.73,2.08,24.47,13.87,-13.69,44.4,25.43,4.46,14.35,2022-03-30,1,14.35,1.50%',"
    "'000001,华夏成长混合,HXCZHH,2026-09-14,0.9000,3.4000,1.0,2.0,3.0,4.0,5.0,"
    "6.0,7.0,8.0,9.0,10.0,2001-12-18,1,600.0,0.15%'"
    "],allRecords:20357,pageIndex:1,pageNum:200};"
)


class TestRankhandlerUrl:
    def test_must_be_https(self):
        """回归锁定：必须用 https。

        http 会得到 301（重定向到 https），而 `fetch_async` 不跟随重定向且 301 不触发
        `raise_for_status` → 拿到空 body → 快照为空 → **静默退入 69 分钟慢路径**。
        """
        assert _RANKHANDLER_URL.startswith("https://")


class TestParseRankhandlerPage:
    def test_real_page_parses_code_date_cum_nav(self):
        """字段位必须与实测一致：0=代码 3=净值日期 5=累计净值。"""
        assert parse_rankhandler_page(REAL_PAGE) == [
            ("014841", "2026-09-14", 1.1435),
            ("000001", "2026-09-14", 3.4),
        ]

    def test_missing_datas_returns_empty(self):
        """响应无 datas（被拒/改版）→ 空列表，由调用方判定快照失败。"""
        assert parse_rankhandler_page("<!DOCTYPE html><html>...") == []
        assert parse_rankhandler_page("var rankData = {datas:[]};") == []

    def test_malformed_rows_are_skipped_not_fatal(self):
        """行缺字段/数值非法只跳过该行，不影响整页。"""
        text = (
            "var rankData = {datas:["
            "'014841,名字,PY',"                                   # 字段不足
            "'000002,名字,PY,2026-09-14,1.0,',"                    # 累计净值为空
            "'000003,名字,PY,2026-09-14,1.0,abc',"                 # 累计净值非数
            "'000004,名字,PY,2026-09-14,1.0,2.5'"                  # 合法
            "]};"
        )
        assert parse_rankhandler_page(text) == [("000004", "2026-09-14", 2.5)]
