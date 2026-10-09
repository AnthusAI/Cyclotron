import * as React from "react";

export interface RefreshCwWideProps extends React.SVGProps<SVGSVGElement> {
  /** Height in pixels (width scales to 1.75x) or custom size */
  size?: number | string;
  strokeWidth?: number | string;
  color?: string;
  className?: string;
}

/**
 * 1.75:1 aspect ratio refresh icon designed in the Lucide iconographic style.
 * ViewBox: 0 0 42 24, 2px stroke, round caps/joins. It was 3:1 while the
 * tagline under it was long; the shorter tagline wants a shorter loop.
 */
export const RefreshCwWide = React.forwardRef<SVGSVGElement, RefreshCwWideProps>(
  (
    {
      size,
      width,
      height,
      strokeWidth = 2,
      color = "currentColor",
      className,
      ...props
    },
    ref
  ) => {
    const computedHeight = height ?? (size ?? 24);
    const computedWidth =
      width ??
      (typeof computedHeight === "number"
        ? computedHeight * 1.75
        : `calc(${computedHeight} * 1.75)`);

    return (
      <svg
        ref={ref}
        xmlns="http://www.w3.org/2000/svg"
        width={computedWidth}
        height={computedHeight}
        viewBox="0 0 42 24"
        fill="none"
        stroke={color}
        strokeWidth={strokeWidth}
        strokeLinecap="round"
        strokeLinejoin="round"
        className={className}
        {...props}
      >
        <path d="M3 12a9 9 0 0 1 9-9h18a9.75 9.75 0 0 1 6.74 2.74L39 8" />
        <path d="M39 3v5h-5" />
        <path d="M39 12a9 9 0 0 1-9 9H12a9.75 9.75 0 0 1-6.74-2.74L3 16" />
        <path d="M8 16H3v5" />
      </svg>
    );
  }
);

RefreshCwWide.displayName = "RefreshCwWide";

export default RefreshCwWide;
