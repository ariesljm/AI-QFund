-- AI-QFund 数据库 Schema（SQLite WAL 模式）
-- 单一真相源：app/database._init_schema 读取此文件，_migrate 只做历史列迁移与缺表兜底

-- 基金基本信息
CREATE TABLE IF NOT EXISTS fund_basic (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    company TEXT,
    is_buyable INTEGER DEFAULT 1,
    aum REAL,        -- 合并规模（元，票 06）
    shares REAL      -- 份额（份，票 06 的 AUM_surge 输入）
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
    -- 公告日（共识 Q15 的 PIT 口径：样本只允许 disclosure_date <= d）。
    -- 东财 jjcc 不含公告日期，故存的是保守估计（报告期 + 15 个工作日）
    disclosure_date TEXT,
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
    drawdown_60d REAL,
    reversal_20d REAL,
    mom_5d REAL,
    mom_60d REAL,
    vol_20d REAL,
    sharpe_60d REAL,
    sortino_60d REAL,
    ttr_60d REAL,
    style_r2 REAL DEFAULT 0,
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
    vetoed_json TEXT,
    -- 推荐来源路径：sector=赛道内选基（主路径），degrade=全市场 Top10 降级路径
    reco_path TEXT DEFAULT 'sector',
    -- 内部决策依据（P2-7 决策与文案解耦）：LLM 的 decision_logic，审计用，
    -- 不进展示文案（buy_reason 回归纯文案，不再拼否决/决策尾巴）
    decision_logic TEXT,
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
    context_json TEXT,
    -- 新闻条目实际归属日期（跨日回退时为 T-1），供 UI 区分展示；行主键仍是决策日期
    news_date TEXT
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

-- 个股估值日频（票 05）：重仓股加权 PE 分位 / PEG 匹配度的唯一数据源
CREATE TABLE IF NOT EXISTS stock_valuation_daily (
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    pe REAL,
    pb REAL,
    market_cap REAL,
    PRIMARY KEY (stock_code, date)
);

-- 个股日线（票 07）：前复权收盘价（搜狐 hisHq），供模块三虚拟组合比对
CREATE TABLE IF NOT EXISTS stock_daily (
    stock_code TEXT NOT NULL,
    date TEXT NOT NULL,
    close REAL,        -- 前复权收盘价
    PRIMARY KEY (stock_code, date)
);

-- 全市场初筛候选池（票 11）：Top30 落库 + 特征快照（审计与复盘用）
CREATE TABLE IF NOT EXISTS screen_candidates (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    score REAL NOT NULL,
    feature_snapshot TEXT,
    PRIMARY KEY (date, code)
);

-- 三级状态机跟踪对象（票 15 决策周期入口；object_type 留缝，ADR-0011）
CREATE TABLE IF NOT EXISTS tracked_states (
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    state TEXT NOT NULL,          -- HOLD / WATCH / EXIT
    date TEXT NOT NULL,           -- 最近转移日期
    signals_json TEXT,            -- 触发 signals 快照（审计可追溯）
    PRIMARY KEY (object_type, object_id)
);

-- 2.0 最终推荐 Top5（票 11 完整：审计后复合分定稿，Web/结算消费）
CREATE TABLE IF NOT EXISTS recommend_v2 (
    date TEXT NOT NULL,
    code TEXT NOT NULL,
    final_score REAL NOT NULL,
    audit_json TEXT,
    PRIMARY KEY (date, code)
);


-- 校准层信号记账（票 18 决策周期入口）：每路信号触发/结算，assess 消费历史
CREATE TABLE IF NOT EXISTS signal_outcomes (
    signal_id TEXT NOT NULL,
    ts TEXT NOT NULL,
    date TEXT,          -- 触发日
    outcome INTEGER,    -- NULL 待结算 / 0 未命中 / 1 命中
    PRIMARY KEY (signal_id, ts)
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
    points_json TEXT,
    -- 分口径度量（按 reco_path 分组）：{"sector": {...}, "degrade": {...}}
    by_path_json TEXT,
    -- 端到端 P&L（#2）：按实际退出日期扣赎回费后的净收益，对比 40 日理论收益
    e2e_profit_rate REAL,
    e2e_mean_ret REAL,
    e2e_payoff_ratio REAL,
    timing_contribution REAL,
    e2e_sample_count INTEGER,
    e2e_points_json TEXT,
    -- 体验指标（ticket 12）：推荐至退出的平均持仓自然日数与平均最大回撤
    e2e_mean_hold_days REAL,
    e2e_mean_max_drawdown REAL,
    -- 分桶赚钱率（模型校准观测 #1）：按预测分分桶的赚钱率/样本数，验证 L1 回归与胜率口径对齐
    by_score_bucket_json TEXT
);

-- 同一统计区间只保留一次度量（重复运行 run_evolve 幂等）
CREATE UNIQUE INDEX IF NOT EXISTS idx_quality_metrics_period
    ON quality_metrics (period_start, period_end);



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
-- 申购状态（T10 限购检测，降级方案：维护者手工维护清单，启动/日更导入）
CREATE TABLE IF NOT EXISTS purchase_restrictions (
    code TEXT PRIMARY KEY,
    status TEXT NOT NULL,          -- normal / limited / suspended
    daily_limit REAL,              -- 单日申购上限（元），NULL = 无限购（票 06）
    note TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);


