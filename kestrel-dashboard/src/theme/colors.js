// src/theme/colors.js
// Single source of truth for the KESTREL palette (modern tactical).
// CSS custom properties in index.css mirror these values.
// Import this file in any component that needs hex values in JS
// (e.g. Leaflet template literals, canvas draws).

export const colors = {
  // Backgrounds — cool graphite with a faint green undertone
  bgPrimary:   '#0E1312',   // main app background
  bgSurface:   '#141A1B',   // cards, panels, sidebars
  bgElevated:  '#1B2325',   // modals, dropdowns, hover states
  bgBorder:    '#243031',   // dividers and borders
  bgBorderHi:  '#38484A',   // brighter hairline / corner brackets

  // Primary accent — interaction / selection
  steelBlue:      '#5C93D6',
  steelBlueLight: '#7AAEE8',
  steelBlueDim:   '#20364C',

  // Brand accent
  olive:    '#A6C255',
  oliveDim: '#59692F',

  // Live / active — used sparingly
  signal:    '#2FE3C6',
  signalDim: '#14433C',

  // Threat levels
  threatLow:        '#51A06A',
  threatLowBg:      '#10211A',
  threatLowBorder:  '#2C5340',

  threatMed:        '#E3A53F',
  threatMedBg:      '#241A0C',
  threatMedBorder:  '#5A431C',

  threatHigh:       '#E8574C',
  threatHighBg:     '#230F0E',
  threatHighBorder: '#5A2420',

  // Text
  textPrimary:   '#DEE6E1',
  textSecondary: '#8DA09C',
  textMuted:     '#51615B',
  textBright:    '#F4F8F5',

  // Node status
  nodeOnline:  '#A6C255',   // online node  (= olive)
  nodeOffline: '#35403D',
  nodeWarning: '#E3A53F',   // degraded node (= threatMed)
}
