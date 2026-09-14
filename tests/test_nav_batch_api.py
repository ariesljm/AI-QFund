"""fundmobapi 批量接口的响应解析契约（ticket 25 根因 / ticket 26 修复）。

**这是本模块最重要的一条断言**：`Success=false` 的**接口故障**与
`Success=true` 但 `Datas` 为空的**合法无新数据**，在代码里必须可区分。

真实故障（2026-09-14 实测，原样抄进测试）：
接口持续返回 `ErrCode=61136403 网络繁忙`，而旧解析只读 `Datas`（为 null），
于是整组 30 只被当作"接口未返回" → 6,411 只整体回退到约 71 分钟的逐只
lsjz 路径 → 跑到一半被截断 → 半个市场静默停在 2026-09-03。
"""

import json

import pytest

from app.data.nav import FundmobapiError, parse_fundmobapi_payload

# 2026-09-14 curl 实测的真实响应原文（HTTP 200，但 Success=false）
REAL_FAILURE_PAYLOAD = (
    '{"Datas":null,"ErrCode":61136403,"Success":false,'
    '"ErrMsg":"网络繁忙，请稍后重试！","Message":null,"ErrorCode":"61136403",'
    '"ErrorMessage":"网络繁忙，请稍后重试！","ErrorMsgLst":null,'
    '"TotalCount":0,"Expansion":null}'
)


class TestParseFundmobapiPayload:
    def test_real_failure_payload_raises(self):
        """把真实故障响应钉进测试：它必须抛错，不能退化成空列表。"""
        with pytest.raises(FundmobapiError) as ei:
            parse_fundmobapi_payload(REAL_FAILURE_PAYLOAD)
        assert "61136403" in str(ei.value)
        assert "网络繁忙" in str(ei.value)

    def test_ok_payload_is_parsed(self):
        payload = json.dumps({"Success": True, "Datas": [
            {"FCODE": "000001", "PDATE": "2026-09-11", "ACCNAV": "1.2345"}]})
        assert parse_fundmobapi_payload(payload) == [
            {"code": "000001", "date": "2026-09-11", "cum_nav": 1.2345}]

    def test_ok_but_empty_is_legitimately_empty(self):
        """Success=true + Datas 空 = 真的没有新数据 → 不得抛错。

        若把这一条也抛错，就会把"这些基金停更"误判成"接口故障"，方向反了。
        """
        assert parse_fundmobapi_payload('{"Datas":null,"Success":true}') == []
        assert parse_fundmobapi_payload('{"Datas":[],"Success":true}') == []

    def test_malformed_rows_are_skipped_not_fatal(self):
        """个别行字段缺失只跳过该行，不影响整组。"""
        payload = json.dumps({"Success": True, "Datas": [
            {"FCODE": "000001"},  # 缺 PDATE/ACCNAV
            {"FCODE": "000002", "PDATE": "2026-09-11", "ACCNAV": "9.9"}]})
        assert parse_fundmobapi_payload(payload) == [
            {"code": "000002", "date": "2026-09-11", "cum_nav": 9.9}]
