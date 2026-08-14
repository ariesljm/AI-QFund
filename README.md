# AI-QFund · 智能进化公募基金推荐与监控系统

[![CI](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml/badge.svg)](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml)

AI 驱动的基金量化投研系统：数据基座拉取全市场基金净值 → LightGBM 特征打分 → LLM 宏观选赛道与终审定论 → 虚拟池每日监控 → 月末进化沉淀教训。极简架构，本地 SQLite 存储，无高资源消耗框架。

> ⚠️ **免责声明**：本项目仅用于个人研究与学习，输出结果不构成任何投资建议。基金有风险，投资需谨慎。

---

## 功能特性

- **数据基座**：全市场 12K+ 基金列表、历史净值（真·增量更新）、沪深300 / 上证指数、季报重仓股、申万行业映射
- **量化特征**：Hurst 指数、20 日动量、卡玛比率、下行波动率、上涨/下跌捕获率、60 日乖离率、RBSA 行业暴露、大盘状态机（BULL/BEAR/NEUTRAL）
- **推荐引擎**：LightGBM 预测"未来 20 日**绝对收益**"（R1 目标：训练/验收/回测同口径）→ 赛道内排序（模型权重主导）→ LLM 综合宏观/持仓/新闻做终选定论与否决；全天候出手（不再以预测分 > 0 硬过滤）；判断当日无机会时优雅输出"今日无推荐"。终选定论质量由裁决损耗度量（选中 vs 候选池均值 + 对候选池最优的差距，月度入库）；regime 判定由量化代码直接给出（close vs MA60），LLM 只做赛道选择定性推理
- **监控引擎**：五道防线（EMA60 趋势 / RBSA 风格漂移 / 赛道锚点 / 板块优势 / 模型信号 / LLM 逻辑证伪）每日对 HOLD 持仓盯盘，输出持有/加仓/警惕/离场四类信号，触发即标记 EXIT；净值陈旧写独立数据告警（不计入信号升级链）。监控与推荐解耦：推荐失败不断档持仓盯盘。R4 逻辑证伪失败自动降级跳过（复核层不拖死规则层），多持仓并发预计算；判定阈值量化（暴露下降>15pp/权重腰斩/重仓≥3只退出）
- **进化引擎**：每日结算待定推荐（满 20 日窗口即结算）+ 质量度量上月（幂等覆盖收敛）+ 月度重量活（LLM 教训沉淀 `evolution_insights` 回流选赛道与定论提示词（新洞察 0.5 置信度试用期起步，批次内近似洞察去重）+ GA 排序权重寻优（fitness=赚钱胜率×2+期望收益，时间种子随机化）+ 置信度衰减 + 否决反事实/空仓率监控）驱动闭环；模型每周重训（训练与推理解耦：重训 7 天一次，每日推理用已保存模型 + 当天最新特征）
- **Web 面板**：FastAPI 单页仪表板，实时展示推荐、持仓、宏观摘要、管线日志，支持手动触发管线与数据管理

## 系统架构

```
    数据基座                 推荐引擎                 监控引擎
┌────────────────┐   ┌───────────────────┐   ┌─────────────────┐
│ 基金/净值/指数   │→│ LLM 宏观选赛道      │   │ 追踪止损         │
│ 重仓股/行业映射  │   │ LightGBM 赛道内排序 │→│ 风格漂移         │
│ 特征/RBSA       │   │ LLM 终选定论/否决  │   │ 板块优势         │
└────────────────┘   └───────────────────┘   │ 模型信号         │
       ↑                                      │ LLM 逻辑证伪     │
  ┌────────┐    ┌───────────────────┐        └────────┬────────┘
  │ SQLite │◄───│  进化引擎（每日结算/月度重量活） │◄────────────────┘
  └────────┘    └───────────────────┘
```
推荐漏斗：**LLM 选赛道（宏观） → LightGBM 赛道内排序 → LLM 终选定论**

LightGBM 预测目标是 `R_fund(t+20)`（未来 20 日**绝对收益**，训练目标为绝对收益 + 市场状态列，让模型自行感知 beta）；推荐按预测收益横截面取 TopN，全天候出手，风险由监控防线与极端止损兜底。

## 技术栈

| 组件 | 选型 |
|------|------|
| 语言 | Python ≥ 3.11 |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| 数据库 | SQLite（WAL 模式，16 张表） |
| HTTP 客户端 | httpx（同步/异步统一；push2 域名 TLS 指纹伪装 + 三级降级） |
| 量化计算 | NumPy / LightGBM |
| LLM 接口 | OpenAI 兼容 API（base_url / api_key / model 可配置） |
| Web | FastAPI + Jinja2（单页仪表板） |
| 部署 | Docker / docker-compose / GitHub Actions |

### Web 面板前端构建

前端静态资源（`app/web/static/`）已本地化：**运行时零 Node / 零外网 CDN 依赖**——
Tailwind 产物 `output.css`、自托管字体与 `app.js` 均已提交仓库，Docker 镜像无需 Node 即可运行。

修改模板或 JS 中的类名后需重新生成 CSS（前端构建在 `web/` 子项目）：

```bash
cd web       # 前端构建子项目（package.json / tailwind.config.cjs）
npm install   # 首次（node_modules 不入库）
npm run build:css   # tailwindcss -i ../app/web/static/input.css -o ../app/web/static/output.css --minify
```

实时指数经后端 `/api/indices` 代理（15s 缓存），行情源不可用或非交易时段自动降级为数据库最近收盘价（沪深300 / 上证指数）。

## 快速开始（本地）

```bash
# 1. 安装 uv（若未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步依赖
uv sync

# 3. 初始化配置
cp config/settings.toml.example config/settings.toml
#    编辑：填入 LLM 的 base_url / api_key / model

# 4. 初始化数据库
uv run python scripts/init_db.py

# 5. 启动 Web 服务（首次访问可手动触发管线）
uv run python -m app.web.app
```

访问 http://localhost:9123 。

### 其他入口

```bash
uv run python -m app.data.foundation            # 数据基座（基金/净值/指数/持仓/特征）
uv run python -m app.data.foundation --step 2   # 仅更新净值
uv run python -c "from app.pipeline import run; run()"   # 全流程管线（数据基座→推荐→监控→进化）
uv run pytest tests/                            # 运行测试
```

## Docker 部署

```bash
# 从 GitHub Container Registry 拉取并启动
docker compose pull
docker compose up -d
```

docker-compose 默认配置：

- 端口 `9123`
- 数据卷挂载：`./data` → `/app/data`、`config/settings.toml`（**宿主机必须先创建该文件**，否则 Docker 会把它建为目录导致配置加载失败）、`./models` → `/app/models`
- 环境变量：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `SCHEDULER_HOUR` / `SCHEDULER_MINUTE` / `ENABLE_SCHEDULER` / `WEB_PORT`
- 每日定时推荐（默认 08:00）

> **首次部署 = 数据基座自举**：空数据卷启动后，全流程需依次完成 基金列表 → 净值全量（约 1,140 万条，数小时）→ 持仓（约 1.2 万只基金）→ 行业映射 → RBSA → 特征 → 模型训练，**整个过程可能跨多次调度/手动触发**（增量幂等，可续跑）。
> 自举完成前，推荐前置门控会**自动自愈**：持仓为空时自动触发数据基座 Step 4（持仓+行业映射），持仓已就绪但行业映射缺失时自动增量补拉（修复持仓成功但行业映射失败导致的 7 天拦截期）；自愈后仍空才拦截（UI 日志提示"持仓/行业映射为空"），且自愈失败后 24 小时冷却，避免限流接口被反复重试。**推进方式**：触发任意一次全流程/推荐即可自动补齐；期间可查 `data_fetch_failures` 表确认失败接口（云服务器访问东财 F10/push2 易被反爬限流，系统已内置三级降级与熔断）。
> 自举完成后（`fund_holdings` 与 `stock_industry_map` 均非空），推荐/监控/进化正常运转；新环境首次会自动训练 LightGBM 模型（依赖数据量，耗时较长属正常）。

## 配置说明（`config/settings.toml`）

```toml
[llm]          # OpenAI 兼容接口
base_url = "http://localhost:11434/v1"
api_key = "your-api-key-here"
model = "gpt-4o-mini"

[scheduler]    # 每日定时推荐（hour 为空表示关闭）
hour = 8
minute = 0

[logging]      # system_logs 滚动保留：先按时间删超期日志，再按行数兜底（双条件）
db_retention_days = 30
db_max_rows = 20000

[web]          # Web 服务
port = 9123
settings_password = ""   # 设置页访问密码（留空不设）
```

> **LLM 代理可用性说明**：聚合代理（omniroute 等）会间歇性返回 HTTP 500 或"200 但 `content` 为空"（多为「纯思考」模型把输出预算耗在 `reasoning_content` 上，或后端抖动），二者都可能持续数十秒。`call_llm` 已内置 3 次重试 + 「空内容视为失败重试」，监控逻辑证伪失败时保守降级为 HOLD。若某模型反复失败，优先检查代理/网关健康（或换用能直接返回 `content` 的对话模型），而非视为代码 bug。

## 项目结构

```
AI-QFund/
├── app/                        # 主应用包
│   ├── config.py               # 配置管理（TOML + 环境变量 + meta 覆盖）
│   ├── database.py             # SQLite 连接管理、schema 初始化、迁移（单一连接 seam）
│   ├── domain.py               # 领域常量与纯函数单一来源（信号/窗口/状态机/排序权重 schema）
│   ├── model.py                # 模型生命周期（训练/加载/打分/重训判定单一 module）
│   ├── pipeline.py             # 管线编排（数据基座 → 推荐 → 监控 → 进化）
│   ├── repo/                   # 数据仓库层（统一数据 seam）
│   │   ├── base.py             # 底层数据读写（净值/特征/指数/持仓/行业映射）
│   │   └── decision.py         # 推荐决策域读写（推荐记录/赛道选择/监控事件/进化洞察/质量度量）
│   ├── data/                   # 数据基座子包
│   │   ├── foundation.py       # 数据基座编排与抓取逻辑
│   │   ├── fetchers.py         # 统一 HTTP 请求层（TLS 伪装 + 三级降级）
│   │   ├── nav.py              # 净值抓取（全量下载 / 真·增量）
│   │   ├── store.py            # 数据写入层（失败/冷却/恢复状态机）
│   │   └── ingest.py           # 异步批量下载 harness（并发批次 + 熔断）
│   ├── features/               # 特征计算子包
│   │   ├── calculator.py       # Hurst / 动量 / 卡玛 / RBSA / 大盘状态机 / 统一打分
│   │   └── sector.py           # 申万行业板块代码过滤
│   ├── engine/                 # 引擎子包
│   │   ├── recommend.py        # 推荐引擎（LightGBM + LLM 否决）
│   │   ├── monitor.py          # 虚拟池监控引擎（五道防线）
│   │   ├── evolve.py           # 进化引擎（错题本规则生成 + GA 调节）
│   │   ├── ga.py               # 排序权重遗传算法寻优
│   │   ├── quality.py          # 推荐质量度量（赚钱胜率/期望绝对收益/盈亏比）
│   │   └── valuation.py        # 组合估值（收益/夏普/回撤/超额曲线）
│   ├── llm/                    # LLM 交互子包
│   │   ├── client.py           # 统一 LLM 调用接口（含重试）
│   │   ├── prompts.py          # Prompt 模板集中管理
│   │   ├── macro_agent.py      # 宏观分析 agent（选赛道）
│   │   └── context.py          # 持仓/基金上下文文本构建
│   ├── web/                    # Web 服务
│   │   ├── app.py              # FastAPI 应用
│   │   ├── static/             # 前端静态资源（Tailwind 产物 / 自托管字体）
│   │   └── templates/          # Jinja2 模板
│   └── utils/
│       ├── log.py              # 日志工具（JSON / 结构化）
│       └── trading_calendar.py # A 股交易日历
├── backtest/                     # 回测研究脚本（python -m backtest.xxx 运行）
├── scripts/                      # 入口脚本
│   └── init_db.py                # 建库脚本
├── config/                     # 配置模板
├── data/                       # SQLite 数据库 + schema.sql
├── models/                     # LightGBM 模型（运行时生成）
├── tests/                      # 测试
├── web/                          # 前端构建子项目（package.json / tailwind.config.cjs / node_modules）
└── docs/                       # 开发文档
```

## 数据来源

- 天天基金（fund.eastmoney.com）：基金列表、历史净值、季报重仓股
- 东方财富（push2 系列接口）：板块行情、行业映射、ETF 资金流
- 新浪财经：沪深300 / 上证指数日线
- 财联社：财经快讯（宏观新闻）

所有数据仅用于学习研究，版权归原始数据源所有。

## License

本项目仅供个人学习研究使用。
