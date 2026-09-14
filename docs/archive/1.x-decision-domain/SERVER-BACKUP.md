# 服务器全库备份与回滚（1.x → 2.0 合并前必做）

**为什么服务器单独一份**：`docker-compose.yml` 把宿主机 `/root/workspace/ai-qfund/data` 挂到容器 `/app/data`，与开发机的 `E:/code/AI-QFund/data` 是**两份互不相干的数据**。本地备份不覆盖服务器。

**为什么不能靠 git**：`qfund.db` 是 871 MB 二进制文件，从未进版本控制。代码能 `git checkout`，数据只能靠备份——`DROP TABLE` / `DELETE` 不可逆。

---

## 0. 前置：确认对象

```bash
# 当前容器运行的镜像（记录下来，回滚时要回到它）
docker inspect ai-qfund --format '{{.Config.Image}}  {{.Image}}'

# 数据卷真实路径与大小
ls -la /root/workspace/ai-qfund/data/qfund.db*
du -sh /root/workspace/ai-qfund/data

# 剩余空间（备份 + 未来新库，建议留 ≥ 5 GB）
df -h /root/workspace
```

同时记录当时的库规模，供校验对比：

```bash
docker exec ai-qfund python -c "
import sqlite3
c = sqlite3.connect('file:/app/data/qfund.db?mode=ro', uri=True)
for t in ('fund_nav','fund_holdings','fund_features','index_daily','fund_basic'):
    print(t, c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
"
```

> 如果容器已不在或命令失败，直接跳到 §1（`docker stop` 后仍可从宿主机文件系统读）。

---

## 1. 停容器（保证快照一致性）

```bash
docker stop ai-qfund

# 确认 WAL 已归零（SQLite 正常关闭会 checkpoint）
ls -la /root/workspace/ai-qfund/data/qfund.db*
# qfund.db-wal 应为 0 字节或不存在；若仍是几百 MB，说明上次是非正常退出，
# 此时**不要**直接 cp，改用 §1b 的 sqlite 方式。
```

### 1b. 备选：不停容器的在线一致性快照

若不能停机，用 `VACUUM INTO` 在源库一致性读的基础上生成单文件快照（可与其他写入并发，必要时重试）：

```bash
docker exec ai-qfund python -c "
import sqlite3
c = sqlite3.connect('file:/app/data/qfund.db?mode=ro', uri=True)
c.execute('VACUUM INTO ?', ('/tmp/qfund-1.x-final.db',))
print('done')
"
docker cp ai-qfund:/tmp/qfund-1.x-final.db /root/backups/qfund-1.x-final.db
docker exec ai-qfund rm -f /tmp/qfund-1.x-final.db
```

---

## 2. 备份

```bash
mkdir -p /root/backups/1.x-final
STAMP=$(date +%Y%m%d)

# 2.1 数据库
cp -v /root/workspace/ai-qfund/data/qfund.db \
      /root/backups/1.x-final/qfund-$STAMP.db

# 2.2 配置（含 LLM key 与调度参数，缺失会导致回滚后行为不一致）
cp -v /root/workspace/ai-qfund/config/settings.toml \
      /root/backups/1.x-final/settings.toml

# 2.3 已训练的模型产物（决策直接依赖，不能只备数据）
tar -czf /root/backups/1.x-final/models-$STAMP.tar.gz \
        -C /root/workspace/ai-qfund models

# 2.4 结构快照与运行日志（体积小，便于事后复盘）
cp -v /root/workspace/ai-qfund/data/schema.sql \
      /root/backups/1.x-final/schema.sql
tar -czf /root/backups/1.x-final/logs-$STAMP.tar.gz \
        -C /root/workspace/ai-qfund/data logs

chmod 600 /root/backups/1.x-final/settings.toml   # 含密钥
ls -la /root/backups/1.x-final/
```

---

## 3. 校验（**必须做，否则等于没备份**）

```bash
docker run --rm -v /root/backups/1.x-final:/b \
  ghcr.io/ariesljm/ai-qfund:latest python -c "
import sqlite3, sys
c = sqlite3.connect('file:/b/qfund-$STAMP.db?mode=ro', uri=True)
print('integrity_check :', c.execute('PRAGMA integrity_check').fetchone()[0])
print('表数            :', c.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0])
for t in ('fund_nav','fund_holdings','fund_features','index_daily','fund_basic'):
    print(f'{t:<16}:', c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0])
"
```

判据：

- `integrity_check` 必须为 `ok`
- 表数必须为 **21**
- 逐表行数必须**与 §0 记录的一致**（或更多——若备份期间有调度写入）

行数对不上就重做，不要带着可疑的备份继续。

---

## 4. 起容器并确认健康

```bash
docker start ai-qfund
sleep 15
docker ps --filter name=ai-qfund --format '{{.Status}}'
curl -fsS http://localhost:9123/ >/dev/null && echo "web OK"
docker logs --tail 30 ai-qfund
```

---

## 5. 异地一份

服务器备份与服务器同盘同机，磁盘故障会一起丢。拉到开发机或对象存储：

```bash
# 在开发机执行
scp -r root@<服务器>:/root/backups/1.x-final ./qfund-server-backup/
python scripts/db_backup.py --src ./qfund-server-backup/qfund-$STAMP.db \
       --dst ./qfund-server-backup/qfund-verified.db --verify-only
```

---

## 6. 可选加固：把生产镜像钉住

**风险**：`docker-compose.yml` 是 `pull_policy: always`，而 CI 在 **push main 时**重建 `:latest`。2.0 重构期间只要有人推 main（或误推），下次容器重建就会把生产拉到半成品。

2.0 的纪律是"不推 main"，但加一道物理保险更稳妥——把镜像钉到当前 digest：

```bash
DIGEST=$(docker inspect ai-qfund --format '{{.Image}}')   # 形如 ghcr.io/...@sha256:...
echo "$DIGEST"
# 在 /root/workspace/ai-qfund/docker-compose.yml 中把
#   image: ghcr.io/ariesljm/ai-qfund:latest
#   pull_policy: always
# 改为
#   image: $DIGEST
#   pull_policy: missing
docker compose -f /root/workspace/ai-qfund/docker-compose.yml up -d
docker inspect ai-qfund --format '{{.Config.Image}}'      # 确认已钉住
```

2.0 正式上线时再改回 `:latest`（那时 CI 会从 2.0 合并后的 main 构建）。

---

## 7. 合并 2.0 前的检查清单

- [ ] §3 校验通过（integrity ok、21 表、行数一致）
- [ ] §5 异地副本已落地并在开发机二次校验通过
- [ ] 本地备份同样完成（`python scripts/db_backup.py`）
- [ ] 本次运行的镜像 digest 已记录（§0 输出）
- [ ] `1.x-final` tag 存在且未 push 到 main
- [ ] 决策域归档已提交（`docs/archive/1.x-decision-domain/`）

---

## 8. 回滚

### 只回滚代码（数据不动）

```bash
git checkout 1.x-final
# 服务器：把镜像改回 §6 记录的 digest，然后
docker compose -f /root/workspace/ai-qfund/docker-compose.yml up -d
```

### 回滚数据（**会覆盖数据库，确认后再执行**）

```bash
docker stop ai-qfund
cp /root/backups/1.x-final/qfund-$STAMP.db /root/workspace/ai-qfund/data/qfund.db
# 清掉可能的 WAL 残留，避免旧日志覆盖新文件
rm -f /root/workspace/ai-qfund/data/qfund.db-wal \
      /root/workspace/ai-qfund/data/qfund.db-shm
docker start ai-qfund
```

模型产物回滚：

```bash
docker stop ai-qfund
rm -rf /root/workspace/ai-qfund/models
tar -xzf /root/backups/1.x-final/models-$STAMP.tar.gz -C /root/workspace/ai-qfund
docker start ai-qfund
```
