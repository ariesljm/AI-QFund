# 📊 AI-QFund · 智能进化公募基金推荐与监控系统

[![CI](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml/badge.svg)](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml)

AI 驱动的基金量化投研系统：**数据基座 → LightGBM 特征打分 → LLM 宏观选赛道与终审定论 → 虚拟池每日监控 → 月末进化沉淀教训**。极简架构、本地 SQLite、无高资源消耗框架。

> ⚠️ **免责声明**：本项目仅用于个人研究与学习，输出结果不构成任何投资建议。基金有风险，投资需谨慎。

---

## ✨ 核心能力

### 1️⃣ 数据基座
全市场 12K+ 基金列表、历史净值（真·增量更新）、沪深300 / 上证指数、季报重仓股、申万行业映射、板块行情与财经快讯。

### 2️⃣ 推荐引擎
- LightGBM 预测未来 20 日**绝对收益** → 赛道内排序（模型权重主导）
- LLM 综合宏观 / 持仓 / 新闻做**终选定论与否决**，全天候出手；无机会时优雅输出"今日无推荐"
- regime（牛/熊/中性）由量化代码直接判定（close vs MA60），LLM 只做赛道定性推理

### 3️⃣ 监控引擎
**五道防线**每日对 HOLD 持仓盯盘：EMA60 趋势 / RBSA 风格漂移 / 赛道锚点 / 板块优势 / 模型信号 / LLM 逻辑证伪 → 输出持有 / 加仓 / 警惕 / 离场四类信号，触发即标记 EXIT。监控与推荐解耦，推荐失败不断档持仓盯盘。

### 4️⃣ 进化引擎
每日结算待定推荐（满 20 日窗口）+ 月度质量度量 + **LLM 教训沉淀**（`evolution_insights` 回流选赛道与定论提示词）+ **GA 排序权重寻优** + 置信度衰减 + 否决反事实监控；模型每周重训（训练与推理解耦，7 天重训一次，每日用已保存模型 + 最新特征）。

### 5️⃣ Web 面板
FastAPI 响应式单页仪表板（桌面 / 手机自适应）：AI 赛道报告与大盘状态卡、基金卡片、追踪监控分页列表、基金详情滑出面板（推荐理由、**推荐当日预测超额**、**多周期涨跌幅**、前十大重仓、净值走势、监控信号）、快讯轮播、实时指数、管线状态卡（含系统运行累计时间）、结构化系统日志，支持手动触发管线、数据管理与设置页密码保护；保留旧版 v1 模板（`index_v1.html`）。

## 🏗️ 系统架构

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

**推荐漏斗**：`LLM 选赛道（宏观） → LightGBM 赛道内排序 → LLM 终选定论`

## 🛠️ 技术栈

| 组件 | 选型 |
|------|------|
| 语言 / 包管理 | Python ≥ 3.11 / [uv](https://docs.astral.sh/uv/) |
| 数据库 | SQLite（WAL 模式，18 张表） |
| HTTP 客户端 | httpx（TLS 指纹伪装 + 三级降级） |
| 量化计算 | NumPy / LightGBM |
| LLM 接口 | OpenAI 兼容 API（base_url / api_key / model 可配置） |
| Web | FastAPI + Jinja2（单页仪表板） |
| 部署 | Docker / docker-compose / GitHub Actions（GHCR） |

> 前端静态资源已本地化：Tailwind 产物、自托管字体（Noto Sans/Serif SC、Material Symbols 按需加载分片）与 `app.js` 全部入库，**运行时零 Node / 零外网 CDN 依赖**。修改模板类名后需重新生成 CSS：`cd web && npm install && npm run build:css`。

## 🚀 快速开始（本地）

```bash
# 1. 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 同步依赖并初始化配置
uv sync
cp config/settings.toml.example config/settings.toml   # 填入 LLM base_url / api_key / model

# 3. 初始化数据库并启动（首次访问可手动触发管线）
uv run python scripts/init_db.py
uv run python -m app.web.app
```

访问 http://localhost:9123 。

**其他入口**：

```bash
uv run python -m app.data.foundation            # 数据基座（基金/净值/指数/持仓/特征）
uv run python -m app.data.foundation --step 2   # 仅更新净值
uv run python -c "from app.pipeline import run; run()"   # 全流程管线
uv run pytest tests/                            # 运行测试
```

## 🐳 Docker 部署

```bash
docker compose pull && docker compose up -d
```

- 端口 `9123`；挂载 `data/`、`models/` 与 `config/settings.toml`（**宿主机须先创建该文件**，否则 Docker 会建为目录）
- 环境变量：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `SCHEDULER_HOUR` / `SCHEDULER_MINUTE` / `ENABLE_SCHEDULER` / `WEB_PORT`
- 每日定时推荐：compose 默认 **14:30**（T-1 数据完整后跑），本地 `settings.toml` 默认 08:00

> **首次部署自举**：空数据卷启动后，全流程需依次完成 基金列表 → 净值全量（约 1,140 万条，数小时）→ 持仓（约 1.2 万只）→ 行业映射 → RBSA → 特征 → 模型训练，可跨多次调度续跑（增量幂等）。推荐前置门控自动自愈：持仓/行业映射缺失时自动补拉，仍空才拦截；自愈失败有 24 小时冷却。期间可查 `data_fetch_failures` 表定位被限流接口。

## ⚙️ 配置说明（`config/settings.toml`）

```toml
[llm]          # OpenAI 兼容接口
base_url = "http://localhost:11434/v1"
api_key = "your-api-key-here"
model = "gpt-4o-mini"

[scheduler]    # 每日定时推荐（hour 为空表示关闭）
hour = 8
minute = 0

[evolve]       # 进化引擎（预留段）

[logging]      # 文件/控制台日志级别 + system_logs 滚动保留（双条件兜底）
file_level = "INFO"
console_level = "INFO"
db_retention_days = 30
db_max_rows = 20000

[web]          # Web 服务
port = 9123
settings_password = ""   # 设置页访问密码（留空不设）
```

> **LLM 代理排障提示**：聚合代理可能间歇性返回 HTTP 500 或"200 但 `content` 为空"（纯思考模型把输出预算耗在 `reasoning_content` 上）。`call_llm` 已内置 3 次重试 + 空内容重试；监控逻辑证伪失败保守降级为 HOLD。反复失败时优先检查代理/网关健康，而非视为代码 bug。

## 📁 项目结构

```
AI-QFund/
├── app/                        # 主应用包
│   ├── config.py / database.py / domain.py / model.py / pipeline.py
│   ├── repo/                   # 数据仓库层：base（底层读写）/ decision（决策域）/ nav（净值序列）/ meta_keys
│   ├── data/                   # 数据基座：foundation / fetchers / nav / macro / store / ingest
│   ├── features/               # 特征计算：calculator（Hurst/动量/RBSA/状态机）/ sector
│   ├── engine/                 # 引擎：sector_pool（量化定池）/ recommend / monitor / evolve / ga / quality / valuation
│   ├── llm/                    # LLM 交互：client / prompts / macro_agent / context
│   ├── web/                    # Web 服务：app / dashboard / charts / quotes / runner + static + templates（新/旧两版）
│   └── utils/                  # log / trading_calendar / sina_calendar_decode
├── backtest/                   # 回测研究脚本（python -m backtest.xxx）
├── scripts/                    # init_db.py 建库脚本
├── config/                     # settings.toml.example 配置模板
├── data/                       # SQLite 数据库（运行时生成，不入库）
├── models/                     # LightGBM 模型（运行时生成，不入库）
├── tests/                      # 测试
└── web/                        # 前端构建子项目（tailwindcss）
```

## 📡 数据来源

- **天天基金**（fund.eastmoney.com）：基金列表、历史净值、季报重仓股
- **东方财富**（push2 系列）：板块行情、行业映射、ETF 资金流
- **新浪财经**：沪深300 / 上证指数日线
- **财联社**：财经快讯（宏观新闻）

所有数据仅用于学习研究，版权归原始数据源所有。

## 📄 License

本项目仅供个人学习研究使用。
