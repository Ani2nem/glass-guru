/**
 * The mark: a pane of glass whose crack is a route.
 *
 * Everything this product does is in that one joke - broken glass arrives as a
 * jagged line across a pane, and the system's answer to a jagged line is to read
 * it as a path: a depot dot, the stops, the way through the day. Safety-blue pane
 * on the product's own black, the glint every glass logo earns, and the crack
 * drawn in white with the route's endpoints lit green (rolling) and blue (done).
 *
 * One drawing, two sizes: the favicon is this same SVG serialized as a data URI in
 * index.html, so the tab and the topbar can never drift apart.
 */
export function Logo({ size = 30 }: { size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="Krama"
    >
      <rect width="64" height="64" rx="14" fill="#09091A" />
      <path d="M21 9 L53 17 L45 55 L11 46 Z" fill="#1FBAD6" opacity="0.22" />
      <path
        d="M21 9 L53 17 L45 55 L11 46 Z"
        fill="none"
        stroke="#1FBAD6"
        strokeWidth="3.5"
        strokeLinejoin="round"
      />
      <path
        d="M28 14.5 L23 30"
        stroke="#7FE3F2"
        strokeWidth="3"
        strokeLinecap="round"
        opacity="0.85"
      />
      <path
        d="M17 43 L29 35.5 L34.5 40 L46 24"
        fill="none"
        stroke="#FFFFFF"
        strokeWidth="3.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="17" cy="43" r="4" fill="#06C167" />
      <circle cx="34.5" cy="40" r="3.2" fill="#FFFFFF" />
      <circle cx="46" cy="24" r="4" fill="#1FBAD6" />
    </svg>
  );
}
