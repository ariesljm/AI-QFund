---
name: Precision Capital
colors:
  surface: '#051424'
  surface-dim: '#051424'
  surface-bright: '#2c3a4c'
  surface-container-lowest: '#010f1f'
  surface-container-low: '#0d1c2d'
  surface-container: '#122131'
  surface-container-high: '#1c2b3c'
  surface-container-highest: '#273647'
  on-surface: '#d4e4fa'
  on-surface-variant: '#c6c6cd'
  inverse-surface: '#d4e4fa'
  inverse-on-surface: '#233143'
  outline: '#909097'
  outline-variant: '#45464d'
  surface-tint: '#bec6e0'
  primary: '#bec6e0'
  on-primary: '#283044'
  primary-container: '#0f172a'
  on-primary-container: '#798098'
  inverse-primary: '#565e74'
  secondary: '#bcc7de'
  on-secondary: '#263143'
  secondary-container: '#3e495d'
  on-secondary-container: '#aeb9d0'
  tertiary: '#4edea3'
  on-tertiary: '#003824'
  tertiary-container: '#001c10'
  on-tertiary-container: '#009365'
  error: '#ffb4ab'
  on-error: '#690005'
  error-container: '#93000a'
  on-error-container: '#ffdad6'
  primary-fixed: '#dae2fd'
  primary-fixed-dim: '#bec6e0'
  on-primary-fixed: '#131b2e'
  on-primary-fixed-variant: '#3f465c'
  secondary-fixed: '#d8e3fb'
  secondary-fixed-dim: '#bcc7de'
  on-secondary-fixed: '#111c2d'
  on-secondary-fixed-variant: '#3c475a'
  tertiary-fixed: '#6ffbbe'
  tertiary-fixed-dim: '#4edea3'
  on-tertiary-fixed: '#002113'
  on-tertiary-fixed-variant: '#005236'
  background: '#051424'
  on-background: '#d4e4fa'
  surface-variant: '#273647'
typography:
  headline-xl:
    fontFamily: Inter
    fontSize: 36px
    fontWeight: '700'
    lineHeight: 44px
    letterSpacing: -0.02em
  headline-lg:
    fontFamily: Inter
    fontSize: 28px
    fontWeight: '600'
    lineHeight: 36px
    letterSpacing: -0.01em
  headline-lg-mobile:
    fontFamily: Inter
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  display-data:
    fontFamily: Geist
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
    letterSpacing: -0.02em
  body-md:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  label-sm:
    fontFamily: Geist
    fontSize: 13px
    fontWeight: '500'
    lineHeight: 18px
    letterSpacing: 0.02em
  subcaption-en:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '500'
    lineHeight: 14px
    letterSpacing: 0.05em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  base: 4px
  xs: 8px
  sm: 12px
  md: 16px
  lg: 24px
  xl: 32px
  container-max: 1440px
  gutter: 20px
---

## Brand & Style
The design system is engineered for high-stakes financial decision-making, where clarity and reliability are paramount. The brand personality is **Reliable, Professional, and Analytical**, catering to institutional and retail investors who require real-time fund data and actionable signals.

The aesthetic follows a **Modern Corporate** style infused with **Refined Glassmorphism**. By using semi-transparent surfaces and sophisticated background blurs, the UI maintains a sense of depth and technical sophistication without compromising the readability of dense financial datasets. The interface should feel like a high-end terminal—precise, calm, and authoritative.

## Colors
This design system utilizes a deep-dark palette to reduce eye strain during prolonged analysis. 
- **Primary & Secondary:** Deep Navy (`#0F172A`) and Slate (`#1E293B`) form the structural base, establishing a "Trust" foundation.
- **Accent/Positive:** Vibrant Green (`#10B981`) is reserved for positive performance, buy signals, and growth indicators.
- **Alert/Negative:** A Professional Red (`#EF4444`) is used sparingly for drawdowns, sell alerts, and critical risk factors.
- **Neutral:** A range of slates is used for secondary text and borders to maintain a low-friction visual hierarchy.

## Typography
The system uses **Inter** for its exceptional readability in dense UI environments. To enhance the "premium" feel, the system pairs Chinese primary labels with English secondary subtitles in a smaller, all-caps Geist font.

- **Data-First:** Financial figures and percentages use **Geist**, a technical mono-spaced inspired sans-serif, to ensure numbers align perfectly in tables.
- **Bilingual Hierarchy:** Primary headers in Chinese (Medium/Bold weight) should be followed by English subtitles (Regular weight, muted color) to provide a sophisticated, international aesthetic.

## Layout & Spacing
The layout utilizes a **12-column fluid grid** for the main dashboard content, transitioning to a single-column stack on mobile. 

- **Density:** Given the data-rich nature of fund recommendations, a "Compact-to-Comfortable" spacing model is used. 16px is the standard gutter between cards.
- **Sidebars:** A fixed 280px left navigation bar houses global controls, while a collapsible right-side drawer provides deep-dive fund metrics.
- **Modular Grids:** Dashboard widgets should span 3, 4, 6, or 12 columns to maintain structural alignment.

## Elevation & Depth
This design system employs a **Glassmorphic Tonal Layering** approach. Depth is created through material properties rather than traditional drop shadows.

- **Background:** Solid `#020617`.
- **Surface (Cards):** Semi-transparent Slate (`#1E293B` at 60% opacity) with a 20px `backdrop-filter: blur()`.
- **Borders:** "Ghost outlines" using 1px solid strokes at 10% white opacity provide definition without visual clutter.
- **Interactions:** Hover states should increase the background opacity of the card and brighten the border stroke, simulating a light source behind the glass.

## Shapes
A **Soft** geometric approach is used to balance the clinical nature of financial data with modern UI trends. 
- **Standard Cards:** 8px (`rounded-md`) corner radius.
- **Action Buttons:** 4px (`rounded-sm`) to maintain a professional, sharp-edged look.
- **Badges/Signals:** 12px or fully pill-shaped to differentiate status indicators from structural layout elements.

## Components
### Buttons
- **Primary:** Solid Primary Navy with high-contrast text. No gradients.
- **Action:** Subtle glass background with vibrant green/red text for "Buy/Sell" signals.

### Data Visualization
- **Line Charts:** Ultra-thin 1.5pt strokes with subtle area gradients (20% opacity to 0%).
- **Candlesticks:** Clean blocks without shadows to ensure price action is the focus.

### Cards & Widgets
- Every card must include a title row: **[Chinese Title] / [English Subtitle]**.
- High-priority signals (e.g., "Daily Recommendation") should feature a glowing 1px border using the accent color.

### Status Indicators (Signals)
- **Positive:** Green dot with a pulse animation for "Live" recommendations.
- **Alert:** Red outline with a subtle warning icon.

### Inputs
- Dark-mode optimized text fields with a 1px border that glows on focus. Use monospaced fonts for numerical input to ensure precision.