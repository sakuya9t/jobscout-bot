# JobScout — UI Design Specification

**Version:** 1.0  
**Date:** June 25, 2026  
**Application:** JobScout — AI-Powered Job Hunting Web Application  
**Scope:** Full design system covering color, typography, spacing, elevation, iconography, components, and screen-level specifications for both Light and Dark themes.

---

## 1. Design Principles

JobScout's visual language is built on four core principles:

1. **Clarity first.** Information hierarchy must be immediately legible. Match scores, job titles, and action buttons should never compete for attention.
2. **Purposeful density.** The app surfaces a large volume of job data; layouts must be information-dense without feeling cluttered.
3. **Accessible contrast.** All text and interactive elements meet WCAG 2.1 AA contrast requirements in both themes.
4. **Consistent theming.** Every token — color, shadow, border — has a light and dark variant. No element is hard-coded to a single theme.

---

## 2. Color System

### 2.1 Primitive Palette

These are the raw color values from which all semantic tokens are derived. They are never used directly in component code; always reference a semantic token instead.

| Swatch Name | Hex Value | Usage Context |
|-------------|-----------|---------------|
| Indigo 400 | `#818CF8` | Dark-theme primary accent |
| Indigo 500 | `#6366F1` | Light-theme primary accent, buttons |
| Indigo 600 | `#4F46E5` | Hover state for primary buttons |
| Indigo 700 | `#4338CA` | Active/pressed state |
| Emerald 400 | `#34D399` | Dark-theme success, win % badge |
| Emerald 500 | `#10B981` | Light-theme success, win % badge |
| Emerald 600 | `#059669` | Hover on success elements |
| Red 500 | `#EF4444` | Destructive actions, error states |
| Red 400 | `#F87171` | Dark-theme error |
| Amber 500 | `#F59E0B` | Warning states |
| Gray 50 | `#F9FAFB` | Lightest surface |
| Gray 100 | `#F3F4F6` | Input backgrounds (light) |
| Gray 200 | `#E5E7EB` | Borders (light) |
| Gray 300 | `#D1D5DB` | Disabled text (light) |
| Gray 400 | `#9CA3AF` | Placeholder text (light) |
| Gray 500 | `#6B7280` | Secondary text (light) |
| Gray 700 | `#374151` | Body text (light) |
| Gray 900 | `#111827` | Heading text (light) |
| Slate 800 | `#1E293B` | — |
| GitHub Dark 1 | `#0D1117` | Darkest background |
| GitHub Dark 2 | `#161B22` | Card / sidebar surface |
| GitHub Dark 3 | `#21262D` | Elevated card surface |
| GitHub Dark 4 | `#30363D` | Border (dark) |
| GitHub Dark 5 | `#8B949E` | Secondary text (dark) |
| GitHub Dark 6 | `#C9D1D9` | Body text (dark) |
| GitHub Dark 7 | `#F0F6FC` | Heading text (dark) |

---

### 2.2 Semantic Color Tokens

Semantic tokens map a purpose to a primitive. All components reference these tokens.

#### Background Tokens

| Token | Light Value | Dark Value | Purpose |
|-------|-------------|------------|---------|
| `bg-page` | `#F5F7FA` | `#0D1117` | Page / root background |
| `bg-surface` | `#FFFFFF` | `#161B22` | Cards, modals, panels |
| `bg-surface-raised` | `#FFFFFF` | `#21262D` | Elevated cards, dropdowns |
| `bg-sidebar` | `#FFFFFF` | `#161B22` | Navigation sidebar |
| `bg-sidebar-active` | `#EEF2FF` | `#1F2937` | Active nav item background |
| `bg-input` | `#F9FAFB` | `#0D1117` | Form input background |
| `bg-input-hover` | `#F3F4F6` | `#161B22` | Input hover state |
| `bg-overlay` | `rgba(0,0,0,0.4)` | `rgba(0,0,0,0.6)` | Modal backdrop |
| `bg-tag` | `#EEF2FF` | `#1E2A3A` | Chip / tag background |
| `bg-badge-success` | `#D1FAE5` | `#064E3B` | Success badge fill |
| `bg-badge-warning` | `#FEF3C7` | `#451A03` | Warning badge fill |
| `bg-badge-error` | `#FEE2E2` | `#450A0A` | Error / rejected badge fill |
| `bg-badge-neutral` | `#F3F4F6` | `#21262D` | Neutral / applied badge fill |

#### Border Tokens

| Token | Light Value | Dark Value | Purpose |
|-------|-------------|------------|---------|
| `border-default` | `#E5E7EB` | `#30363D` | Default card / panel border |
| `border-strong` | `#D1D5DB` | `#484F58` | Input border, table divider |
| `border-focus` | `#6366F1` | `#818CF8` | Focused input ring |
| `border-error` | `#EF4444` | `#F87171` | Error input ring |
| `border-sidebar-active` | `#6366F1` | `#818CF8` | Left bar on active nav item |

#### Text Tokens

| Token | Light Value | Dark Value | Purpose |
|-------|-------------|------------|---------|
| `text-heading` | `#111827` | `#F0F6FC` | H1–H3 headings |
| `text-body` | `#374151` | `#C9D1D9` | Body paragraphs |
| `text-secondary` | `#6B7280` | `#8B949E` | Subtitles, metadata, captions |
| `text-placeholder` | `#9CA3AF` | `#484F58` | Input placeholder text |
| `text-disabled` | `#D1D5DB` | `#30363D` | Disabled field text |
| `text-link` | `#6366F1` | `#818CF8` | Inline links |
| `text-link-hover` | `#4F46E5` | `#A5B4FC` | Hovered link |
| `text-on-primary` | `#FFFFFF` | `#FFFFFF` | Text on filled primary button |
| `text-success` | `#059669` | `#34D399` | Success text, win % score |
| `text-error` | `#DC2626` | `#F87171` | Error messages |
| `text-warning` | `#D97706` | `#FBBF24` | Warning messages |
| `text-score-primary` | `#6366F1` | `#818CF8` | Match score number (e.g. 98/100) |
| `text-score-secondary` | `#10B981` | `#34D399` | Win % label |
| `text-sidebar-active` | `#6366F1` | `#818CF8` | Active nav item label |
| `text-sidebar-inactive` | `#6B7280` | `#8B949E` | Inactive nav item label |
| `text-sidebar-section` | `#9CA3AF` | `#484F58` | Nav section header (e.g. WORKSPACE) |

#### Brand / Interactive Tokens

| Token | Light Value | Dark Value | Purpose |
|-------|-------------|------------|---------|
| `brand-primary` | `#6366F1` | `#818CF8` | Logo, primary accent |
| `brand-primary-hover` | `#4F46E5` | `#A5B4FC` | Hover on brand elements |
| `interactive-primary-bg` | `#6366F1` | `#6366F1` | Filled primary button bg |
| `interactive-primary-bg-hover` | `#4F46E5` | `#4F46E5` | Filled primary button hover |
| `interactive-primary-bg-active` | `#4338CA` | `#4338CA` | Filled primary button pressed |
| `interactive-outline-border` | `#6366F1` | `#818CF8` | Outline button border |
| `interactive-outline-text` | `#6366F1` | `#818CF8` | Outline button text |
| `interactive-outline-hover-bg` | `#EEF2FF` | `#1E2A3A` | Outline button hover fill |
| `interactive-destructive-bg` | `#EF4444` | `#EF4444` | Destructive button bg |
| `interactive-destructive-hover` | `#DC2626` | `#DC2626` | Destructive button hover |

---

## 3. Typography

### 3.1 Typeface

| Role | Family | Fallback Stack |
|------|--------|----------------|
| Primary | **Inter** | `ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif` |
| Monospace | **JetBrains Mono** | `ui-monospace, "Cascadia Code", "Source Code Pro", Menlo, monospace` |

Inter is loaded via Google Fonts at weights 400, 500, 600, and 700. JetBrains Mono is used exclusively for code snippets, ATS tokens, and URL fields.

### 3.2 Type Scale

| Token | Size | Line Height | Weight | Letter Spacing | Usage |
|-------|------|-------------|--------|----------------|-------|
| `text-xs` | 11px | 16px | 400 | +0.02em | Captions, timestamps, fine print |
| `text-sm` | 13px | 20px | 400 | 0 | Secondary body, form labels |
| `text-base` | 15px | 24px | 400 | 0 | Primary body text |
| `text-md` | 16px | 24px | 500 | 0 | Card subtitles, emphasized body |
| `text-lg` | 18px | 28px | 600 | -0.01em | Card headings, section titles |
| `text-xl` | 20px | 28px | 600 | -0.01em | Page subtitles |
| `text-2xl` | 24px | 32px | 700 | -0.02em | Page H2 headings |
| `text-3xl` | 30px | 36px | 700 | -0.02em | Page H1 headings |
| `text-4xl` | 36px | 44px | 800 | -0.03em | Landing page hero headline |
| `text-5xl` | 48px | 56px | 800 | -0.04em | Hero display text (large viewport) |
| `score-display` | 28px | 32px | 700 | -0.02em | Match score number (98/100) |
| `score-label` | 13px | 16px | 600 | +0.02em | Win % label |
| `nav-section` | 11px | 16px | 600 | +0.08em | Sidebar section headers (uppercase) |
| `nav-item` | 14px | 20px | 500 | 0 | Sidebar navigation items |
| `badge` | 12px | 16px | 600 | +0.01em | Status badges |
| `button-sm` | 13px | 20px | 600 | +0.01em | Small buttons |
| `button-md` | 14px | 20px | 600 | +0.01em | Default buttons |
| `button-lg` | 16px | 24px | 600 | 0 | Large / hero CTA buttons |
| `code` | 13px | 20px | 400 | 0 | Monospace code, URLs, tokens |

---

## 4. Spacing & Layout

### 4.1 Spacing Scale

JobScout uses a base-4 spacing scale. All padding, margin, and gap values must be multiples of 4px.

| Token | Value | Common Usage |
|-------|-------|-------------|
| `space-1` | 4px | Icon padding, tight gaps |
| `space-2` | 8px | Inline element gaps, chip padding |
| `space-3` | 12px | Form field internal padding |
| `space-4` | 16px | Card padding (compact), list item padding |
| `space-5` | 20px | Section gaps (tight) |
| `space-6` | 24px | Card padding (default), section gaps |
| `space-8` | 32px | Between cards, section padding |
| `space-10` | 40px | Page section vertical rhythm |
| `space-12` | 48px | Large section gaps |
| `space-16` | 64px | Hero section padding |
| `space-20` | 80px | Landing page section spacing |
| `space-24` | 96px | Hero top/bottom padding |

### 4.2 Layout Grid

| Context | Columns | Gutter | Margin | Max Width |
|---------|---------|--------|--------|-----------|
| Landing page | 12 | 24px | 80px | 1280px |
| App (with sidebar) | Fluid | 24px | 0 | — |
| Form cards | 2 (split) | 16px | — | 720px |
| Job detail | 1 | — | — | 800px |
| Mobile | 4 | 16px | 16px | — |

### 4.3 Application Shell

| Region | Width | Notes |
|--------|-------|-------|
| Sidebar | 240px (fixed) | Collapses to 64px icon-only on `< 1024px` |
| Main content | `calc(100vw - 240px)` | Scrollable, padded `32px` horizontal |
| Topbar (landing) | 100% | Fixed, height 64px |
| Content max-width | 1200px | Centered within main content area |

---

## 5. Border Radius

| Token | Value | Usage |
|-------|-------|-------|
| `radius-sm` | 4px | Badges, chips, small tags |
| `radius-md` | 8px | Buttons, inputs, small cards |
| `radius-lg` | 12px | Cards, panels, dropdowns |
| `radius-xl` | 16px | Large cards, modals |
| `radius-2xl` | 24px | Hero preview cards, feature cards |
| `radius-full` | 9999px | Pills, toggle switches, avatars |

---

## 6. Elevation & Shadow

| Token | Light Value | Dark Value | Usage |
|-------|-------------|------------|-------|
| `shadow-xs` | `0 1px 2px rgba(0,0,0,0.05)` | `0 1px 2px rgba(0,0,0,0.3)` | Subtle card lift |
| `shadow-sm` | `0 1px 3px rgba(0,0,0,0.1), 0 1px 2px rgba(0,0,0,0.06)` | `0 1px 3px rgba(0,0,0,0.4)` | Default card shadow |
| `shadow-md` | `0 4px 6px rgba(0,0,0,0.07), 0 2px 4px rgba(0,0,0,0.06)` | `0 4px 6px rgba(0,0,0,0.5)` | Elevated cards, dropdowns |
| `shadow-lg` | `0 10px 15px rgba(0,0,0,0.1), 0 4px 6px rgba(0,0,0,0.05)` | `0 10px 15px rgba(0,0,0,0.6)` | Modals, popovers |
| `shadow-xl` | `0 20px 25px rgba(0,0,0,0.1), 0 10px 10px rgba(0,0,0,0.04)` | `0 20px 25px rgba(0,0,0,0.7)` | Full-screen overlays |
| `shadow-glow-primary` | `0 0 0 3px rgba(99,102,241,0.2)` | `0 0 0 3px rgba(129,140,248,0.25)` | Focus ring on primary elements |
| `shadow-glow-success` | `0 0 0 3px rgba(16,185,129,0.2)` | `0 0 0 3px rgba(52,211,153,0.25)` | Focus ring on success elements |

---

## 7. Iconography

| Property | Specification |
|----------|--------------|
| Library | [Lucide Icons](https://lucide.dev) (MIT license) |
| Default size | 16×16px (inline), 20×20px (nav), 24×24px (feature cards) |
| Stroke width | 1.5px |
| Color | Inherits `currentColor` from parent text token |
| Active nav icon | `brand-primary` token |
| Inactive nav icon | `text-sidebar-inactive` token |

Key icons used per screen:

| Screen | Icons |
|--------|-------|
| Navbar | `Search`, `Sun`, `Moon`, `Menu` |
| Sidebar | `List`, `FileText`, `Building2`, `Star`, `Settings`, `LogOut` |
| Job Lists | `RefreshCw`, `Filter`, `ChevronDown`, `Bookmark`, `ExternalLink` |
| Job Detail | `ArrowLeft`, `MapPin`, `Briefcase`, `Clock`, `Sparkles`, `RotateCcw` |
| Resume | `Upload`, `File`, `Trash2` |
| Companies | `Plus`, `Globe`, `Trash2` |
| Interests | `Plus`, `Edit2`, `Trash2`, `Info` |
| Login | `Mail`, `Lock`, `Eye`, `EyeOff` |

---

## 8. Component Specifications

### 8.1 Buttons

#### Primary Button (Filled)

| Property | Value |
|----------|-------|
| Background | `interactive-primary-bg` |
| Text color | `text-on-primary` |
| Font | `button-md`, weight 600 |
| Padding | `10px 20px` (md), `8px 16px` (sm), `14px 28px` (lg) |
| Border radius | `radius-md` (8px) |
| Border | none |
| Hover bg | `interactive-primary-bg-hover` |
| Active bg | `interactive-primary-bg-active` |
| Disabled opacity | 50%, `cursor-not-allowed` |
| Focus ring | `shadow-glow-primary` |
| Icon gap | `space-2` (8px) left of label |

#### Outline Button

| Property | Value |
|----------|-------|
| Background | transparent |
| Text color | `interactive-outline-text` |
| Border | 1.5px solid `interactive-outline-border` |
| Hover bg | `interactive-outline-hover-bg` |
| All other properties | Same as Primary Button |

#### Destructive Button

| Property | Value |
|----------|-------|
| Background | `interactive-destructive-bg` |
| Text color | `#FFFFFF` |
| Hover bg | `interactive-destructive-hover` |
| All other properties | Same as Primary Button |

#### Ghost Button

| Property | Value |
|----------|-------|
| Background | transparent |
| Text color | `text-secondary` |
| Border | none |
| Hover bg | `bg-input-hover` |
| All other properties | Same as Primary Button |

---

### 8.2 Form Inputs

| Property | Value |
|----------|-------|
| Background | `bg-input` |
| Border | 1.5px solid `border-strong` |
| Border radius | `radius-md` (8px) |
| Padding | `10px 14px` |
| Font size | `text-sm` (13px) |
| Text color | `text-body` |
| Placeholder color | `text-placeholder` |
| Focus border | `border-focus` |
| Focus ring | `shadow-glow-primary` |
| Error border | `border-error` |
| Disabled bg | `bg-surface` at 60% opacity |
| Height (single-line) | 40px |
| Textarea min-height | 96px |
| Label font | `text-sm`, weight 500, `text-secondary` |
| Label margin-bottom | `space-1` (4px) |
| Helper text font | `text-xs`, `text-secondary` |
| Error text font | `text-xs`, `text-error` |

#### Dropdown / Select

Same as text input. Append a `ChevronDown` icon (16px, `text-secondary`) flush right with `space-3` padding. On open, the dropdown panel uses `bg-surface-raised`, `border-default`, `shadow-md`, `radius-lg`.

---

### 8.3 Cards

| Property | Value |
|----------|-------|
| Background | `bg-surface` |
| Border | 1px solid `border-default` |
| Border radius | `radius-xl` (16px) |
| Padding | `space-6` (24px) |
| Shadow | `shadow-sm` |
| Hover shadow (interactive cards) | `shadow-md` |
| Transition | `box-shadow 150ms ease, border-color 150ms ease` |

**Job Listing Card** additionally has:
- A left accent border (4px, `brand-primary`) on hover
- Score block right-aligned: `score-display` font for number, `score-label` for win %
- Action row at bottom: `Mark applied` outline button + `View posting →` ghost link, separated by `space-3`

---

### 8.4 Badges / Status Chips

| Status | Light bg | Light text | Dark bg | Dark text | Label |
|--------|----------|------------|---------|-----------|-------|
| Applied | `#EEF2FF` | `#6366F1` | `#1E2A3A` | `#818CF8` | Applied |
| Interview | `#D1FAE5` | `#059669` | `#064E3B` | `#34D399` | Interview |
| Offer | `#D1FAE5` | `#059669` | `#064E3B` | `#34D399` | Offer |
| Rejected | `#FEE2E2` | `#DC2626` | `#450A0A` | `#F87171` | Rejected |
| Screened out | `#FEF3C7` | `#D97706` | `#451A03` | `#FBBF24` | Screened out |
| New | `#EEF2FF` | `#6366F1` | `#1E2A3A` | `#818CF8` | New |

Badge properties: `radius-full`, `text-xs` weight 600, padding `3px 10px`, no border.

---

### 8.5 Navigation Sidebar

| Property | Value |
|----------|-------|
| Width | 240px |
| Background | `bg-sidebar` |
| Right border | 1px solid `border-default` |
| Logo area padding | `space-4` (16px) all sides |
| Logo font | `text-lg`, weight 700, `brand-primary` |
| User email font | `text-xs`, `text-secondary`, below logo |
| Section header font | `nav-section` (11px, 600, uppercase, +0.08em) |
| Section header color | `text-sidebar-section` |
| Section header padding | `space-4` horizontal, `space-3` top, `space-1` bottom |
| Nav item height | 36px |
| Nav item padding | `space-2` vertical, `space-4` horizontal |
| Nav item font | `nav-item` (14px, 500) |
| Nav item icon | 16px, `space-3` right gap |
| Active item bg | `bg-sidebar-active` |
| Active item text | `text-sidebar-active` |
| Active item left bar | 3px solid `border-sidebar-active`, flush left |
| Active item border radius | `0 radius-md radius-md 0` (right side only) |
| Inactive item text | `text-sidebar-inactive` |
| Hover item bg | `bg-input-hover` |
| Log out button | Bottom of sidebar, `space-4` padding, `text-secondary`, `LogOut` icon |
| Collapsible section chevron | `ChevronRight` / `ChevronDown`, 16px, `text-secondary` |

---

### 8.6 Top Navigation Bar (Landing Page)

| Property | Value |
|----------|-------|
| Height | 64px |
| Background | `bg-surface` |
| Bottom border | 1px solid `border-default` |
| Position | Fixed, `z-index: 50` |
| Logo font | `text-xl`, weight 700, `brand-primary` |
| Nav links font | `text-sm`, weight 500, `text-secondary` |
| Nav link hover | `text-heading` |
| Nav link gap | `space-8` (32px) |
| CTA button | Primary button, size `sm` |
| Theme toggle | Pill switch, 48×26px, `radius-full` |
| Theme toggle bg (light) | `#E5E7EB` with white knob |
| Theme toggle bg (dark) | `#6366F1` with white knob |
| Toggle icon | `Sun` (light) / `Moon` (dark), 14px |

---

### 8.7 Theme Toggle Switch

The theme toggle is a pill-shaped switch present in both the landing page navbar and the authenticated app navbar.

| Property | Value |
|----------|-------|
| Width | 56px |
| Height | 28px |
| Border radius | `radius-full` |
| Track bg (light mode) | `#E5E7EB` |
| Track bg (dark mode) | `#6366F1` |
| Knob size | 22×22px |
| Knob bg | `#FFFFFF` |
| Knob shadow | `shadow-sm` |
| Knob transition | `transform 200ms cubic-bezier(0.4, 0, 0.2, 1)` |
| Knob position (light) | `translateX(3px)` |
| Knob position (dark) | `translateX(29px)` |
| Icon inside knob | `Sun` (light, amber-500) / `Moon` (dark, indigo-400), 12px |

---

### 8.8 Score Display Block

Used in Job List cards and Job Detail header.

| Property | Value |
|----------|-------|
| Score number font | 28px, weight 700, `text-score-primary` |
| "/100" suffix font | 16px, weight 500, `text-secondary` |
| Win % font | `score-label` (13px, 600), `text-score-secondary` |
| Win % badge | `bg-badge-success`, `radius-full`, padding `2px 8px` |
| Alignment | Right-aligned block, score number top, win % below |

---

### 8.9 Modal / Dialog

| Property | Value |
|----------|-------|
| Backdrop | `bg-overlay`, `backdrop-blur: 4px` |
| Panel bg | `bg-surface` |
| Panel border | 1px solid `border-default` |
| Panel border radius | `radius-xl` (16px) |
| Panel shadow | `shadow-xl` |
| Panel max-width | 480px (sm), 640px (md), 800px (lg) |
| Panel padding | `space-6` (24px) |
| Close button | Top-right, `X` icon 20px, ghost button |
| Header font | `text-2xl`, weight 700, `text-heading` |
| Animation | Scale from 95% + fade in, 150ms ease-out |

---

### 8.10 Notification / Toast

| Property | Value |
|----------|-------|
| Position | Bottom-right, `space-6` from edges |
| Width | 360px |
| Background | `bg-surface-raised` |
| Border | 1px solid `border-default` |
| Border radius | `radius-lg` (12px) |
| Shadow | `shadow-lg` |
| Icon | 20px, left-aligned, colored by type |
| Title font | `text-sm`, weight 600, `text-heading` |
| Body font | `text-sm`, `text-secondary` |
| Duration | 4000ms auto-dismiss |
| Animation | Slide in from right + fade, 200ms ease-out |

---

## 9. Motion & Animation

| Token | Value | Usage |
|-------|-------|-------|
| `duration-fast` | 100ms | Micro-interactions (checkbox, toggle) |
| `duration-default` | 150ms | Hover states, button presses |
| `duration-moderate` | 200ms | Dropdowns, tooltips |
| `duration-slow` | 300ms | Modals, page transitions |
| `ease-default` | `cubic-bezier(0.4, 0, 0.2, 1)` | Most transitions (Material standard) |
| `ease-in` | `cubic-bezier(0.4, 0, 1, 1)` | Elements leaving the screen |
| `ease-out` | `cubic-bezier(0, 0, 0.2, 1)` | Elements entering the screen |
| `ease-spring` | `cubic-bezier(0.34, 1.56, 0.64, 1)` | Toggle knob, score counter |

All animations must respect `prefers-reduced-motion: reduce` — disable transitions and use instant state changes when this media query is active.

---

## 10. Screen-Level Specifications

### 10.1 Landing Page

| Section | Details |
|---------|---------|
| **Navbar** | Fixed 64px, logo left, nav links center, "Log in" ghost + "Get Started Free" primary button right, theme toggle pill far right |
| **Hero** | Full-width, `space-24` top padding. Headline: `text-5xl` / `text-4xl` (responsive), two lines. Subtitle: `text-lg`, `text-secondary`, max-width 560px. CTAs: "Start for free" (primary lg) + "See how it works" (outline lg), `space-4` gap. Dashboard preview: rounded mockup image with `shadow-xl`, `radius-2xl`, indigo glow, below CTAs |
| **Feature Cards** | 3-column row, each card: icon (24px, indigo), title (`text-lg`), description (`text-sm`, `text-secondary`), `bg-surface`, `shadow-sm`, `radius-xl`, `space-6` padding |
| **How It Works** | 3-step numbered flow, alternating icon + text layout |
| **CTA Banner** | Full-width indigo gradient section, centered headline + "Get Started Free" white-text button |
| **Footer** | `bg-surface`, `border-default` top border, logo left, nav links, copyright |

### 10.2 Login / Register

| Element | Details |
|---------|---------|
| Background | `bg-page` |
| Card | Centered, max-width 400px, `bg-surface`, `shadow-md`, `radius-xl`, `space-8` padding |
| Heading | "Welcome back" / "Create account", `text-3xl`, weight 700 |
| Subtitle | `text-sm`, `text-secondary` |
| Inputs | Full-width, `space-4` gap between fields |
| Primary CTA | Full-width primary button |
| Divider | "or" with horizontal rules, `text-secondary` |
| Social buttons | Full-width outline buttons, provider icon left |
| Footer link | `text-sm`, `text-secondary` + `text-link` |

### 10.3 Job Lists

| Element | Details |
|---------|---------|
| Page title | `text-3xl`, weight 700, `text-heading` |
| Subtitle | `text-sm`, `text-secondary`, `space-1` below title |
| Filter bar | `bg-surface`, `shadow-xs`, `radius-lg`, `space-4` padding, flex row with `space-3` gaps |
| Tab group | "Top 5 / All matches / All jobs" — active tab: `bg-sidebar-active`, `text-sidebar-active`; inactive: `text-secondary` |
| Stats row | Pill chips, `bg-tag`, `text-xs`, `text-secondary` |
| Job cards | `space-4` vertical gap, hover lifts to `shadow-md` |
| "Recalculate" button | Primary sm, top-right of page header |

### 10.4 Job Detail

| Element | Details |
|---------|---------|
| Back link | `← Back to job list`, `text-sm`, `text-link`, top-right |
| Header card | Company logo (40px, `radius-md`), title (`text-2xl`), metadata row (`text-sm`, `text-secondary`), score block right-aligned |
| "How you line up" card | `text-lg` heading, `text-base` body |
| "AI application kit" card | Heading + description + `Regenerate` primary button + model attribution (`text-xs`, `text-secondary`), then `What this role is looking for` subheading + bullet list |

### 10.5 Resume & Companies

| Element | Details |
|---------|---------|
| File input row | Native file input + `Upload / replace` primary button, flex row |
| Active file | Indigo link + gray metadata + `Remove` outline button, `space-4` top margin |
| Companies form | Two-column grid (Name + Careers URL, ATS + ATS token), `space-3` gap, `Add company` primary button |
| Company list rows | Divider-separated, company name (`text-sm`, weight 600, `text-link`), ATS badge, URL (`text-xs`, `text-secondary`), `Remove` outline button right |

### 10.6 Interests

| Element | Details |
|---------|---------|
| Form layout | Two-column grid for all fields except Notes (full-width) |
| Notes textarea | Min-height 80px, full-width |
| `Add interest` button | Primary, full-width on mobile / auto-width on desktop |
| Existing interests | Card list rows: label (`text-sm`, weight 600) + metadata (`text-xs`, `text-secondary`) + `Edit` + `Delete` buttons right-aligned |

---

## 11. Responsive Breakpoints

| Breakpoint | Min Width | Layout Changes |
|------------|-----------|----------------|
| `xs` | 0px | Single column, full-width cards |
| `sm` | 640px | Two-column forms |
| `md` | 768px | Sidebar collapses to icon-only (64px) |
| `lg` | 1024px | Full sidebar (240px) restored |
| `xl` | 1280px | Max content width capped at 1200px |
| `2xl` | 1536px | Landing page hero scales up |

---

## 12. Accessibility

| Requirement | Implementation |
|-------------|----------------|
| Color contrast (text) | All `text-body` / `bg-surface` combinations meet WCAG AA (≥ 4.5:1) |
| Color contrast (large text) | All headings meet WCAG AA (≥ 3:1) |
| Focus indicators | `shadow-glow-primary` on all interactive elements, never removed |
| Keyboard navigation | Full tab order, `Escape` closes modals/dropdowns |
| Screen reader labels | All icon-only buttons have `aria-label`; inputs have associated `<label>` |
| Reduced motion | All CSS transitions wrapped in `@media (prefers-reduced-motion: no-preference)` |
| Theme persistence | User's theme preference stored in `localStorage` and applied before first paint to prevent flash |

---

## 13. Theme Switching Implementation Notes

The theme is controlled by a `data-theme="light"` or `data-theme="dark"` attribute on the `<html>` element. All semantic tokens are defined as CSS custom properties scoped to these selectors:

```css
:root, [data-theme="light"] {
  --bg-page: #F5F7FA;
  --bg-surface: #FFFFFF;
  --brand-primary: #6366F1;
  /* ... all light tokens */
}

[data-theme="dark"] {
  --bg-page: #0D1117;
  --bg-surface: #161B22;
  --brand-primary: #818CF8;
  /* ... all dark tokens */
}
```

The toggle switch dispatches a `theme-change` custom event. A script in `<head>` (before render) reads `localStorage.getItem('theme')` and sets `data-theme` immediately to prevent a flash of unstyled content (FOUC). If no preference is stored, the system preference via `prefers-color-scheme` is used as the default.

---

*End of JobScout Design Specification v1.0*
