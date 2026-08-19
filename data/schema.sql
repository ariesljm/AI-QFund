-- AI-QFund 数据库 Schema（SQLite WAL 模式）
-- 单一真相源：app/database._init_schema 读取此文件，_migrate 只做历史列迁移与缺表兜底

-- 基金基本信息
CREATE TABLE IF NOT EXISTS fund_basic (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    company TEXT,
    is_buyable INTEGER DEFAULT 1
);

-- 历史净值
CREATE TABLE IF NOT EXISTS fund_nav (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    unit_nav REAL,
    cum_nav REAL,
    equity_return REAL,
    unit_dividend REAL,
    PRIMARY KEY (code, date)
);

-- 宽基指数日线
CREATE TABLE IF NOT EXISTS index_daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    ema60 REAL,
    PRIMARY KEY (code, date)
);

-- 基金季度重仓股
CREATE TABLE IF NOT EXISTS fund_holdings (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    weight REAL,
    PRIMARY KEY (code, report_date, stock_code)
);

-- 特征计算结果
CREATE TABLE IF NOT EXISTS fund_features (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    regime TEXT,
    hurst_60d REAL,
    momentum_20d REAL,
    calmar REAL,
    downside_vol REAL,
    capture_up REAL,
    capture_down REAL,
    bias_60d REAL,
    drawdown_60d REAL,
    reversal_20d REAL,
    mom_5d REAL,
    mom_60d REAL,
    vol_20d REAL,
    rbsa_industry_1 TEXT,
    rbsa_weight_1 REAL,
    rbsa_industry_2 TEXT,
    rbsa_weight_2 REAL DEFAULT 0,
    rbsa_industry_3 TEXT,
    rbsa_weight_3 REAL DEFAULT 0,
    PRIMARY KEY (code, date)
);

-- 推荐记录
CREATE TABLE IF NOT EXISTS recommend_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    recommend_date TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    rank INTEGER,
    score REAL,
    combo REAL,
    regime TEXT,
    buy_reason TEXT,
    sell_reason TEXT,
    status TEXT DEFAULT 'HOLD',
    exit_date TEXT,
    highest_nav REAL,
    return_rate REAL,
    feature_snapshot TEXT,
    entry_nav REAL,
    candidate_codes TEXT,
    rec_count INTEGER DEFAULT 1,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 每日宏观摘要
CREATE TABLE IF NOT EXISTS macro_news (
    date TEXT PRIMARY KEY,
    news_summary TEXT,
    top_gainers TEXT,
    top_losers TEXT,
    etf_net_flow TEXT,
    flow_json TEXT,
    context_json TEXT
);

-- 通用元数据（键值对）
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- 股票→申万二级行业映射
CREATE TABLE IF NOT EXISTS stock_industry_map (
    stock_code TEXT PRIMARY KEY,
    industry_code TEXT,
    industry_name TEXT,
    update_date TEXT
);

-- 赛道选择记录（进化闭环用）
CREATE TABLE IF NOT EXISTS sector_selections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    recommend_log_id INTEGER,
    recommended_sectors TEXT,
    risk_sectors TEXT,
    sector_reasoning TEXT,
    regime_label TEXT,
    key_news_snippet TEXT,
    used_insight_ids TEXT,
    outcome TEXT DEFAULT '待定',
    outcome_date TEXT,
    outcome_note TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    -- P1-5 否决反事实度量：量化池内全部候选赛道（JSON 数组，结算时逐赛道回看 20 日收益）
    pool_sectors TEXT,
    -- P1-5 池内各赛道代表基金 20 日收益（JSON：{赛道: 收益}，结算时回填，度量否决正确率）
    pool_outcomes TEXT
);

-- 监控事件记录
CREATE TABLE IF NOT EXISTS monitor_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    signal TEXT NOT NULL,
    trigger_trailing BOOLEAN DEFAULT 0,
    trigger_drift BOOLEAN DEFAULT 0,
    trigger_sector_adv BOOLEAN DEFAULT 0,
    logic_verdict TEXT,
    sector_risk BOOLEAN,
    holding_risk BOOLEAN,
    detail TEXT,
    recommend_log_id INTEGER,
    is_stale BOOLEAN DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 模型预测序列（阶段二：R1 模型序列退出的跨日确认期数据源）
CREATE TABLE IF NOT EXISTS monitor_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    score REAL NOT NULL,
    model_version TEXT,
    UNIQUE (code, date)
);

-- LLM 决策审计（P0-3）：prompt 输入快照 + 原始输出 + 解析结果，可复现排查
CREATE TABLE IF NOT EXISTS llm_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    caller TEXT,
    prompt_hash TEXT,
    prompt_preview TEXT,
    raw_output TEXT,
    parsed_result TEXT,
    duration_ms INTEGER,
    tokens INTEGER,
    ok INTEGER DEFAULT 1
);

-- 终选定论质量观测（P1-4 回滚后收敛为裁决损耗扩展，见 quality.decision_gap_best）：
-- LLM 终选 vs 候选池最优的 20 日收益差，随质量度量月度入库，不再单独建表。

-- 进化洞察（替代旧 evolution_rules）
CREATE TABLE IF NOT EXISTS evolution_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    insight TEXT NOT NULL,
    insight_type TEXT NOT NULL,
    source_ids TEXT,
    confidence REAL DEFAULT 1.0,
    created_date TEXT NOT NULL,
    last_applied_date TEXT,
    apply_count INTEGER DEFAULT 0,
    active INTEGER DEFAULT 1,
    -- P3-11 洞察结构化：可选的可判定前置条件（JSON，如 {"condition": "重仓第一行业∈回避赛道", "action": "评分归零"}）
    condition TEXT
);

-- 推荐质量度量（月度进化闭环）
CREATE TABLE IF NOT EXISTS quality_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    computed_date TEXT NOT NULL,
    period_start TEXT,
    period_end TEXT,
    ic REAL,
    excess_win_rate REAL,
    mean_excess REAL,
    cum_excess REAL,
    profit_rate REAL,
    mean_abs_ret REAL,
    payoff_ratio REAL,
    sample_count INTEGER,
    decision_loss REAL,
    decision_gap_best REAL,
    points_json TEXT
);

-- 同一统计区间只保留一次度量（重复运行 run_evolve 幂等）
CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_metrics_period
    ON quality_metrics (period_start, period_end);

-- 空推荐日历史（每天一条）
CREATE TABLE IF NOT EXISTS empty_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    reasoning TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 行业板块每日快照（全板块涨跌+主力净流入，量化定池面板数据源；覆盖式）
CREATE TABLE IF NOT EXISTS sector_daily_snapshot (
    date TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    pct_chg REAL,
    net_flow REAL,
    PRIMARY KEY (date, sector_code)
);

-- 数据拉取失败记录（全量/增量下载失败追踪与重试恢复）
CREATE TABLE IF NOT EXISTS data_fetch_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetch_type TEXT NOT NULL,
    target TEXT NOT NULL,
    stage TEXT DEFAULT '',
    error TEXT,
    attempts INTEGER DEFAULT 1,
    status TEXT DEFAULT 'failed',
    first_failed_at TEXT DEFAULT (datetime('now')),
    last_failed_at TEXT,
    recovered_at TEXT,
    UNIQUE (fetch_type, target)
);