/** @type {import('tailwindcss').Config} */
// 依据 docs/stitch_smart_fund_recommender 设计稿（Institutional Intelligence / code.html 同源 token）迁移：
// Corporate Modern · Terminal Blue（primary #001e40 藏青 / primary-container #003366 海军蓝 / secondary-container #0070eb 活力蓝）
// · 标题 Hanken Grotesk + 正文 Inter + 数据 JetBrains Mono · 红涨绿跌（中国市场惯例，up=#E74C3C / down=#2ECC71）
module.exports = {
  darkMode: "class",
  content: [
    "../app/web/templates/index.html",
    "../app/web/static/app.js", // 拆文件后的前端脚本（内容扫描类名）
  ],
  theme: {
    extend: {
      colors: {
        // —— 设计稿 MD3 色板（stitch DESIGN.md / code.html tailwind.config 同源）——
        "primary": "#001e40",                       // 深藏青（标题/品牌/主按钮）
        "on-primary": "#ffffff",
        "primary-container": "#003366",             // 海军蓝（结构导航/头部）
        "on-primary-container": "#799dd6",
        "accent": "#0070eb",                        // 活力蓝（交互强调/数据高亮）
        "accent-2": "#0058bc",                      // hover 蓝（secondary）
        "accent-soft": "rgba(0, 112, 235, 0.10)",
        "secondary": "#0058bc",
        "on-secondary": "#ffffff",
        "secondary-container": "#0070eb",
        // —— 表面层级（surface #f7f9fb 页面底 / 卡片白 / 容器浅灰）——
        "paper": "#f7f9fb",                         // surface 页面底
        "paper-raised": "#ffffff",                  // surface-container-lowest 卡片
        "paper-2": "#f2f4f6",                       // surface-container-low
        "background": "#f7f9fb",
        "surface": "#eceef0",                       // surface-container（内嵌块/表格底）
        "surface-container": "#eceef0",
        "surface-container-low": "#f2f4f6",
        "surface-container-lowest": "#ffffff",
        "surface-container-high": "#e6e8ea",        // 持仓 chip 底
        "surface-container-highest": "#e0e3e5",
        // —— 文字与边框 ——
        "on-surface": "#191c1e",
        "on-surface-variant": "#43474f",
        "text-muted": "#64748B",
        // 注意：outline 取 surface-border 值（#E2E8F0）是适配既有 border-outline 类名（卡边框/分隔线）的刻意选择；
        // 设计稿 outline(#737780) 深色由 outline-strong 承担；CSS 变量侧对应 --color-rule / --color-rule-2
        "outline": "#E2E8F0",                       // surface-border 卡边框
        "outline-strong": "#737780",                // outline
        "outline-variant": "#c3c6d1",
        "surface-border": "#E2E8F0",
        "ink": "#e0e3e5",
        "graphite": "#2d3133",                      // inverse-surface 日志终端深色带
        // —— 金融语义（红涨绿跌：up=danger-losses 红 / down=success-gains 绿）——
        "up": "#E74C3C",
        "down": "#2ECC71",
        "up-soft": "rgba(231, 76, 60, 0.08)",
        "down-soft": "rgba(46, 204, 113, 0.08)",
        "warn-soft": "rgba(243, 156, 18, 0.10)",
        "error": "#ba1a1a",
        "error-container": "#ffdad6",
        "success": "#2ECC71",
        "warning-amber": "#F39C12",
        "violet": "oklch(0.55 0.14 290)",
        "rose": "oklch(0.60 0.17 12)",
        "cyan": "oklch(0.55 0.11 215)",
        "warn": "#F39C12"
      },
      borderRadius: { DEFAULT: "0.25rem", sm: "0.125rem", md: "0.375rem", lg: "0.5rem", xl: "0.75rem", "2xl": "1rem", full: "9999px" },
      spacing: { "container-padding": "20px", unit: "4px", gutter: "16px", "gutter-lg": "24px", "margin-page": "24px", "stack-sm": "8px", "stack-md": "16px", "stack-lg": "24px" },
      fontFamily: {
        sans: ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        // 设计稿：Hanken Grotesk 标题 / Inter 正文 / JetBrains Mono 数据（latin 本地 woff2，中文回退 Noto Sans SC）
        "label-caps": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "display-lg": ['Hanken Grotesk', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "display-md": ['Hanken Grotesk', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "headline-md": ['Hanken Grotesk', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "headline-sm": ['Hanken Grotesk', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "title-sm": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "body-md": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "body-sm": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "data-md": ['JetBrains Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "data-lg": ['JetBrains Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "data-sm": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "data-tabular": ['JetBrains Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "label-xs": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "display-data": ['Hanken Grotesk', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif']
      },
      fontSize: {
        "label-caps": ["11px", { "lineHeight": "16px", "letterSpacing": "0.05em", "fontWeight": "700" }],
        "label-xs": ["11px", { "lineHeight": "16px", "letterSpacing": "0.05em", "fontWeight": "700" }],
        "display-lg": ["32px", { "lineHeight": "40px", "letterSpacing": "-0.02em", "fontWeight": "700" }],
        "display-md": ["24px", { "lineHeight": "32px", "letterSpacing": "-0.015em", "fontWeight": "700" }],
        "headline-md": ["24px", { "lineHeight": "32px", "letterSpacing": "-0.01em", "fontWeight": "600" }],
        "headline-sm": ["18px", { "lineHeight": "24px", "fontWeight": "600" }],
        "title-sm": ["16px", { "lineHeight": "24px", "fontWeight": "600" }],
        "data-md": ["14px", { "lineHeight": "20px", "fontWeight": "500", "letterSpacing": "-0.01em" }],
        "data-lg": ["19px", { "lineHeight": "24px", "fontWeight": "600" }],
        "body-md": ["14px", { "lineHeight": "20px", "fontWeight": "400" }],
        "body-sm": ["12px", { "lineHeight": "16px", "fontWeight": "400" }],
        "data-sm": ["12px", { "lineHeight": "16px", "fontWeight": "400" }],
        "data-tabular": ["14px", { "lineHeight": "20px", "fontWeight": "500", "letterSpacing": "-0.01em" }],
        "display-data": ["32px", { "lineHeight": "40px", "fontWeight": "700" }]
      }
    }
  }
}
