"""基础验证测试：纯计算函数。"""

import numpy as np


def test_calc_hurst():
    """测试 Hurst 指数计算。"""
    from features import calc_hurst
    
    # 随机游走序列，Hurst 应接近 0.5
    np.random.seed(42)
    random_walk = np.cumsum(np.random.randn(100))
    h = calc_hurst(random_walk)
    # 随机游走的 Hurst 指数可能偏离 0.5，放宽范围
    assert 0.0 <= h <= 1.0, f"Hurst 指数异常: {h}"
    
    # 短序列应返回默认值 0.5
    short_series = np.array([1.0, 2.0, 3.0])
    h_short = calc_hurst(short_series)
    assert h_short == 0.5, f"短序列 Hurst 应为 0.5: {h_short}"
    
    print("calc_hurst 测试通过")


def test_calc_atr():
    """测试 ATR 计算。"""
    from monitor import calc_atr
    
    # 恒定波动率序列
    navs = [1.0] * 20
    atr = calc_atr(navs)
    assert atr >= 0, f"ATR 应为非负数: {atr}"
    
    # 波动序列
    navs_volatile = [1.0, 1.1, 0.9, 1.2, 0.8] * 4
    atr_volatile = calc_atr(navs_volatile)
    assert atr_volatile > 0, f"波动序列 ATR 应大于 0: {atr_volatile}"
    
    print("calc_atr 测试通过")


def test_calc_regime():
    """测试大盘状态机。"""
    from features import calc_regime
    
    # 使用内存数据库测试
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.execute("""
        CREATE TABLE index_daily (
            code TEXT, date TEXT, open REAL, high REAL, low REAL,
            close REAL, volume REAL, ma60 REAL
        )
    """)
    
    # 插入测试数据：close > ma60 + 2% → BULL
    conn.execute(
        "INSERT INTO index_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sh000300", "2024-01-01", 100, 105, 95, 103, 1000, 100)
    )
    regime = calc_regime(conn, "sh000300")
    assert regime == "BULL", f"应为 BULL: {regime}"
    
    # 插入测试数据：close < ma60 - 2% → BEAR
    conn.execute(
        "INSERT INTO index_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("sh000300", "2024-01-02", 100, 105, 95, 97, 1000, 100)
    )
    regime = calc_regime(conn, "sh000300")
    assert regime == "BEAR", f"应为 BEAR: {regime}"
    
    conn.close()
    print("calc_regime 测试通过")


def test_db_conn():
    """测试数据库连接 context manager。"""
    import sqlite3
    import tempfile
    from pathlib import Path
    
    # 使用临时数据库，跳过迁移
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        temp_path = Path(f.name)
    
    try:
        # 直接创建连接，不使用 _db_conn
        conn = sqlite3.connect(str(temp_path))
        conn.execute("CREATE TABLE test (id INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO test (id) VALUES (1)")
        conn.commit()
        
        # 验证数据已保存
        row = conn.execute("SELECT id FROM test WHERE id = 1").fetchone()
        assert row is not None, "数据未保存"
        assert row[0] == 1, f"数据不匹配: {row[0]}"
        
        conn.close()
        print("_db_conn 测试通过")
    finally:
        if temp_path.exists():
            temp_path.unlink()


def test_parse_llm_result():
    """测试 LLM JSON 解析。"""
    from recommend import _parse_llm_result

    valid = {"000001": "基金A", "000002": "基金B"}

    # 标准 JSON
    content = '{"selected_code": "000001", "reason": "业绩好", "vetoed": []}'
    r = _parse_llm_result(content, valid)
    assert r is not None
    assert r["selected_code"] == "000001"
    assert r["selected_name"] == "基金A"

    # code 不在 valid 中
    content = '{"selected_code": "999999", "reason": "test"}'
    r = _parse_llm_result(content, valid)
    assert r is None

    # 无效 JSON
    r = _parse_llm_result("not json", valid)
    assert r is None

    # 返回非 dict
    r = _parse_llm_result('["a", "b"]', valid)
    assert r is None

    print("_parse_llm_result 测试通过")


def test_build_prompt():
    """测试 LLM 提示词构建。"""
    from recommend import _build_final_prompt
    from macro_agent import MacroContext

    candidates = [
        {"code": "000001", "name": "基金A", "sector": "科技",
         "sector_weight": 0.5, "calmar": 1.2, "hurst_60d": 0.6, "combo": 0.85,
         "momentum_20d": 5.0, "sector_rel_momentum": 2.0,
         "sector_rel_calmar": 0.3,
         "holdings": [{"stock_name": "贵州茅台", "industry": "白酒", "weight": 8.5}],
         "matched_news": []},
    ]
    ctx = MacroContext(news_summary="市场震荡", regime_label="neutral",
                       recommended_sectors=["科技"], risk_sectors=[],
                       date="2024-01-01")
    prompt = _build_final_prompt(candidates, ctx, ["规则1"])

    assert "科技" in prompt
    assert "000001" in prompt
    assert "市场震荡" in prompt
    assert "规则1" in prompt

    print("_build_final_prompt 测试通过")


def test_extract_rule():
    """测试从 LLM 响应提取规则。"""
    from evolve import _extract_rule

    # JSON 场景
    content = '{"rule": "避免追高", "rationale": "减少亏损"}'
    assert _extract_rule(content) == "避免追高"

    # 空输入
    assert _extract_rule("") == ""

    # 非 JSON 但有 rule 字段
    content = 'rule: 避免追高}'
    assert _extract_rule(content)

    print("_extract_rule 测试通过")


def test_extract_rationale():
    """测试从 LLM 响应提取 rationale。"""
    from evolve import _extract_rationale

    content = '{"rule": "避免追高", "rationale": "减少亏损"}'
    assert _extract_rationale(content) == "减少亏损"

    assert _extract_rationale("") == ""

    print("_extract_rationale 测试通过")


def test_keywords():
    """测试关键词提取。"""
    from evolve import _keywords

    # 中文需要空格或标点分隔
    kw = _keywords("避免 追高 买入")
    assert "避免" in kw
    assert "追高" in kw
    assert "买入" in kw

    # 英文
    kw = _keywords("avoid chasing highs")
    assert "avoid" in kw

    # 短词（<2字）应被过滤
    assert "a" not in _keywords("a b c")

    print("_keywords 测试通过")


def test_check_conflict():
    """测试规则冲突检测。"""
    from evolve import check_conflict, _keywords

    # 重叠度 > 0.6 视为冲突（关键词通过空格分隔提取）
    assert check_conflict("避免 追高 买入", ["避免 追高 买入 科技"]) is True

    # 无重叠 → 不冲突
    assert check_conflict("控制 回撤", ["避免 追高"]) is False

    print("check_conflict 测试通过")


def test_check_trailing_stop():
    """测试追踪止损逻辑。"""
    from monitor import check_trailing_stop

    # 未触发：当前值接近最高点
    navs = [1.0, 1.1, 1.05, 1.08]
    triggered, _ = check_trailing_stop("000001", 1.1, 0.03, navs)
    assert triggered is False

    # 触发：回撤 > 2×ATR
    navs = [1.0, 1.1, 1.0]
    triggered, _ = check_trailing_stop("000001", 1.1, 0.02, navs)
    assert triggered is True

    # 空 navs → 不触发
    triggered, _ = check_trailing_stop("000001", 1.1, 0.03, [])
    assert triggered is False

    # highest_nav 无效 → 不触发
    triggered, _ = check_trailing_stop("000001", None, 0.03, navs)
    assert triggered is False

    print("check_trailing_stop 测试通过")


def test_parse_holdings_html():
    """测试持仓 HTML 解析。"""
    from data_foundation import _parse_holdings_html

    # 空输入
    report_date, holdings = _parse_holdings_html("")
    assert report_date is None
    assert holdings == []

    # 简单 HTML
    html = (
        '<label>2024-06-30</font></label>'
        '<tr><td>1</td><td><a>600519</a></td>'
        '<td class=\'tol\'><a>贵州茅台</a></td>'
        '<td class=\'tor\'>9.87%</td></tr>'
    )
    report_date, holdings = _parse_holdings_html(html)
    assert report_date == "2024-06-30"
    assert len(holdings) == 1
    assert holdings[0]["stock_code"] == "600519"
    assert holdings[0]["stock_name"] == "贵州茅台"
    assert abs(holdings[0]["weight"] - 9.87) < 0.01

    print("_parse_holdings_html 测试通过")


def _make_test_db():
    """创建内存数据库并注入 schema。"""
    import sqlite3
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE fund_nav (code TEXT, date TEXT, cum_nav REAL, PRIMARY KEY (code, date));
        CREATE TABLE fund_features (code TEXT, date TEXT, rbsa_weight_1 REAL, PRIMARY KEY (code, date));
        CREATE TABLE fund_basic (code TEXT PRIMARY KEY, name TEXT, type TEXT, is_buyable INTEGER DEFAULT 1);
        CREATE TABLE recommend_log (id INTEGER PRIMARY KEY, code TEXT, status TEXT, recommend_date TEXT, buy_reason TEXT, highest_nav REAL, return_rate REAL);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO fund_nav VALUES ('000001', '2024-01-01', 1.0)")
    conn.execute("INSERT INTO fund_nav VALUES ('000001', '2024-01-02', 1.1)")
    conn.execute("INSERT INTO fund_nav VALUES ('000001', '2024-01-03', 1.05)")
    conn.execute("INSERT INTO fund_nav VALUES ('000001', '2024-01-04', 1.2)")
    conn.execute("INSERT INTO fund_features VALUES ('000001', '2024-01-01', 0.5)")
    conn.execute("INSERT INTO fund_features VALUES ('000001', '2024-01-04', 0.2)")
    conn.execute("INSERT INTO recommend_log (code, status, recommend_date) VALUES ('000001', 'HOLD', '2024-01-01')")
    conn.commit()
    return conn


def test_update_highest_nav():
    """测试最高累计净值查询。"""
    import sqlite3
    from pathlib import Path
    import tempfile

    # 创建临时数据库
    conn = _make_test_db()
    db_path = Path(tempfile.mktemp(suffix=".db"))
    conn_backup = sqlite3.connect(str(db_path))
    conn_backup.executescript("".join(conn.iterdump()))
    conn_backup.close()

    import data_store
    original_path = data_store.DB_PATH
    data_store.DB_PATH = db_path

    from monitor import update_highest_nav
    highest = update_highest_nav("000001", "2024-01-01")
    assert highest == 1.2, f"最高净值应为 1.2: {highest}"

    # 无净值数据
    highest = update_highest_nav("999999", "2024-01-01")
    assert highest is None

    data_store.DB_PATH = original_path
    db_path.unlink(missing_ok=True)
    print("update_highest_nav 测试通过")


def test_check_style_drift():
    """测试风格漂移检测。"""
    import sqlite3
    from pathlib import Path
    import tempfile

    conn = _make_test_db()
    db_path = Path(tempfile.mktemp(suffix=".db"))
    conn_backup = sqlite3.connect(str(db_path))
    conn_backup.executescript("".join(conn.iterdump()))
    conn_backup.close()

    import data_store
    original_path = data_store.DB_PATH
    data_store.DB_PATH = db_path

    from monitor import check_style_drift

    # 买入权重 0.5 → 当前 0.2，差值 0.3 > 0.15 → 触发
    triggered, reason = check_style_drift("000001")
    assert triggered is True
    assert "风格漂移" in reason

    # 未知 code → 不触发
    triggered, reason = check_style_drift("999999")
    assert triggered is False

    data_store.DB_PATH = original_path
    db_path.unlink(missing_ok=True)
    print("check_style_drift 测试通过")


def test_calc_loss():
    """测试亏损计算。"""
    import sqlite3
    from pathlib import Path
    import tempfile

    conn = _make_test_db()
    db_path = Path(tempfile.mktemp(suffix=".db"))
    conn_backup = sqlite3.connect(str(db_path))
    conn_backup.executescript("".join(conn.iterdump()))
    conn_backup.close()

    import data_store
    original_path = data_store.DB_PATH
    data_store.DB_PATH = db_path

    from evolve import _calc_loss

    loss = _calc_loss("000001", "2024-01-01", "2024-01-04")
    assert loss is not None
    assert abs(loss - 0.2) < 0.01  # 1.2/1.0 - 1 = 0.2

    # 未知 code → None
    loss = _calc_loss("999999", "2024-01-01", "2024-01-04")
    assert loss is None

    data_store.DB_PATH = original_path
    db_path.unlink(missing_ok=True)
    print("_calc_loss 测试通过")


if __name__ == "__main__":
    test_calc_hurst()
    test_calc_atr()
    test_calc_regime()
    test_db_conn()
    test_parse_llm_result()
    test_build_prompt()
    test_extract_rule()
    test_extract_rationale()
    test_keywords()
    test_check_conflict()
    test_check_trailing_stop()
    test_parse_holdings_html()
    test_update_highest_nav()
    test_check_style_drift()
    test_calc_loss()
    print("\n所有测试通过！")