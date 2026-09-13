# 服务器部署升级指南（AI-QFund）

服务器：`192.168.1.222`（root 密码登录，HP EliteDesk 800 G1 USDT，i5-4570S / 15GB）
挂载目录：`/root/workspace/ai-qfund/`（data / models / config，docker compose 挂载）

本文档记录 2026-09-13 执行过的 Style Tracking 升级（schema v3 / λ=1.0 / 19 维模型）的
完整清单与踩坑排查，供下次大版本升级（schema 变更 / 数据迁移）照做。

---

## 1. 一次性升级清单（已执行，2026-09-13）

```bash
cd /root/workspace/ai-qfund

# ① 备份（数据不可重来：净值/持仓/特征历史）
cp data/qfund.db data/qfund.db.bak-YYYYMMDD

# ② 升级镜像并重启（pull_policy: always 自动拉最新）
docker compose up -d

# ③ 删除板块残留（<61 条、不足一个反推窗口的零散/概念板块）
docker exec ai-qfund python -c "
import app
from app.database import get_db
c = get_db()
c.execute('DELETE FROM sector_daily_snapshot')
c.commit()
print('板块残留已清，行数:', c.execute('SELECT COUNT(*) FROM sector_daily_snapshot').fetchone()[0])
"

# ④ 板块历史回填（60 日反推/热度/style_r2 依赖；服务器 push2 不可达时改用本地迁移，见 §2）
docker exec ai-qfund python -m app.data.sector_history --force
# 或本地迁移：本地导出 sector_daily_snapshot → SQL → 上传 → 容器内 executescript 导入

# ⑤ 一次性 bootstrap（特征全量重算 v3 → 模型重训 19 维 → 持仓反推）
docker exec -d ai-qfund python /app/data/_bootstrap.py > /app/data/bootstrap.log 2>&1
# 弱 CPU 服务器（i5-4570S）串行约 50 分钟；并行版（panel_samples workers=4）约 15 分钟
```

## 2. 踩坑排查记录

### 2.1 服务器 push2 域名不可达（IPv6 + 东财临时故障）
- **症状**：板块回填/每日板块快照全失败；curl_cffi `(56) Connection closed`、tls-client 失败、返回 502 页面
- **排查**：`getent hosts push2.eastmoney.com` → 解析到 **IPv6**（`push2ipv6.trafficmanager.cn`）而
  服务器无 IPv6 路由 → 库级请求（curl_cffi/tls-client）走 IPv6 全失败；`curl -4`（强制 IPv4）可通
- **处置**：① 容器内 `/etc/gai.conf` 加 `precedence ::ffff:0:0/96 100`（IPv4 优先，镜像层不持久，构建时加）；② 2026-09-13 凌晨 push2 连 IPv4 也 56/502（东财侧临时故障，本地同不可达，push2ex 正常 200）→ 等待东财恢复，恢复后调度器自动补每日增量
- **日常影响**：每日板块**当天快照**（推荐流程抓取）依赖 push2；板块**历史**已迁移就位，反推/特征/监控不受影响

### 2.2 curl.exe 跨平台 bug（已修复 `e79a075`）
- **症状**：Linux 容器降级链第三级 `[Errno 2] No such file or directory: 'curl.exe...'`
- **根因**：`_fetch_push2_curl_exe` 硬编码 Windows 命令名 `curl.exe`；且 `subprocess.run(cmd 字符串)` 无
  `shell=True` 时 Linux 把整串当可执行文件名
- **修复**：参数列表形式 `["curl", "-4", "-s", "--noproxy", "*", ...]`（跨平台，Windows/Linux 均正确解析）

### 2.3 sshpass-win32 密码传输缺陷（本地开发工具）
- 本机 sshpass-win32 传密码认证必失败（paramiko 同密码成功）→ SSH 到服务器请用 paramiko 或
  其他客户端；勿据此误判密码错误

### 2.4 样本构建并行化（`8cd9c5c`）
- 背景：重训 60% 时间花在**样本构建**（2000 基金 × 每 20 步 × style_r2 反推 60 日回归，
  纯串行单线程）；弱 CPU 上串行 50 分钟
- 实现：`panel_samples(workers=4)` multiprocessing 分基金池（基金间零耦合 = 天然可并行）；
  已**逐位验证**并行/串行样本集完全一致（同数据同模型，效果零差异）；`workers<=1` 或
  基金池 <64 自动退化为串行
- 收益：i5-4570S 上重训 50 分钟 → ~15 分钟（LightGBM 训练本身受益于多线程）

### 2.5 硬件升级参考
- 主板 HP EliteDesk 800 G1 USDT（LGA1150 / 超薄机箱 / 原装散热仅支持 65W TDP）
- E3-1230V3（80W）插得上但散热压不住、USDT BIOS 支持不确定 → 不推荐
- **E3-1270V5（LGA1151）物理不兼容，装不上**
- 推荐：**i7-4790S**（65W / 4C8T / 睿频 4.0GHz，官方支持列表内，单核比 i5-4570S 快 ~35%）

## 3. 日常运维提示

- **每日 14:30 调度器**自动跑全流程（数据基座→推荐→监控→进化）；日常 CPU 低（增量特征 + 推理 + LLM API）
- **每 7 天一次模型重训**（`_RETRAIN_INTERVAL_DAYS=7`）：并行版 ~15 分钟高 CPU，属正常；
  重训由 `label_version`/`feature_dim` mismatch 或到期自动触发，无需手动
- **特征全量重算**只在 schema 升级时发生（一次性）；日常只增量（`skip_codes` 跳过已最新）
- 反推仅对持仓基金（`update_all_fund_styles`，~20 只）→ 秒级
- 服务器**切勿删除 data/ 目录**（净值 1158 万行 / 持仓 / 特征历史不可快速重来）；升级一律走
  `_migrate` + schema 版本机制自动迁移
