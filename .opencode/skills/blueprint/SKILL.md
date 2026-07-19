---
name: blueprint
description: 生成可机械执行的代码改造计划；用户说"生成开发计划"、"出改造计划"、"blueprint"时触发。
---

## Step 0: Absorb

扫描这个请求中提到的所有文档和当前项目结构。完成前不要提问，完成后说"我已读取所有上下文，开始访谈。"

## Step 1: Grill

围绕这个计划的每个方面持续访谈我，直到我们达成共同理解。沿着 design tree 的每个分支往下走，逐一解决决策之间的依赖。每个问题都要附上你的推荐答案。

一次只问一个问题，并等待我对该问题的反馈后再继续。一次问多个问题会让人失去方向。

如果某个 *fact* 能通过探索 codebase 找到，就直接查找，不要问我。但 *decisions* 属于我；逐个交给我决定，并等待回答。

**最低覆盖维度**：
- Scope：哪些模块要改，哪些绝对不动
- Stack：语言版本、框架版本、包管理器、部署方式
- Contracts：模块间数据格式、API 签名、错误处理约定
- Acceptance：每个阶段的可验证完成标准

在我确认达成共同理解之前——明确说"可以开始生成计划了"——不要进入下一步。

## Step 2: Generate

在我确认后，生成结构化计划。每个 phase 必须：
- **Self-contained**：可独立测试
- **Dependency-ordered**：无循环依赖
- **Architecture-first**：基础层在前，消费层在后

每个 change 必须：引用真实存在的文件路径、通过 [quality-constraints.md](quality-constraints.md) 检查（禁止 hacky 修复、禁止幻觉引用）。

## Step 3: Save

将计划写入 `docs/` 目录下，文件名格式：`blueprint-[日期]-[项目代号].md`。

计划末尾必须包含：
- 代码修改后需要更新的文档清单
- 冲突解决策略（依赖版本、接口变更、breaking changes）

输出格式遵循 [output-format.md](output-format.md)。
