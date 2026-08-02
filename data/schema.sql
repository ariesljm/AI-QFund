-- AI-QFund 数据库 Schema（SQLite WAL 模式）
-- 单一真相源：data_store._init_schema 读取此文件，_migrate 补充列变更

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

-- 分红记录
CREATE TABLE IF NOT EXISTS fund_dividend (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    dividend_per_unit REAL,
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
    ma60 REAL,
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
    outcome TEXT DEFAULT '待定',
    outcome_date TEXT,
    outcome_note TEXT,
    created_at TEXT DEFAULT (datetime('now'))
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
    created_at TEXT DEFAULT (datetime('now'))
);

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
    active INTEGER DEFAULT 1
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
    sample_count INTEGER,
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