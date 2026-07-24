---
name: translate-descriptions
description: 使用当用户要求把 opencode 所有 skill 的非中文 description 翻译成中文
---

# Translate Skill Descriptions to Chinese

扫描项目中所有 SKILL.md 的 frontmatter `description` 字段，将非中文的翻译成中文，不超过 22 个中文字符。

## 扫描范围

1. `E:\code\AI-QFund\.opencode\skills\` — 项目本地技能
2. `C:\Users\aries\.cache\opencode\packages\superpowers*` — superpowers 插件缓存

递归查找所有 `SKILL.md` 文件。

## 规则

1. 读取每个 SKILL.md 的 frontmatter（`---` 之间的 YAML），提取 `description` 字段。
2. 如果 description 为空或不存在，跳过。
3. 判断是否包含中文字符（Unicode \u4e00-\u9fff）。如果不包含，需要翻译。
4. 将英文 description 翻译成中文，限制在 22 个中文字符以内。
5. 用 `description_en` 字段保留原文，覆盖 `description` 为中文翻译。

## 输出格式

对每个翻译过的文件，输出：
```
[translated] skill_name | 原文: "...", 译文: "..."
```

最后汇总：翻译了多少个，跳过了多少个。
