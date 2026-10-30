/** One small inline-SVG icon set (stroke icons, 24×24). No icon font, no network, CSP-safe. */
const PATHS = {
  ask: "M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z|M9.5 10h5|M9.5 13.5h3",
  documents: "M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z|M14 3v5h5|M9 13h6|M9 17h4",
  usage: "M4 20V10|M10 20V4|M16 20v-7|M22 20H2",
  shield: "M12 3 4 6v6c0 4.5 3.2 8 8 9 4.8-1 8-4.5 8-9V6Z",
  audit: "M12 3 4 6v6c0 4.5 3.2 8 8 9 4.8-1 8-4.5 8-9V6Z|M9 12l2 2 4-4",
  system: "M4 6h16v5H4Z|M4 13h16v5H4Z|M8 8.5h.01|M8 15.5h.01",
  menu: "M4 6h16|M4 12h16|M4 18h16",
  close: "M6 6l12 12|M18 6 6 18",
  sun: "M12 8a4 4 0 1 0 0 8 4 4 0 0 0 0-8Z|M12 2v2|M12 20v2|M4.9 4.9l1.4 1.4|M17.7 17.7l1.4 1.4|M2 12h2|M20 12h2|M4.9 19.1l1.4-1.4|M17.7 6.3l1.4-1.4",
  moon: "M21 13A9 9 0 1 1 11 3a7 7 0 0 0 10 10Z",
  monitor: "M3 5h18v11H3Z|M8 20h8|M12 16v4",
  chevronLeft: "M15 6l-6 6 6 6",
  chevronRight: "M9 6l6 6-6 6",
  logout: "M9 4H5a1 1 0 0 0-1 1v14a1 1 0 0 0 1 1h4|M16 8l4 4-4 4|M20 12H9",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12Z|M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6Z",
  eyeOff: "M3 3l18 18|M10.6 6.1A9.8 9.8 0 0 1 12 5c6.4 0 10 7 10 7a17 17 0 0 1-3.2 4|M6.5 6.7C3.9 8.5 2 12 2 12s3.6 7 10 7c1.6 0 3-.4 4.3-1|M9.9 9.9a3 3 0 0 0 4.2 4.2",
  check: "M5 12.5l4.5 4.5L19 7.5",
  alert: "M12 3 2 20h20Z|M12 10v4|M12 17h.01",
  info: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z|M12 11v5|M12 8h.01",
  upload: "M12 16V4|M7 9l5-5 5 5|M4 20h16",
  building: "M5 21V4a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1v17|M15 9h3a1 1 0 0 1 1 1v11|M3 21h18|M9 8h2|M9 12h2|M9 16h2",
  send: "M22 2 11 13|M22 2l-7 20-4-9-9-4Z",
  refresh: "M20 11A8 8 0 0 0 5.6 7M4 4v4h4|M4 13a8 8 0 0 0 14.4 4M20 20v-4h-4",
  copy: "M9 9h10v11H9Z|M5 15V4h10",
  list: "M8 6h13|M8 12h13|M8 18h13|M3 6h.01|M3 12h.01|M3 18h.01",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  return (
    <svg
      className="icon"
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.75}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {PATHS[name].split("|").map((d) => (
        <path key={d} d={d} />
      ))}
    </svg>
  );
}
