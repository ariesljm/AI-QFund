# AI-QFund · 智能进化公募基金推荐与监控系统

[![CI](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml/badge.svg)](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml)

AI 驱动的基金量化投研系统：数据基座拉取全市场基金净值 → LightGBM 特征打分 → LLM 宏观选赛道与终审定论 → 虚拟池每日监控 → 月末进化沉淀教训。极简架构，本地 SQLite 存储，无高资源消耗框架。

> ⚠️ **免责声明**：本项目仅用于个人研究与学习，输出结果不构成任何投资建议。基金有风险，投资需谨慎。

---

## 功能特性

- **数据基座**：全市场 12K+ 基金列表、历史净值（真·增量更新）、沪深300 指数、季报重仓股、申万行业映射
- **量化特征**：Hurst 指数、20 日动量、卡玛比率、下行波动率、上涨/下跌捕获率、60 日乖离率、RBSA 行业暴露、大盘状态机（BULL/BEAR/NEUTRAL）
- **推荐引擎**：LightGBM 预测"未来 20 日相对沪深300 的超额收益"（买入信号；产品层持有/监控周期 30 天）→ 赛道内排序 → LLM 综合宏观/持仓/新闻做终选定论与否决；判断当日无机会时优雅输出"今日无推荐"
- **监控引擎**：三道防线（追踪止损 / RBSA 风格漂移 / LLM 逻辑证伪）每日对 HOLD 持仓盯盘，输出持有/加仓/警惕/离场四类信号，触发即标记 EXIT
- **进化引擎**：月末沉淀教训（`evolution_insights`）回流推荐与监控提示词 + 排序权重自纠偏 + 推荐质量度量（IC/超额胜率/累计超额）驱动闭环；模型每周自动重训
- **Web 面板**：FastAPI 单页仪表板，实时展示推荐、持仓、宏观摘要、管线日志，支持手动触发管线与数据管理

## 系统架构

```
        数据基座                 推荐引擎                  监控引擎
┌─────────────────────┐   ┌──────────────────────┐   ┌──────────────┐
│ 基金列表 / 净值 / 指数 │→│ LLM 宏观选赛道         │   │ 追踪止损       │
│ 重仓股 / 行业映射     │   │ LightGBM 赛道内排序    │→ │ 风格漂移       │
│ 特征计算 / RBSA      │   │ LLM 终选定论 / 否决    │   │ LLM 逻辑证伪   │
└─────────────────────┘   └──────────────────────┘   └──────────────┘
        ↑                                                       ↓
   ┌────────┐                                         ┌──────────────────┐
   │ SQLite │◄────────────────────────────────────────│ 进化引擎（月末）   │
   └────────┘                                         └──────────────────┘
```

推荐漏斗：**LLM 选赛道（宏观） → LightGBM 赛道内排序 → LLM 终选定论**

LightGBM 预测目标是 `R_fund(t+20) - R_hs300(t+20)`（相对沪深300 的超额收益），而非绝对收益，防止熊市推荐高波动基金。

## 技术栈

| 组件 | 选型 |
|------|------|
| 语言 | Python ≥ 3.11 |
| 包管理 | [uv](https://docs.astral.sh/uv/) |
| 数据库 | SQLite（WAL 模式，14 张表） |
| HTTP 客户端 | requests / aiohttp（push2 域名 TLS 指纹伪装 + 三级降级） |
| 量化计算 | NumPy / LightGBM |
| LLM 接口 | OpenAI 兼容 API（base_url / api_key / model 可配置） |
| Web | FastAPI + Jinja2（单页仪表板） |
| 部署 | Docker / docker-compose / GitHub Actions |

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
uv run python init_db.py

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
- 数据卷挂载：`./data` → `/app/data`、`config/settings.toml`
- 环境变量：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `SCHEDULER_HOUR` / `SCHEDULER_MINUTE` / `ENABLE_SCHEDULER` / `WEB_PORT`
- 每日定时推荐（默认 08:00）

> 首次部署后，进入 Web 面板点击「立即启动推荐管线」触发全流程；新环境会自动训练 LightGBM 模型（依赖数据量，耗时较长属正常）。

## 配置说明（`config/settings.toml`）

```toml
[llm]          # OpenAI 兼容接口
base_url = "http://localhost:11434/v1"
api_key = "your-api-key-here"
model = "gpt-4o-mini"

[scheduler]    # 每日定时推荐（hour 为空表示关闭）
hour = 8
minute = 0

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
│   ├── database.py             # SQLite 连接管理、schema 初始化、迁移
│   ├── repo.py                 # 数据仓库层（封装所有 SQL 查询）
│   ├── pipeline.py             # 管线编排（数据基座 → 推荐 → 监控 → 进化）
│   ├── data/                   # 数据基座子包
│   │   ├── foundation.py       # 数据基座编排与抓取逻辑
│   │   ├── fetchers.py         # 统一 HTTP 请求层（TLS 伪装 + 三级降级）
│   │   ├── nav.py              # 净值抓取（全量下载 / 真·增量）
│   │   └── store.py            # 数据写入层
│   ├── features/               # 特征计算子包
│   │   ├── calculator.py       # Hurst / 动量 / 卡玛 / RBSA / 大盘状态机
│   │   └── sector.py           # 申万行业板块代码过滤
│   ├── engine/                 # 引擎子包
│   │   ├── recommend.py        # 推荐引擎（LightGBM + LLM 否决）
│   │   ├── monitor.py          # 虚拟池监控引擎（三道防线）
│   │   └── evolve.py           # 进化引擎（错题本规则生成）
│   ├── llm/                    # LLM 交互子包
│   │   ├── client.py           # 统一 LLM 调用接口（含重试）
│   │   ├── prompts.py          # Prompt 模板集中管理
│   │   └── macro_agent.py      # 宏观分析 agent（选赛道）
│   ├── web/                    # Web 服务
│   │   ├── app.py              # FastAPI 应用
│   │   └── templates/          # Jinja2 模板
│   └── utils/
│       └── log.py              # 日志工具（JSON / 结构化）
├── backtest.py                 # 回测框架（独立入口）
├── init_db.py                  # 建库脚本
├── config/                     # 配置模板
├── data/                       # SQLite 数据库 + schema.sql
├── models/                     # LightGBM 模型（运行时生成）
├── tests/                      # 测试
└── docs/                       # 开发文档
```

## 数据来源

- 天天基金（fund.eastmoney.com）：基金列表、历史净值、季报重仓股
- 东方财富（push2 系列接口）：板块行情、行业映射、ETF 资金流
- 新浪财经：沪深300 指数日线
- 财联社：财经快讯（宏观新闻）

所有数据仅用于学习研究，版权归原始数据源所有。

## License

本项目仅供个人学习研究使用。
