---
name: Institutional Intelligence
colors:
  surface: '#f7f9fb'
  surface-dim: '#d8dadc'
  surface-bright: '#f7f9fb'
  surface-container-lowest: '#ffffff'
  surface-container-low: '#f2f4f6'
  surface-container: '#eceef0'
  surface-container-high: '#e6e8ea'
  surface-container-highest: '#e0e3e5'
  on-surface: '#191c1e'
  on-surface-variant: '#43474f'
  inverse-surface: '#2d3133'
  inverse-on-surface: '#eff1f3'
  outline: '#737780'
  outline-variant: '#c3c6d1'
  surface-tint: '#3a5f94'
  primary: '#001e40'
  on-primary: '#ffffff'
  primary-container: '#003366'
  on-primary-container: '#799dd6'
  inverse-primary: '#a7c8ff'
  secondary: '#0058bc'
  on-secondary: '#ffffff'
  secondary-container: '#0070eb'
  on-secondary-container: '#fefcff'
  tertiary: '#381300'
  on-tertiary: '#ffffff'
  tertiary-container: '#592300'
  on-tertiary-container: '#d8885c'
  error: '#ba1a1a'
  on-error: '#ffffff'
  error-container: '#ffdad6'
  on-error-container: '#93000a'
  primary-fixed: '#d5e3ff'
  primary-fixed-dim: '#a7c8ff'
  on-primary-fixed: '#001b3c'
  on-primary-fixed-variant: '#1f477b'
  secondary-fixed: '#d8e2ff'
  secondary-fixed-dim: '#adc6ff'
  on-secondary-fixed: '#001a41'
  on-secondary-fixed-variant: '#004493'
  tertiary-fixed: '#ffdbca'
  tertiary-fixed-dim: '#ffb690'
  on-tertiary-fixed: '#341100'
  on-tertiary-fixed-variant: '#723610'
  background: '#f7f9fb'
  on-background: '#191c1e'
  surface-variant: '#e0e3e5'
  success-gains: '#2ECC71'
  danger-losses: '#E74C3C'
  warning-amber: '#F39C12'
  surface-border: '#E2E8F0'
  text-muted: '#64748B'
typography:
  display-lg:
    fontFamily: Hanken Grotesk
    fontSize: 32px
    fontWeight: '700'
    lineHeight: 40px
    letterSpacing: -0.02em
  headline-md:
    fontFamily: Hanken Grotesk
    fontSize: 24px
    fontWeight: '600'
    lineHeight: 32px
  headline-sm:
    fontFamily: Hanken Grotesk
    fontSize: 18px
    fontWeight: '600'
    lineHeight: 24px
  body-lg:
    fontFamily: Inter
    fontSize: 16px
    fontWeight: '400'
    lineHeight: 24px
  body-md:
    fontFamily: Inter
    fontSize: 14px
    fontWeight: '400'
    lineHeight: 20px
  body-sm:
    fontFamily: Inter
    fontSize: 12px
    fontWeight: '400'
    lineHeight: 16px
  data-tabular:
    fontFamily: JetBrains Mono
    fontSize: 14px
    fontWeight: '500'
    lineHeight: 20px
    letterSpacing: -0.01em
  label-caps:
    fontFamily: Inter
    fontSize: 11px
    fontWeight: '700'
    lineHeight: 16px
    letterSpacing: 0.05em
rounded:
  sm: 0.125rem
  DEFAULT: 0.25rem
  md: 0.375rem
  lg: 0.5rem
  xl: 0.75rem
  full: 9999px
spacing:
  unit: 4px
  gutter: 16px
  margin-page: 24px
  container-padding: 20px
  stack-sm: 8px
  stack-md: 16px
  stack-lg: 24px
---

## Brand & Style

This design system is built for an AI-powered quantitative investment research terminal. It balances the "high-tech" precision of artificial intelligence with the "reliable" authority of institutional finance. The personality is professional, analytical, and fast-paced, designed to reduce cognitive load for analysts managing complex data streams.

The design style is **Corporate / Modern**, characterized by:
- **Modular Data Architecture:** Information is encapsulated in refined containers to create clear mental models of different data types (AI narrative vs. hard quant metrics).
- **Functional Density:** Optimized whitespace that allows for high data density without feeling cluttered.
- **Precision Visuals:** Use of subtle borders and deliberate tonal layering to define hierarchy rather than heavy shadows or vibrant gradients.
- **Trust-Oriented Aesthetics:** A palette and typography choice that evokes stability, accuracy, and institutional grade performance.

## Colors

The color system is rooted in a "Terminal Blue" hierarchy. The **Primary Navy (#003366)** provides the institutional foundation, used for structural navigation and headers to ground the interface. The **Vibrant Blue (#007AFF)** is reserved for interactivity—primary actions, active states, and data highlights.

Functional colors follow strict financial industry standards:
- **Success/Gains:** Used for positive ROI, "Buy" signals, and system "Healthy" states.
- **Danger/Losses:** Used for negative performance, outflows, and critical system errors.
- **Warning:** Used for cautious signals and pending pipeline states.

The neutral palette leverages cool grays (`#F8FAFC` to `#1E293B`) to maintain a professional, high-tech environment that keeps the user focused on the data.

## Typography

The typography system is designed for maximum legibility in data-dense environments. 

1. **Hanken Grotesk** is used for headlines and branding to provide a sharp, contemporary "fintech" feel.
2. **Inter** is the primary workhorse for body text, descriptions, and UI labels, chosen for its exceptional readability at small sizes.
3. **JetBrains Mono** is introduced for tabular data, ticker codes, and timestamps. Its monospaced nature ensures that columns of numbers align perfectly, allowing for faster vertical scanning of financial metrics.

For mobile devices, any headline exceeding 24px should scale down by 15% to maintain visual balance on smaller screens.

## Layout & Spacing

This design system utilizes a **12-column Fixed Grid** for desktop (max-width 1440px) to ensure that complex data visualizations maintain their intended proportions. On smaller screens, the layout transitions to a fluid model with defined margins.

- **The 4px Base Unit:** All spacing must be a multiple of 4px.
- **Sectioning:** Major modules like "AI Track Report" and "Tracking Monitor" use a `stack-lg` (24px) separation. 
- **Data Tables:** Use a compact vertical rhythm with 8px internal cell padding to maximize information density without sacrificing touch/click targets.
- **Breakpoints:** 
    - Desktop: 1280px+ (12 columns, 24px margins)
    - Tablet: 768px - 1279px (8 columns, 16px margins)
    - Mobile: <767px (4 columns, 12px margins, vertical stack)

## Elevation & Depth

Depth is conveyed through **Tonal Layers** rather than heavy shadows. This maintains a clean, "flat-modern" aesthetic suitable for professional tools.

- **Level 0 (Background):** The base application surface (`#F8FAFC`).
- **Level 1 (Cards/Modules):** Pure white background (`#FFFFFF`) with a subtle `1px` border in `surface-border`. Use an extremely soft, low-opacity shadow (4% opacity) to provide a "lift" from the background.
- **Level 2 (Modals/Popovers):** Elevated surfaces that use a slightly more pronounced shadow and a background blur (12px) if positioned over live data.
- **Dividers:** Horizontal and vertical rules should be `1px` and use `surface-border`. Avoid using pure black or high-contrast lines to separate data.

## Shapes

The shape language is **Soft (0.25rem)**. This provides a professional and precise look that feels more modern than sharp corners but remains more serious and "institutional" than fully rounded/pill-shaped systems.

- **Standard Buttons & Inputs:** 4px (`rounded-md`).
- **Data Cards:** 8px (`rounded-lg`).
- **Status Tags/Chips:** 4px or fully rounded if used as a indicator dot.
- **Interactive Graphs:** Line strokes should be 2px with rounded caps to maintain a polished feel.

## Components

### Buttons
- **Primary:** Deep Blue background with white text. High-contrast for main terminal actions.
- **Secondary/Ghost:** Transparent background with `surface-border`. Used for utility actions (e.g., "Export", "Refresh").
- **Signal Buttons:** Small, compact buttons for "Buy/Hold/Sell" using the success/danger palette.

### Cards
Cards are the primary container for "AI Track Report" and "Top Picks." Every card must have a 20px internal padding and a clear header section separated by a subtle divider. AI-generated sections should feature a subtle "AI-Quant" icon in the corner to denote provenance.

### Data Tables (Tracking Monitor)
- **Header:** Sticky headers with a light gray background and `label-caps` typography.
- **Rows:** Alternate row striping is discouraged; use subtle hover states and 1px dividers instead.
- **Metrics:** All percentage changes must include a `+` or `-` sign and be color-coded.

### Input Fields
Clean, minimalist borders that turn `secondary-blue` on focus. Use `body-sm` for help text and `label-caps` for field labels to maintain the professional terminal aesthetic.

### Status Chips
Compact tags used for "Pipeline Status" or "Sector." Use low-saturation backgrounds with high-saturation text of the same hue for a sophisticated look (e.g., light green background with dark green text for "Stable").