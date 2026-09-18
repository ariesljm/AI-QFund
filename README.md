# 📊 AI-QFund · 智能公募基金量化投研终端（2.0）

[![CI](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml/badge.svg)](https://github.com/ariesljm/AI-QFund/actions/workflows/ci.yml)

AI 驱动的中国公募基金量化投研系统。核心范式为 **「全市场量化初筛 → LLM 排雷审计 → Top5 定论」**，辅以三级状态机盯盘与知识/校准驱动的自我进化。极简架构、本地 SQLite、无高资源消耗框架。

> 2.0 重构（`docs/2.0-consensus.md`）把 1.x 的「LLM 选赛道 → 赛道内排序」重写为全市场直选 + LLM 定性排雷；监控从「六道防线」收敛为「三级状态机 + 校准层」；进化从「GA 参数寻优」改为「知识积累 + 校准记账」。

> ⚠️ **免责声明**：本项目仅用于个人研究与学习，输出结果不构成任何投资建议。基金有风险，投资需谨慎。

---

## 🔄 核心流程

```
数据基座 → 全市场初筛(Top30) → LLM 排雷审计 → Top5 定论 → 三级状态机盯盘 → 校准/知识回流
```

**推荐漏斗**：`全市场初筛（Top30 候选池）→ LLM 排雷审计（VETO/风险分剪枝）→ 复合分定 Top5`

## ✨ 核心能力

### 1️⃣ 数据基座
全市场 12K+ 基金列表（周重建）、历史净值（真·增量更新 + 停更/短历史打标）、沪深300 / 上证指数、季报重仓股（多期 + `disclosure_date` PIT 公告日口径）、申万行业映射、个股日线（前复权）与估值（PE/PB）、基金 AUM/份额/申购状态。

### 2️⃣ 全市场初筛（模块一）
- 主动权益池：混合型 + 股票型（**剔指数型**）
- 硬过滤链：合并规模 <5000 万 或 >100 亿 / 暂停申购 / 单日限额 <1000 元 / 净值历史 <62 条
- 简单多因子打分（`sharpe_60d` + `mom_250d` + `ttr_60d` 反向，等权百分位）→ **Top30 候选池**落库并保留特征快照

### 3️⃣ LLM 排雷审计（模块二）
- 四组切片：**持仓异动 / 重仓股风险雷达 / 管理团队变动 / 舆情争议**（缺失项明确标注，不脑补）
- 审计 JSON：`audit_verdict`（PASS / CONDITIONAL_PASS / VETO）、`risk_score` 0–100、`veto_reasons`、`audit_details`、`recommendation_summary`；schema **严格校验，非法即失败不静默降级**
- 剪枝：`VETO` 或 `risk_score > 60` 剔除；复合分 `量化分 × (1 − Risk/100)` 定 **Top5**
- **事件驱动审计缓存**：失效条件三元组（持仓期次 / 重仓股指纹 / 风险事件版本）任一变化才重跑 LLM
- 无机会时优雅区分 `no_opportunity`（市场判断）与 `data_failure`（数据故障）

### 4️⃣ 跟踪与校准（模块三）
- **三级状态机** HOLD / WATCH / EXIT（**EXIT 不可逆**，无跨级直通，先观察后退出）
- 信号：估值分位 ≥85% / Alpha 连续 5 日负 → WATCH；跌破 EMA20 / 极端估值+动量转负 / 致命公告 → EXIT
- **净值陈旧不计入升级序列**（数据问题 ≠ 信号）
- **校准层**：每路信号如实记账历史命中率与样本量，连续失灵 3 次降权 / 5 次停用，样本不足不降权

### 5️⃣ 进化引擎（模块四）
- **知识库**：异常回撤 >8% 或触发 EXIT 时归因分叉（特征滞后 vs 隐患漏判）→ 结构化 Bad/Good-Case → Few-Shot 回流排雷 prompt（只增不改）
- **影子闸门**（Champion-Challenger）：≥3 个不重叠子区间（牛/熊/震荡）同向不劣 + 非重叠窗口显著性检验，通过才切换模型
- **规则退役**：否决类单向增严不可逆；信号/特征类命中率长期低于阈值可提案退役（留痕可恢复）
- **参数层冻结**：λ 等阈值人工改，不做连续寻优（1.x 实证：噪声 ±8pp > 真实信号 +6pp）

### 6️⃣ Web 面板
FastAPI 响应式单页仪表板（桌面 / 手机自适应）：**最近 LLM 审计**（排雷过程可追溯）、**状态机视图**（HOLD/WATCH/EXIT）、**校准曲线**（信号命中率）、质量度量、赛道热力图、基金卡片、追踪列表、基金详情滑出面板（推荐理由、多周期涨跌幅、前十大重仓、净值走势、监控信号）、快讯轮播、实时指数、管线状态卡（含系统运行累计时间）、结构化系统日志；支持手动触发管线、数据管理与设置页密码保护。

---

## 🏗️ 系统架构

```
    数据基座                  模块一 初筛                 模块二 排雷审计
┌────────────────┐   ┌───────────────────┐   ┌─────────────────────┐
│ 基金/净值/指数   │   │ 主动权益池          │   │ 四组切片装配          │
│ 重仓股/行业映射  │→│ 硬过滤链(4条)        │→│ LLM 审计(PASS/VETO)  │
│ 估值/日线/AUM   │   │ 多因子打分 → Top30  │   │ 剪枝/复合分 → Top5   │
└────────────────┘   └───────────────────┘   └──────────┬──────────┘
       ↑                                                 ↓
  ┌────────┐    ┌──────────────────────────────────────────────┐
  │ SQLite │◄───│  模块三 三级状态机(HOLD/WATCH/EXIT) + 校准层      │
  └────────┘    │  模块四 知识库 / 影子闸门 / 规则退役 / 结算账本      │
                └──────────────────────────────────────────────┘
```

**主标尺**（ADR-0008 分职）：120 日超额收益（推荐后收益 − 同类 RBSA 第一行业均值）作守门/验收；校准命中率作过程诊断；绝对收益 >1% 胜率仅 Web 展示。

## 🛠️ 技术栈

| 组件 | 选型 |
|------|------|
| 语言 / 包管理 | Python ≥ 3.11 / [uv](https://docs.astral.sh/uv/) |
| 数据库 | SQLite（WAL 模式，27 张表） |
| HTTP 客户端 | httpx（push2 域名 TLS 指纹伪装，curl_cffi → tls_client → curl 三级降级） |
| 量化计算 | NumPy / LightGBM / scikit-learn / scipy |
| LLM 接口 | OpenAI 兼容 API（base_url / api_key / model 可配置，单接口 + 重试 + 决策审计） |
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
uv run python -m app.data.foundation              # 数据基座（基金/净值/指数/持仓/特征）
uv run python -m app.data.foundation --step 2     # 仅更新净值
uv run python -m app.data.foundation --index-backfill   # 指数历史断档补拉
uv run python scripts/backfill_2_0_data.py --val      # 回填个股估值
uv run python scripts/backfill_2_0_data.py --daily    # 回填个股日线
uv run python scripts/backfill_2_0_data.py --fund     # 回填基金 AUM/申赎状态
uv run python scripts/backfill_2_0_data.py --holdings # 回填多期持仓历史（慢，按年）
uv run python -c "from app.pipeline import run; run()"   # 全流程管线
uv run pytest tests/                            # 运行测试（76 个测试文件）
```

## 🐳 Docker 部署

```bash
docker compose pull && docker compose up -d
```

- 端口 `9123`；挂载 `data/`、`models/` 与 `config/settings.toml`（**宿主机须先创建该文件**，否则 Docker 会建为目录）
- 环境变量：`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` / `SCHEDULER_HOUR` / `SCHEDULER_MINUTE` / `ENABLE_SCHEDULER` / `WEB_PORT`
- 每日定时全流程：compose 默认 **14:30**（T-1 数据完整后跑），仅交易日触发；本地 `settings.toml` 默认 08:00

> **首次部署自举**：空数据卷启动后，全流程需依次完成 基金列表 → 净值全量（约 1150 万条，数小时）→ 持仓（多期回填）→ 行业映射 → 估值/日线/AUM 回填 → 特征，可跨多次调度续跑（增量幂等，`backfill_2_0_data.py` 断点续传）。期间可查 `data_fetch_failures` 表定位被限流接口。

## ⚙️ 配置说明（`config/settings.toml`）

```toml
[llm]          # OpenAI 兼容接口
base_url = "http://localhost:11434/v1"
api_key = "your-api-key-here"
model = "gpt-4o-mini"

[scheduler]    # 每日定时全流程（hour 为空表示关闭）
hour = 8
minute = 0

[label]        # 超额标签的回撤惩罚系数（初值 1.0 = 生产标定值）
lambda = 1.0

[recommend_v2] # 2.0 推荐主流程开关（enabled=true 为唯一合法值）
enabled = true

[logging]      # 文件/控制台日志级别 + system_logs 滚动保留（双条件兜底）
file_level = "INFO"
console_level = "INFO"
db_retention_days = 30
db_max_rows = 20000

[web]          # Web 服务
port = 9123
settings_password = ""   # 设置页访问密码（留空不设）
```

> **LLM 代理排障提示**：聚合代理可能间歇性返回 HTTP 500 或「200 但 `content` 为空」（纯思考模型把输出预算耗在 `reasoning_content` 上）。`call_llm` 已内置 6 次重试 + 空内容重试；审计/监控逻辑失败保守降级。反复失败时优先检查代理/网关健康，而非视为代码 bug。

## 📁 项目结构

```
AI-QFund/
├── app/                        # 主应用包
│   ├── config.py / database.py / domain.py / pipeline.py / benchmark.py / settlement.py
│   ├── repo/                   # 数据仓库层：base（底层读写）/ decision（决策域）/ nav（净值序列）/ ledger（结算账本）/ meta_keys
│   ├── data/                   # 数据基座：foundation / fetchers / nav / holdings / industry_map / valuation / stock_daily / announcements / manager / sentiment / store
│   ├── features/               # 特征计算：calculator / v2（2.0 三维度）/ valuation / stats / fees
│   ├── engine/                 # 引擎：screen（初筛）/ recommend_v2（审计→Top5）/ state_machine / supervise（监控接线）/ calibration / knowledge / gate / quality / drift
│   ├── llm/                    # LLM 交互：client（单接口）/ prompts / audit（校验剪枝）/ context（素材装配）
│   ├── web/                    # Web 服务：app / dashboard / charts / quotes / runner + static + templates
│   └── utils/                  # log / trading_calendar / sina_calendar_decode
├── backtest/                   # 回测研究脚本（python -m backtest.xxx）
├── scripts/                    # init_db.py 建库 / backfill_2_0_data.py 回填 / db_backup.py 备份
├── config/                     # settings.toml.example 配置模板
├── data/                       # SQLite 数据库与 schema.sql（运行时生成，不入库）
├── models/                     # 模型产物（运行时生成，不入库）
├── docs/                       # 共识基线 / ADR（0001–0011）/ 回测报告 / 代理文档
├── tests/                      # 测试（76 个测试文件）
└── web/                        # 前端构建子项目（tailwindcss）
```

## 📡 数据来源

- **天天基金**（fund.eastmoney.com）：基金列表、历史净值、季报重仓股、经理档案（F10）
- **东方财富**（push2 / datacenter / emweb 系列）：板块行情、行业映射、ETF 资金流、个股估值（PE/PB）、个股公告（风险雷达）、舆情搜索
- **新浪财经**：沪深300 / 上证指数日线
- **搜狐**（hisHq）：个股前复权日线
- **财联社**：财经快讯（宏观新闻）

所有数据仅用于学习研究，版权归原始数据源所有。

## 📚 进一步阅读

- `docs/2.0-consensus.md` — 2.0 重构共识基线（Q1–Q21 决策清单与证据）
- `docs/README.md` — 2.0 架构设计文档
- `docs/adr/` — 架构决策记录 0001–0011（数据写不变式 / LLM 单接口 / PIT 公告日 / 标尺分职 / 进化分层等）
- `docs/backtest/120d-multifactor-rebuild.md` — 120 日主标尺 + 简单多因子排序的关键回测依据
- `CONTEXT.md` — 领域词汇表（2.0 统一术语）

## 📄 License

本项目仅供个人学习研究使用。
