/** @type {import('tailwindcss').Config} */
// 从 app/web/templates/index.html 的内联 tailwind.config 迁移而来（C1 Tailwind 静态化）
module.exports = {
  darkMode: "class",
  content: [
    "./app/web/templates/index.html",
    "./app/web/static/app.js", // 拆文件后的前端脚本（内容扫描类名）
  ],
  theme: {
    extend: {
      colors: {
        "primary": "oklch(0.24 0.02 258)",
        "accent": "oklch(0.55 0.19 256)",
        "accent-soft": "oklch(0.55 0.19 256 / 0.10)",
        "secondary": "oklch(0.50 0.012 256)",
        "paper": "oklch(0.985 0.004 250)",
        "paper-raised": "oklch(0.995 0.002 250)",
        "background": "oklch(0.985 0.004 250)",
        "surface": "oklch(0.965 0.005 250)",
        "surface-raised": "oklch(0.995 0.002 250)",
        "surface-container": "oklch(0.94 0.006 250)",
        "on-surface": "oklch(0.32 0.018 258)",
        "on-surface-variant": "oklch(0.50 0.012 256)",
        "outline": "oklch(0.88 0.006 250)",
        "outline-strong": "oklch(0.76 0.010 252)",
        "ink": "oklch(0.94 0.006 250)",
        "graphite": "oklch(0.24 0.02 258)",
        "up": "oklch(0.62 0.19 30)",
        "down": "oklch(0.50 0.115 152)",
        "error": "oklch(0.62 0.19 27)",
        "success": "oklch(0.50 0.115 152)",
        "tertiary": "oklch(0.60 0.13 70)",
        "violet": "oklch(0.55 0.14 290)",
        "rose": "oklch(0.60 0.17 12)",
        "cyan": "oklch(0.55 0.11 215)",
        "warn": "oklch(0.60 0.13 70)"
      },
      borderRadius: { DEFAULT: "6px", sm: "4px", md: "6px", lg: "10px", xl: "10px", "2xl": "12px", full: "9999px" },
      spacing: { "container-padding": "16px", unit: "4px", gutter: "12px", "gutter-lg": "24px" },
      fontFamily: {
        sans: ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "label-caps": ['IBM Plex Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "display-lg": ['Space Grotesk', 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', 'serif'],
        "display-md": ['Space Grotesk', 'Noto Serif SC', 'Songti SC', 'STSong', 'SimSun', 'serif'],
        "body-md": ['Inter', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'sans-serif'],
        "data-md": ['IBM Plex Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "data-lg": ['IBM Plex Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "data-sm": ['IBM Plex Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace'],
        "display-data": ['IBM Plex Mono', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei', 'monospace']
      },
      fontSize: {
        "label-caps": ["12px", { "lineHeight": "16px", "letterSpacing": "0.06em", "fontWeight": "600" }],
        "display-lg": ["32px", { "lineHeight": "40px", "letterSpacing": "-0.02em", "fontWeight": "700" }],
        "display-md": ["24px", { "lineHeight": "32px", "letterSpacing": "-0.015em", "fontWeight": "700" }],
        "data-md": ["14px", { "lineHeight": "18px", "fontWeight": "500" }],
        "data-lg": ["19px", { "lineHeight": "24px", "fontWeight": "600" }],
        "body-md": ["15px", { "lineHeight": "22px", "fontWeight": "400" }],
        "data-sm": ["13px", { "lineHeight": "18px", "fontWeight": "400" }],
        "display-data": ["30px", { "lineHeight": "38px", "fontWeight": "700" }]
      }
    }
  }
}
