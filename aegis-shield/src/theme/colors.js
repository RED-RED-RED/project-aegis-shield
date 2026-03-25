// src/theme/colors.js
// Single source of truth for the Tactical Olive palette.
// CSS custom properties in index.css mirror these values.
// Import this file in any component that needs hex values in JS
// (e.g. Leaflet template literals, canvas draws).

export const colors = {
  // Backgrounds
  bgPrimary:   '#151814',   // main app background — dark olive-black
  bgSurface:   '#1E2420',   // cards, panels, sidebars
  bgElevated:  '#252C24',   // modals, dropdowns, hover states
  bgBorder:    '#2A3028',   // dividers and borders

  // Primary accent
  steelBlue:      '#4A6FA5',   // primary interactive — links, active states, node indicators
  steelBlueLight: '#7A9FD0',   // hover states, highlights
  steelBlueDim:   '#2A4060',   // subtle blue tint backgrounds

  // UI accent
  olive:    '#7A8C4E',   // secondary accent — online indicators, labels, headings
  oliveDim: '#3A4828',   // subtle olive tint backgrounds

  // Threat levels
  threatLow:        '#4A7C59',   // LOW — muted sage green
  threatLowBg:      '#1A2A1A',
  threatLowBorder:  '#3A6040',

  threatMed:        '#C8924A',   // MED — amber/tan
  threatMedBg:      '#2A2010',
  threatMedBorder:  '#6B4E20',

  threatHigh:       '#E06060',   // HIGH — muted red
  threatHighBg:     '#2A1414',
  threatHighBorder: '#6B2020',

  // Text
  textPrimary:   '#D8E0D0',   // main text
  textSecondary: '#9BAAB8',   // timestamps, labels
  textMuted:     '#5A6A58',   // disabled, placeholder, offline

  // Node status
  nodeOnline:  '#4A6FA5',   // online node dot  (= steelBlue)
  nodeOffline: '#3A4040',   // offline node dot
  nodeWarning: '#C8924A',   // degraded node dot (= threatMed)
}
