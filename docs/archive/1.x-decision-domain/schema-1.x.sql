CREATE TABLE data_fetch_failures (
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

CREATE TABLE empty_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    reasoning TEXT,
    created_at TEXT DEFAULT (datetime('now'))
, reason_type TEXT DEFAULT 'no_opportunity');

CREATE TABLE evolution_insights (
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

CREATE TABLE fund_basic (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    company TEXT,
    is_buyable INTEGER DEFAULT 1
);

CREATE TABLE fund_features (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    regime TEXT,
    hurst_60d REAL,
    momentum_20d REAL,
    calmar REAL,
    downside_vol REAL,
    capture_up REAL,
    capture_down REAL,
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
    rbsa_weight_3 REAL DEFAULT 0, sharpe_60d REAL, sortino_60d REAL, ttr_60d REAL, style_r2 REAL DEFAULT 0,
    PRIMARY KEY (code, date)
);

CREATE TABLE fund_holdings (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    weight REAL,
    PRIMARY KEY (code, report_date, stock_code)
);

CREATE TABLE fund_nav (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    unit_nav REAL,
    cum_nav REAL,
    equity_return REAL,
    unit_dividend REAL,
    PRIMARY KEY (code, date)
);

CREATE TABLE fund_style_track (
    fund_code TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    industry_1 TEXT,
    weight_1 REAL,
    industry_2 TEXT,
    weight_2 REAL,
    r_squared REAL,
    PRIMARY KEY (fund_code, trade_date)
);

CREATE TABLE index_daily (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    open REAL,
    high REAL,
    low REAL,
    close REAL,
    volume REAL,
    ma60 REAL, ema60 REAL,
    PRIMARY KEY (code, date)
);

CREATE TABLE llm_audit (
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

CREATE TABLE macro_news (
    date TEXT PRIMARY KEY,
    news_summary TEXT,
    top_gainers TEXT,
    top_losers TEXT,
    etf_net_flow TEXT,
    flow_json TEXT,
    context_json TEXT
, news_date TEXT);

CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE monitor_events (
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

CREATE TABLE monitor_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    score REAL NOT NULL,
    model_version TEXT,
    UNIQUE (code, date)
);

CREATE TABLE purchase_restrictions (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- normal / limited / suspended
    note TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE quality_metrics (
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
, by_path_json TEXT, e2e_profit_rate REAL, e2e_mean_ret REAL, e2e_payoff_ratio REAL, timing_contribution REAL, e2e_sample_count INTEGER, e2e_points_json TEXT, by_score_bucket_json TEXT, e2e_mean_hold_days REAL, e2e_mean_max_drawdown REAL);

CREATE TABLE recommend_log (
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
    created_at TEXT DEFAULT (datetime('now'))
, rec_count INTEGER DEFAULT 1, vetoed_json TEXT, reco_path TEXT DEFAULT 'sector', decision_logic TEXT);

CREATE TABLE sector_daily_snapshot (
    date TEXT NOT NULL,
    sector_code TEXT NOT NULL,
    sector_name TEXT NOT NULL,
    pct_chg REAL,
    net_flow REAL,
    PRIMARY KEY (date, sector_code)
);

CREATE TABLE sector_selections (
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

CREATE TABLE sqlite_sequence(name,seq);

CREATE TABLE stock_industry_map (
    stock_code TEXT PRIMARY KEY,
    industry_code TEXT,
    industry_name TEXT,
    update_date TEXT
);

CREATE TABLE system_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    level TEXT NOT NULL,
    logger TEXT,
    event TEXT,
    message TEXT,
    correlation_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX idx_quality_metrics_period
    ON quality_metrics (period_start, period_end);

