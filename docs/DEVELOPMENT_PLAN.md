# AI-QFund 项目开发计划

> 基于 `docs/README.md` 与 grilling session 结论生成。按数据流顺序分 6 个 Phase。

---

## 前置结论（来自 Grill）

| 决策点 | 结论 |
|--------|------|
| 开发策略 | **数据流顺序**：data_foundation → recommend → monitor → evolve → web |
| 基金池范围 | 全市场，剔除：货币型/债券类/封闭/偏债/QDII/FOF/不可申购 |
| 数据源 | 东方财富 `pingzhongdata/{code}.js`（历史净值）+ 天天基金 API（基金列表/排行/搜索）|
| LLM 接口 | OpenAI 兼容 API（通用格式，模型名通过 `config/settings.toml` 配置）|
| 调度方式 | 开发期：手动逐模块执行；生产期：Linux Crontab |
| 回测 | 不需要 |
| Web 深度 | 完整多页仪表板（FastAPI 只读 API + 前端，框架待定，需求见 README 第 9 章）|
| DB Schema | 由 AI 根据业务描述设计 |
| Python 版本 | 3.11 + uv 已安装 |

---

## Phase 0：项目骨架与数据接口验证

**目标**：建好项目骨架，调通 3 条核心数据接口，确认数据链路可用。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 0.1 | `uv init` 初始化项目，配置 `pyproject.toml` | 依赖：`requests`, `numpy`, `scipy`, `lightgbm`, `fastapi`, `jinja2`, `openai`（前端框架与图表库待 Phase 5 确定） |
| 0.2 | 创建目录结构与空壳文件 | `config/settings.toml`, `data/`, `web/`, `data_foundation.py`, `recommend.py`, `monitor.py`, `evolve.py` |
| 0.3 | 编写 `config/settings.toml` 模板 | API base_url、LLM 配置、阈值参数、调度时间 |
| 0.4 | **接口验证脚本** `probe_apis.py` | 验证 3 条核心接口能否成功请求并解析 |
| 0.5 | 设计并创建 SQLite 表结构 `data/schema.sql` | 所有表 DDL（详见下文 Schema） |

### 核心数据接口（已验证可用）

| 用途 | 接口 | 返回 |
|------|------|------|
| 基金全量列表 | 天天基金 API `fundMNNetNewList`（按类型分页拉取） | 基金代码、名称、类型 |
| 单基历史净值 | `http://fund.eastmoney.com/pingzhongdata/{code}.js` | 单位净值、累计净值、分红记录（JS var 格式，正则提取）|
| 实时估值 | `http://fundgz.1234567.com.cn/js/{code}.js` | 当日估算净值、估算涨幅（JSONP 格式）|
| 沪深300指数 | 东方财富或新浪财经指数 API | OHLCV 日线数据 |

### DB Schema（SQLite WAL 模式）

```sql
-- 基金基本信息
CREATE TABLE fund_basic (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL,
    company TEXT,
    is_buyable INTEGER DEFAULT 1
);

-- 历史净值
CREATE TABLE fund_nav (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    unit_nav REAL,
    cum_nav REAL,
    equity_return REAL,
    unit_dividend REAL,
    PRIMARY KEY (code, date)
);

-- 分红记录
CREATE TABLE fund_dividend (
    code TEXT NOT NULL,
    date TEXT NOT NULL,
    dividend_per_unit REAL,
    PRIMARY KEY (code, date)
);

-- 宽基指数日线
CREATE TABLE index_daily (
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
CREATE TABLE fund_holdings (
    code TEXT NOT NULL,
    report_date TEXT NOT NULL,
    stock_code TEXT NOT NULL,
    stock_name TEXT,
    weight REAL,
    PRIMARY KEY (code, report_date, stock_code)
);

-- 特征计算结果
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
    bias_60d REAL,
    rbsa_industry_1 TEXT,
    rbsa_weight_1 REAL,
    etf_flow_slope_5d REAL,
    PRIMARY KEY (code, date)
);

-- 推荐记录
CREATE TABLE recommend_log (
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
CREATE TABLE evolution_rules (
    id TEXT PRIMARY KEY,
    rule TEXT NOT NULL,
    source_trade_id INTEGER,
    created_date TEXT NOT NULL,
    active INTEGER DEFAULT 1
);

-- 每日宏观摘要
CREATE TABLE macro_news (
    date TEXT PRIMARY KEY,
    news_summary TEXT,
    top_gainers TEXT,
    top_losers TEXT,
    etf_net_flow TEXT
);
```

### 验证
- `probe_apis.py` 能获取到 ≥100 条基金列表数据、≥500 天历史净值、当日沪深300收盘价。
- SQLite 数据库文件创建成功，所有表可写入测试数据。

---

## Phase 1：数据基座与特征工程 (`data_foundation.py`)

**目标**：实现完整的"凌晨拉取→增量更新→宏观状态机→特征计算→特征入库"链路。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 1.1 | **基金列表获取与过滤** | 从天天基金 API 拉取全量列表，按规则剔除不可投类型，写入 `fund_basic` |
| 1.2 | **净值增量更新** | 逐基检查本地 `fund_nav` 最新日期，仅拉取缺失的 T-1/T 数据，写入 `fund_nav`；使用累计净值 (cumulative NAV) |
| 1.3 | **宏观指数获取** | 拉取沪深300日线，写入 `index_daily`，计算 EMA60（冷启动 250 条，之后增量续算，ma60 列存 EMA 值） |
| 1.4 | **大盘状态机 (Regime Switch)** | 基于收盘价 vs EMA60 判断 BULL/BEAR，写入当日状态标记 |
| 1.5 | **重仓股数据获取** | 从天天基金 f10 (`FundArchivesDatas.aspx?type=jjcc`) 拉取最新季报重仓股（代码+名称+占净值比例），写入 `fund_holdings` |
| 1.6 | **RBSA 行业暴露** | 以重仓股为约束，用滚动回归计算基金前3大隐形行业暴露度 |
| 1.7 | **特征计算（6 类特征）** | Hurst 指数(60日)、绝对动量(20日)、卡玛比率、下行波动率、向上/向下捕获率、乖离率 BIAS、ETF资金流斜率 |
| 1.8 | **特征入库** | 将计算结果写入 `fund_features` 表 |

> **决策：持仓表保留历史季报快照，不做覆盖删除。**
> `fund_holdings` 按 `(code, report_date, stock_code)` 存每期季报快照，累积保留全部历史季度。
> 历史季报是 Phase 4 进化引擎的数据基础：风格漂移检测（Q1→Q2→Q3 重仓/行业变化）、
> 持仓集中度与换手率趋势、进化规则回测均依赖多期序列，删除历史会自断这条数据腿。
> 下游取用规则：需要「最新持仓」用 `report_date = (SELECT MAX(report_date) FROM fund_holdings WHERE code=?)`；
> 需要「风格对比」则取多期。禁止用 `WHERE code=?` 直接全捞（会混用多季度）。

### 关键函数签名

```python
def fetch_fund_list() -> list[dict]:
    """获取并过滤基金列表，剔除货币/债券/QDII等不可投类型"""

def fetch_fund_nav(code: str, start_date: str) -> list[dict]:
    """增量拉取单只基金历史净值（累计净值）"""

def fetch_index_daily(code: str) -> list[dict]:
    """获取沪深300指数日线数据"""

def calc_regime(index_df: DataFrame) -> str:
    """返回 "BULL" 或 "BEAR"，基于指数 vs MA60"""

def calc_rbsa(holdings: DataFrame) -> dict:
    """计算 RBSA 行业暴露度，返回前3大行业及权重"""

def calc_hurst(nav_series: Series) -> float:
    """计算60日 Hurst 指数"""

def calc_features(code: str, regime: str) -> dict:
    """全特征计算入口，返回单只基金当日特征字典"""
```

### 验证
- 基金列表过滤后数量与类型分布合理（无货币/债券/QDII 残留）。
- 任选 3 只基金，累计净值序列连续无断崖（验证未误用单位净值）。
- Regime 状态值与沪深300 vs MA60 实际位置一致。

---

## Phase 2：推荐引擎 (`recommend.py`)

**目标**：实现"LightGBM 打分 Top 10 → LLM 顺位否决 → 输出唯一推荐入库"的完整漏斗。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 2.1 | **LightGBM 标注数据准备** | 从 `fund_nav` 计算 Y = R_fund(t+20) - R_hs300(t+20)，构建训练集 |
| 2.2 | **LightGBM 模型训练** | 用当前特征 X 预测 Y，输出排序打分；保存模型文件 |
| 2.3 | **Top 10 筛选** | 对所有可投基金打分，取前 10，剔除异常值 |
| 2.4 | **宏观摘要获取** | 获取当日财经新闻摘要、领涨/领跌行业、ETF 净流入（已实现：东财板块排行+快讯实时抓取，写入 `macro_news` 表） |
| 2.5 | **LLM 顺位否决** | 构造 Prompt（系统状态 + 宏观摘要 + Top 10 特征 → 逐位验证否决），调用 OpenAI 兼容 API |
| 2.6 | **推荐入库** | 将最终选定基金写入 `recommend_log`（含排名、打分、买入理由、否决记录） |

### 目标函数

```
Y = R_fund(t+20) - R_hs300(t+20)
```

即未来 20 日相对沪深300的超额收益 Alpha，防止模型在熊市推荐高波动垃圾基。

### LLM Prompt 模板

```
【系统状态与规则】
大盘状态: {BULL/BEAR}
策略侧重: {BULL → 赫斯特指数/向上捕获率/动量因子, BEAR → 卡玛比率/向下捕获率/BIAS超跌反弹}
核心宪法（进化规则）:
  - {rule_1}
  - {rule_2}
  ...

【今日宏观摘要与资金流向】
{当日财经新闻摘要}
领涨行业: {行业1}, {行业2}, ...
领跌行业: {行业1}, {行业2}, ...
ETF净流入: {行业ETF名称: 净流入份额}

【候选名单（按超额收益动能排序）】
第1名: {code} {name} | RBSA行业: {行业1}:{权重1}, {行业2}:{权重2} | 卡玛: {x} | Hurst: {x} | 20日动量: {x}%
第2名: ...
...
第10名: ...

【任务指令】
从第1名开始向下逐位验证：
1. 其重仓行业是否符合当日宏观资金流向？
2. 是否存在明确的政策利空或行业性风险？
3. 是否违背核心宪法中的任何一条？
若验证通过（无利空+行业符合），立即选定该基金并停止。
若有明确利空或违背宪法，行使否决权并记录理由，继续验证下一位。

请严格输出以下JSON格式：
{
  "selected_code": "选中的基金代码",
  "selected_name": "选中的基金名称",
  "reason": "选定理由",
  "vetoed": [
    {"code": "被否决代码", "name": "被否决名称", "reason": "否决理由"}
  ]
}
```

### 关键函数签名

```python
def prepare_lgb_training_data() -> tuple[DataFrame, Series]:
    """准备 LightGBM 训练集：X=特征, Y=未来20日超额收益"""

def train_lgb_model(X: DataFrame, y: Series) -> lgb.Booster:
    """训练 LightGBM 模型并保存"""

def rank_funds(model: lgb.Booster) -> list[dict]:
    """对所有可投基金打分并返回 Top 10"""

def llm_veto(candidates: list[dict], regime: str, macro: dict, rules: list) -> dict:
    """调用 LLM 执行顺位否决，返回选定基金和否决记录"""

def run_recommendation() -> None:
    """推荐引擎主入口：打分→否决→入库"""
```

### 验证
- 模型打分结果与特征值逻辑自洽（高动量→高分）。
- 调用 LLM 输出格式可解析为 JSON。
- `recommend_log` 每天最多一条新记录。

---

## Phase 3：虚拟池监控引擎 (`monitor.py`)

**目标**：对 HOLD 状态的基金执行三道防线扫描，触发 EXIT 平仓。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 3.1 | **追踪止损（第一防线）** | 读取买入后最高累计净值 `highest_nav`，计算 ATR(14)，当前净值回撤超 2×ATR 标记 EXIT |
| 3.2 | **风格漂移监测（第二防线）** | 定期重算 RBSA，若第一大行业权重降幅 > 15%，标记 EXIT |
| 3.3 | **LLM 逻辑证伪（第三防线）** | 传入 `buy_reason` + 当日新闻，调用 LLM 判定逻辑链是否断裂 |
| 3.4 | **平仓入库** | 更新 `recommend_log` 状态为 EXIT，记录 `sell_reason` 和 `exit_date` |

### 三道防线触发条件

```
防线1 - 追踪止损:
  highest_nav = 买入后累计净值最高值
  atr_14 = ATR(14)  # 14日平均真实波幅
  current_nav = 当前累计净值
  触发: highest_nav - current_nav > 2 × atr_14

防线2 - 风格漂移:
  init_weight = 买入时 RBSA 第一大行业权重
  current_weight = 当前 RBSA 第一大行业权重
  触发: (init_weight - current_weight) > 0.15

防线3 - LLM 逻辑证伪:
  输入: buy_reason (买入时的逻辑) + 今日新闻摘要
  判定: "逻辑维持" / "逻辑断裂"
  触发: LLM 判定为"逻辑断裂"
```

### 关键函数签名

```python
def update_highest_nav(code: str) -> float:
    """更新并返回基金买入后的最高累计净值"""

def calc_atr(nav_series: Series, period: int = 14) -> float:
    """计算 ATR(平均真实波幅)"""

def check_trailing_stop(code: str, highest_nav: float, atr: float) -> bool:
    """第一防线：追踪止损检查"""

def check_style_drift(code: str) -> bool:
    """第二防线：RBSA 风格漂移检查"""

def check_logic_falsification(code: str, buy_reason: str, news: str) -> bool:
    """第三防线：LLM 逻辑证伪"""

def run_monitor() -> None:
    """监控引擎主入口：遍历所有 HOLD 基金，执行三道防线"""
```

### 验证
- 对一只历史数据中有明显回撤的基金，追踪止损能正确触发。
- RBSA 风格漂移阈值 15% 逻辑正确。
- EXIT 后 `recommend_log.status` 正确更新为 `EXIT`。

---

## Phase 4：进化引擎 (`evolve.py`)

**目标**：月末从亏损 EXIT 交易中提取教训，生成硬规则写入 `evolution_rules` 表（单一真相源，不额外维护 JSON 文件）。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 4.1 | **亏损交易筛选** | 查询当月 EXIT 且收益 < -5% 的交易记录 |
| 4.2 | **LLM 规则生成** | 以系统优化架构师视角，输入亏损交易的 `buy_reason` + `sell_reason`，输出一条硬规则 |
| 4.3 | **规则冲突检查** | 新规则不与已有规则矛盾（简单关键词重叠检测） |
| 4.4 | **规则追加** | 写入 `evolution_rules` 表（单一真相源，不维护额外 JSON 文件） |

### 进化规则表结构（`evolution_rules`）

规则以 `evolution_rules` 表为单一真相源，字段：`id`、`rule`、`source_trade_id`、`created_date`、`active`。示例：

- `R20260731_001`：若基金 RBSA 第一大暴露行业为房地产且行业 ETF 近 5 日净流出，则禁止推荐（来源交易 42）
- `R20260731_002`：BULL 环境下，Hurst 指数低于 0.45 的基金禁止推荐（趋势持续性不足，来源交易 57）

### LLM Meta-Prompt（系统优化架构师视角）

```
你是一位基金投资策略的系统优化架构师。

以下是一笔亏损超过-5%的交易记录：
买入日期: {buy_date}
卖出日期: {sell_date}
买入理由: {buy_reason}
卖出理由: {sell_reason}
实际亏损: {loss}%

请分析这笔亏损的根本原因，并输出一条具体的、可操作的硬规则，
用于避免未来类似亏损。规则应具体到量化条件，避免模糊表述。

请输出JSON格式：
{
  "rule": "具体的量化规则描述",
  "rationale": "为什么这条规则能避免类似亏损"
}
```

### 关键函数签名

```python
def query_losing_trades(month: str) -> list[dict]:
    """查询当月亏损超过-5%的 EXIT 交易"""

def generate_rule(trade: dict) -> dict:
    """调用 LLM 生成一条硬规则"""

def check_conflict(new_rule: str, existing_rules: list) -> bool:
    """检查新规则是否与已有规则冲突"""

def append_rule(rule: dict, source_trade_id: int) -> None:
    """追加规则到 evolution_rules 表（单一真相源，不额外维护 JSON 文件）"""

def run_evolve() -> None:
    """进化引擎主入口"""
```

### 验证
- 查询 `evolution_rules` 表确认新规则已追加。
- 生成的规则可被 `recommend.py` 的 Prompt 模板正确引用（作为 LLM 否决的"核心宪法"）。

---

## Phase 5：Web 展现层 (`web/`)

**目标**：完整多页仪表板，只读 SQLite，展示信号、持仓、进化规则。

### 任务

| # | 任务 | 说明 |
|---|------|------|
| 5.1 | **FastAPI 后端** | 只读 REST API 层，端点：`/api/recommendations`, `/api/holdings`, `/api/rules`, `/api/dashboard` |
| 5.2 | **仪表盘页面** | 当前大盘状态、最新推荐、HOLD 持仓一览 |
| 5.3 | **推荐历史页面** | 推荐记录表格 + 状态筛选（HOLD/EXIT）+ 收益统计 |
| 5.4 | **持仓追踪页面** | 当前 HOLD 的净值曲线图 + 三道防线状态指示 |
| 5.5 | **进化规则页面** | 进化规则列表，支持启用/停用 |
| 5.6 | **前端集成** | 整合所有页面为多页仪表板（框架待定，需求见 README 第 9 章） |

### 目录结构

```
web/
├── api.py              # FastAPI 路由定义
├── app.py              # 前端多页入口
├── db.py               # SQLite 只读连接层
├── pages/
│   ├── dashboard.py    # 仪表盘：大盘状态 + 最新推荐 + HOLD 概览
│   ├── history.py      # 推荐历史：表格 + 筛选 + 收益统计
│   ├── holdings.py     # 持仓追踪：净值曲线 + 三道防线状态
│   └── rules.py        # 进化规则：规则列表 + 启用/停用
└── templates/          # 模板（如需要服务端渲染）
```

### API 端点设计

```
GET /api/dashboard        → 大盘状态 + 最新推荐 + HOLD 数量
GET /api/recommendations  → 推荐历史列表（支持 status 过滤）
GET /api/holdings         → 当前 HOLD 的基金及其防线状态
GET /api/rules            → 进化规则列表
GET /api/features/{code}  → 单只基金的特征详情
```

### 验证
- FastAPI `/api/dashboard` 返回 JSON 格式正确。
- 仪表板各页面均可从 SQLite 正常加载数据。

---

## 项目最终目录结构

```
AI-QFund/
├── config/
│   └── settings.json            # 配置：API key、LLM配置、阈值参数、调度时间
├── data/
│   ├── qfund.db                 # SQLite 数据库（WAL 模式）
│   └── schema.sql               # 数据库 DDL
├── web/
│   ├── api.py                   # FastAPI REST 接口
│   ├── app.py                   # 前端多页仪表板入口
│   ├── db.py                    # SQLite 只读连接层
│   ├── pages/
│   │   ├── dashboard.py         # 仪表盘
│   │   ├── history.py           # 推荐历史
│   │   ├── holdings.py          # 持仓追踪
│   │   └── rules.py             # 进化规则
│   └── templates/               # 模板
├── data_foundation.py           # 数据基座与特征工程
├── recommend.py                 # 推荐引擎（LightGBM + LLM 否决）
├── monitor.py                   # 虚拟池监控引擎（三道防线）
├── evolve.py                    # 进化引擎（错题本规则生成）
├── probe_apis.py                # 接口验证脚本（开发期使用）
├── pyproject.toml               # 项目配置与依赖声明
└── docs/
    ├── README.md                # 项目说明文档
    └── DEVELOPMENT_PLAN.md      # 本开发计划
```

---

## 总体执行检查清单

| Phase | 任务 | 状态 | 验证方式 |
|-------|------|------|---------|
| **0** | 项目骨架与数据接口验证 | ✅ | `probe_apis.py` 通过，DB schema 创建成功 |
| **1** | 数据基座与特征工程 | ✅ | 基金列表过滤正确，净值序列连续无断崖，Regime 判断正确 |
| **2** | 推荐引擎 | ✅ | LightGBM 打分 + LLM 否决链路跑通，`recommend_log` 有数据；实时新闻源已接入 |
| **3** | 虚拟池监控引擎 | ✅ | 追踪止损/风格漂移/逻辑证伪三道防线均可触发，EXIT 入库正确 |
| **4** | 进化引擎 | ✅ | 亏损 EXIT → LLM 生成量化规则 → 入库 `evolution_rules` 表（单一真相源，不维护额外 JSON） |
| **5** | Web 展现层 | ⬜ | 多页仪表板正常运行，数据实时刷新 |
