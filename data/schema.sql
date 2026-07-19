-- AI-QFund 数据库 Schema（SQLite WAL 模式）
-- 由 DEVELOPMENT_PLAN.md Phase 0 定义

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
    etf_flow_slope_5d REAL,
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
    regime TEXT,
    buy_reason TEXT,
    sell_reason TEXT,
    status TEXT DEFAULT 'HOLD',
    exit_date TEXT,
    highest_nav REAL,
    created_at TEXT DEFAULT (datetime('now'))
);

-- 进化规则
CREATE TABLE IF NOT EXISTS evolution_rules (
    id TEXT PRIMARY KEY,
    rule TEXT NOT NULL,
    source_trade_id INTEGER,
    created_date TEXT NOT NULL,
    active INTEGER DEFAULT 1
);

-- 每日宏观摘要
CREATE TABLE IF NOT EXISTS macro_news (
    date TEXT PRIMARY KEY,
    news_summary TEXT,
    top_gainers TEXT,
    top_losers TEXT,
    etf_net_flow TEXT
);

-- 通用元数据（键值对），记录各类数据的最近更新时间等
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
