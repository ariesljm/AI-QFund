#!/bin/sh
# 一次性下载远程主机(192.168.1.222) AI-QFund 运行数据到本地（局域网通道）
set -x
cd /e/code/AI-QFund

export SSH_ASKPASS=/tmp/askpass.sh
export SSH_ASKPASS_REQUIRE=force
export DISPLAY=:0
HOST=root@192.168.1.222
OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o PreferredAuthentications=password -o PubkeyAuthentication=no"

echo "=== [1/5] qfund.db (857MB) ==="
scp $OPTS $HOST:/root/workspace/ai-qfund/data/qfund.download.db data/qfund.db || exit 1

echo "=== [2/5] logs/ ==="
scp $OPTS -r $HOST:/root/workspace/ai-qfund/data/logs data/ || exit 1

echo "=== [3/5] models/lgb_model.txt ==="
scp $OPTS $HOST:/root/workspace/ai-qfund/models/lgb_model.txt models/lgb_model.txt || exit 1

echo "=== [4/5] config/settings.toml ==="
scp $OPTS $HOST:/root/workspace/ai-qfund/config/settings.toml config/settings.toml || exit 1

echo "=== [5/5] last_recommendation.txt (已弃用，存在则拉) ==="
scp $OPTS $HOST:/root/workspace/ai-qfund/data/last_recommendation.txt data/last_recommendation.txt 2>/dev/null || echo "跳过：展示文件已从引擎移除（Wave 1 深化），服务器旧文件不拉取"

echo "=== 清理远程临时快照 ==="
ssh $OPTS $HOST "rm -f /root/workspace/ai-qfund/data/qfund.download.db" || true

echo "=== ALL DOWNLOAD DONE ==="
ls -la data/qfund.db models/lgb_model.txt config/settings.toml