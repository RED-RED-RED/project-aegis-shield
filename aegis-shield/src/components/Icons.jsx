// src/components/Icons.jsx
// KESTREL line-icon set. One grid, one stroke weight, currentColor.
// <Icon name="map" size={18} />
const PATHS = {
  map: (<><path d="M9 4 3 6v14l6-2 6 2 6-2V4l-6 2-6-2Z"/><path d="M9 4v14M15 6v14"/></>),
  threat: (<><path d="M12 3 2 20h20L12 3Z"/><path d="M12 10v5M12 18h.01"/></>),
  signal: (<><path d="M5 9a9 9 0 0 1 14 0M8 12a5 5 0 0 1 8 0"/><circle cx="12" cy="16" r="1.3" fill="currentColor" stroke="none"/></>),
  flight: (<><path d="M3 19c6 0 4-14 10-14 2.5 0 3 2 1.5 3.2"/><circle cx="19" cy="6" r="2"/><path d="M5 19l-2 2M9 19H5"/></>),
  registry: (<><rect x="3" y="4" width="18" height="16" rx="1.5"/><path d="M7 9h4M7 13h7M7 16h5"/><circle cx="16.5" cy="9" r="1.5"/></>),
  system: (<><path d="M5 7h14M5 12h14M5 17h14"/><circle cx="9" cy="7" r="2"/><circle cx="15" cy="12" r="2"/><circle cx="8" cy="17" r="2"/></>),
  user: (<><circle cx="12" cy="8" r="3.2"/><path d="M5 20c0-3.6 3-6 7-6s7 2.4 7 6"/></>),
  flag: (<><path d="M5 21V4M5 4h11l-2 4 2 4H5"/></>),
  pin: (<><path d="M12 21s7-6.2 7-11a7 7 0 1 0-14 0c0 4.8 7 11 7 11Z"/><circle cx="12" cy="10" r="2.4"/></>),
  crosshair: (<><circle cx="12" cy="12" r="7"/><path d="M12 2v4M12 18v4M2 12h4M18 12h4"/></>),
  gps: (<><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3"/><circle cx="12" cy="12" r="8" strokeDasharray="2 3"/></>),
  pulse: (<><path d="M3 12h4l2-6 4 12 2-6h6"/></>),
}

export default function Icon({ name, size = 18, strokeWidth = 1.6, style, className }) {
  const body = PATHS[name]
  if (!body) return null
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none"
      stroke="currentColor" strokeWidth={strokeWidth} strokeLinecap="round"
      strokeLinejoin="round" style={style} className={className} aria-hidden="true">
      {body}
    </svg>
  )
}
