# 特征计算域科学计算库豁免（scipy/sklearn）

架构决策：系统自始刻意保持极简（无 scipy/sklearn，量化计算仅 NumPy + LightGBM）。
净值反推实时持仓（Style Tracking）需要带约束岭回归/弹性网求解（非负、和为 1），
以及后续动态 Kalman 滤波的矩阵求逆——这些若用 NumPy 手写坐标下降/投影梯度下降，
极易引入收敛性 Bug 且难维护。决定：在**特征计算域**破例引入 scikit-learn + scipy，
其余域继续维持极简约束。

## 落地（2026-09-10）

- 依赖清单新增 `scikit-learn`（弹性网坐标下降求解，处理行业收益率矩阵的共线性与稀疏）
  与 `scipy`（`linalg`/`optimize`，供后续 Kalman 与带约束优化使用）。
- **豁免边界**：仅 `app/features/`（特征计算域）允许 import sklearn/scipy；
  `app/model.py` 的训练/推理热路径仍只用 lightgbm + numpy（不进本豁免）。
  新增依赖不得扩散到数据基座 / 引擎 / web 层。

## Considered Options

- **方案 A：手写 NumPy 坐标下降 / 投影梯度下降（维持零依赖）** — 被拒：非负 + 和为 1
  的双约束投影需自实现收敛判据与对偶变量，代码量数百行且无现成测试基准，风险大于收益。
- **方案 B：引入 scikit-learn ElasticNet（采纳）** — 成熟实现，`positive=True` +
  归一化权重即可满足非负约束，和为 1 由残差项（现金+选股 alpha）吸收；工程稳定、
  可替换（日后换 Kalman 只动 features 域）。

## Consequences

- 特征计算域可做带约束回归/矩阵运算，Style Tracking（P1）与 Kalman 动态化（P2）得以落地。
- 豁免是**域内例外**而非全局放开：新增依赖的 import 若出现在 features/ 之外，应视为
  违反本 ADR，需另行评审。
- 训练/推理热路径不引入新依赖，LightGBM 训推性能不受影响。

## 边界执行（2026-09-12，架构深化候选 2）

审查发现引擎研究工具（walk_forward/backtest_model）曾直接 import scipy、而 quality.py
为守边界手写 25 行 rankdata——同概念两套实现。已收敛：

- **features/stats.py 深模块**：秩相关/线性相关/显著性检验命名函数（内部 scipy，
  豁免域内），引擎层只消费命名函数。
- **可执行约束**：`tests/test_stats_primitives.py::TestExemptionBoundary` 扫描
  features/ 之外的所有源码，出现真实 `import scipy`/`from scipy` 语句即失败——
  边界从"注释承诺"升级为 CI 可执行断言。
