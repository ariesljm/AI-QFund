# AI-QFund

AI 驱动的基金量化投研系统。数据基础 → 推荐 → 监控 → 进化，全管线自动化运行，通过 Web UI 查看推荐结果与系统状态。

## 功能

- **数据基础**：自动抓取基金净值、行业映射、宏观新闻
- **推荐**：基于 LightGBM 模型对基金排序打分，生成每日推荐
- **监控**：跟踪持仓表现，多防线风险检测
- **进化**：基于交易结果自动更新推荐规则
- **Web UI**：实时推荐面板、净值走势、行业热力图、资金流向、宏观快讯
- **定时管线**：每日定时运行推荐流程，可配置
- **LLM 集成**：支持任意 OpenAI 兼容接口，用于宏观分析与赛道判断

## 快速开始

### 前置条件

- Docker & Docker Compose
- Python ≥ 3.11（本地开发）

### 使用 uv 安装依赖

```bash
uv sync
```

### 本地运行

```bash
# 初始化数据库
python init_db.py

# 启动 Web 服务（端口 9123）
uv run python web/app.py
```

### Docker 部署

```bash
# 1. 准备配置文件
mkdir -p config data
cp config/settings.toml config/settings.toml  # 编辑 LLM API 密钥等

# 2. 启动
docker compose up -d
```

Web 服务地址：http://localhost:9123

## 配置

编辑 `config/settings.toml`：

| 配置项 | 说明 |
|--------|------|
| `[llm].api_key` | LLM API 密钥（必填） |
| `[llm].base_url` | OpenAI 兼容接口地址 |
| `[llm].model` | 模型名称 |
| `[scheduler].hour` | 定时运行小时（留空禁用） |
| `[scheduler].minute` | 定时运行分钟 |
| `[web].port` | Web 服务端口（默认 9123） |
| `[web].settings_password` | 设置页面访问密码（留空不设） |
| `[logging]` | 文件与控制台日志级别 |

## 项目结构

```
├── Dockerfile              # 容器构建
├── docker-compose.yml      # 一键部署
├── config/                 # 配置文件
├── data/                   # 数据持久化
│   └── schema.sql          # 数据库 schema
├── web/                    # FastAPI Web 服务
│   ├── app.py
│   └── templates/
├── pipeline.py             # 管线编排（数据基础→推荐→监控→进化）
├── data_foundation.py      # 数据抓取与处理
├── recommend.py            # 推荐引擎
├── monitor.py              # 监控与风险检测
├── evolve.py               # 规则进化
├── macro_agent.py          # 宏观分析（LLM 驱动）
├── log_utils.py            # 日志工具
└── pyproject.toml          # Python 依赖
```

## Docker 镜像

```
ghcr.io/ariesljm/ai-qfund:latest
```

构建与推送：

```bash
echo $CR_PAT | docker login ghcr.io -u <用户名> --password-stdin
docker build -t ghcr.io/ariesljm/ai-qfund:latest .
docker push ghcr.io/ariesljm/ai-qfund:latest
```

## 许可证

MIT
