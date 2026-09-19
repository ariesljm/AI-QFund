/** @type {import('tailwindcss').Config} */
// 深色 + 琥珀 主题（Hallmark redesign）：
// 深墨暖底（paper #070503）+ 琥珀 accent（#e8a44c）· 红涨绿跌（中国市场惯例，up=#e85a4f / down=#4caf6e）
// · 标题 Space Grotesk + 正文 Inter + 数据 JetBrains Mono · 中文 Noto Sans SC → Microsoft YaHei 兜底（防宋体回退）
module.exports = {
  darkMode: "class",
  content: [
    "../app/web/templates/index.html",
    "../app/web/static/app.js", // 拆文件后的前端脚本（内容扫描类名）
  ],
  theme: {
    extend: {
      colors: {
        // —— 深色琥珀色板 ——
        "primary": "#e8a44c",                       // 琥珀（标题/品牌/主按钮）
        "on-primary": "#1a1714",                    // 主按钮文字深墨
        "primary-container": "#3a3128",
        "on-primary-container": "#e8a44c",
        "accent": "#e8a44c",                        // 活力琥珀（交互/数据高亮）
        "accent-2": "#f0b860",                      // hover 琥珀亮
        "accent-soft": "rgba(232,164,76,0.10)",
        "secondary": "#f0b860",
        "on-secondary": "#1a1714",
        "secondary-container": "#e8a44c",
        // —— 表面层级（深墨暖灰系）——
        "paper": "#070503",                         // surface 页面底
        "paper-raised": "#15110c",                  // surface-container-lowest 卡片
        "paper-2": "#100d09",                       // surface-container-low
        "background": "#070503",
        "surface": "#1c160e",                       // surface-container（内嵌块/表格底）
        "surface-container": "#1c160e",
        "surface-container-low": "#100d09",
        "surface-container-lowest": "#15110c",
        "surface-container-high": "#221c14",        // 持仓 chip 底
        "surface-container-highest": "#28221c",
        // —— 文字与边框 ——
        "on-surface": "#f2eee2",                    // 正文暖白
        "on-surface-variant": "#b0a890",            // 次级文字
        "text-muted": "#78705f",
        "outline": "#2a2418",                       // surface-border hairline
        "outline-strong": "#382f1f",
        "outline-variant": "#3a3128",
        "surface-border": "#2a2418",
        "ink": "#d8d0c0",                           // 日志终端区正文（graphite 深带上）
        "graphite": "#0c0a07",                      // inverse-surface 日志终端深色带
        // —— 金融语义（红涨绿跌：up=danger 红 / down=success 绿）——
        "up": "#e85a4f",
        "down": "#4caf6e",
        "up-soft": "rgba(232,90,79,0.10)",
        "down-soft": "rgba(76,175,110,0.10)",
        "warn-soft": "rgba(232,164,76,0.12)",
        "error": "#e85a4f",
        "error-container": "rgba(232,90,79,0.14)",
        "success": "#4caf6e",
        "warning-amber": "#e8a44c",
        "violet": "oklch(0.55 0.14 290)",
        "rose": "oklch(0.60 0.17 12)",
        "cyan": "oklch(0.55 0.11 215)",
        "warn": "#e8a44c"
      },
      borderRadius: { DEFAULT: "0.25rem", sm: "0.125rem", md: "0.375rem", lg: "0.5rem", xl: "0.75rem", "2xl": "1rem", full: "9999px" },
      spacing: { "container-padding": "20px", unit: "4px", gutter: "16px", "gutter-lg": "24px", "margin-page": "24px", "stack-sm": "8px", "stack-md": "16px", "stack-lg": "24px" },
      fontFamily: {
        sans: ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        // 标题 Space Grotesk（现代几何）/ 正文 Inter / 数据 JetBrains Mono（latin 本地 woff2，中文回退 Noto Sans SC → 雅黑防宋体）
        "label-caps": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "display-lg": ['Space Grotesk', 'Hanken Grotesk', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "display-md": ['Space Grotesk', 'Hanken Grotesk', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "headline-md": ['Space Grotesk', 'Hanken Grotesk', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "headline-sm": ['Space Grotesk', 'Hanken Grotesk', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "title-sm": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "body-md": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "body-sm": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "data-md": ['JetBrains Mono', 'Noto Sans SC', 'Microsoft YaHei', 'monospace'],
        "data-lg": ['JetBrains Mono', 'Noto Sans SC', 'Microsoft YaHei', 'monospace'],
        "data-sm": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "data-tabular": ['JetBrains Mono', 'Noto Sans SC', 'Microsoft YaHei', 'monospace'],
        "label-xs": ['Inter', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif'],
        "display-data": ['Space Grotesk', 'Hanken Grotesk', 'Noto Sans SC', 'Microsoft YaHei', 'PingFang SC', 'sans-serif']
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
        "body-md": ["15px", { "lineHeight": "24px", "fontWeight": "400" }],
        "body-sm": ["12px", { "lineHeight": "16px", "fontWeight": "400" }],
        "data-sm": ["12px", { "lineHeight": "16px", "fontWeight": "400" }],
        "data-tabular": ["14px", { "lineHeight": "20px", "fontWeight": "500", "letterSpacing": "-0.01em" }],
        "display-data": ["32px", { "lineHeight": "40px", "fontWeight": "700" }]
      }
    }
  }
}
