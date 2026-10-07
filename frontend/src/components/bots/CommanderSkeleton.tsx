import React from "react";
import type { CommanderProfile } from "../../config/commanders.config";

export interface CommanderSkeletonProps extends React.HTMLAttributes<HTMLDivElement> {
  commander?: CommanderProfile;
}

export function CommanderSkeleton({ commander, className, ...rest }: CommanderSkeletonProps): React.ReactNode {
  const tint = commander?.theme.primary;
  return (
    <div
      role="status"
      aria-label="Loading commander scene"
      className={["relative aspect-square w-full animate-pulse rounded-2xl border bg-slate-900/60", className].filter(Boolean).join(" ")}
      style={{ borderColor: tint ? `${tint}40` : undefined }}
      {...rest}
    >
      <div
        className="absolute left-1/2 top-1/2 h-1/3 w-1/3 -translate-x-1/2 -translate-y-1/2 rounded-full"
        style={{ backgroundColor: tint ? `${tint}26` : undefined }}
      />
      <span className="sr-only">Loading…</span>
    </div>
  );
}

export default CommanderSkeleton;
