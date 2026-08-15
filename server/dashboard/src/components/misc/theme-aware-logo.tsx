"use client";

export default function ThemeAwareLogo({
  width = 120,
  height = 40,
}: {
  width?: number;
  height?: number;
}) {
  return (
    <svg
      width={width}
      height={height}
      viewBox="0 0 360 80"
      role="img"
      aria-label="Agentar 记忆平台"
      className="text-onSurface-default-primary"
    >
      <text
        x="180"
        y="40"
        textAnchor="middle"
        dominantBaseline="middle"
        fill="currentColor"
        fontSize="44"
        fontWeight="600"
        letterSpacing="1"
      >
        Agentar 记忆平台
      </text>
    </svg>
  );
}
