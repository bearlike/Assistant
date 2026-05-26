/**
 * Brand icons for the wizard's platform tiles.
 *
 * Source of truth is the `simple-icons` package — official CC0 brand SVG
 * paths kept in lockstep with each vendor's actual identity. For platforms
 * simple-icons has dropped (Azure DevOps was removed when Microsoft tightened
 * trademark rules), we fall back to `@lobehub/icons` which already ships
 * Azure as a coloured React component.
 *
 * Render path: thin `<svg viewBox="0 0 24 24">` wrapper around the simple-
 * icons `path` data, drawn with `currentColor` so the parent (the colored
 * tile) controls the fill. One `Si` helper, no per-icon JSX.
 */

import Azure from "@lobehub/icons/es/Azure";
import {
  siBitbucket,
  siGit,
  siGitea,
  siGithub,
  siGitlab,
  type SimpleIcon,
} from "simple-icons";

import type { Platform } from "../api/types";

const SI_BY_ID: Partial<Record<Platform["id"], SimpleIcon>> = {
  github: siGithub,
  gitlab: siGitlab,
  bitbucket: siBitbucket,
  gitea: siGitea,
  git: siGit,
};

interface PlatformIconProps {
  platformId: Platform["id"];
  className?: string;
}

export function PlatformIcon({
  platformId,
  className = "h-4 w-4 text-white",
}: PlatformIconProps) {
  // Azure: not in simple-icons (Microsoft trademark policy). Use the
  // existing @lobehub/icons coloured variant — sized via `style.fontSize`
  // because lobehub icons accept a `size` prop rather than width/height.
  if (platformId === "azure") {
    return <Azure className={className} aria-hidden="true" />;
  }

  const icon = SI_BY_ID[platformId];
  if (!icon) return null;
  return (
    <svg
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 24 24"
      role="img"
      aria-label={icon.title}
      className={className}
    >
      <path d={icon.path} fill="currentColor" />
    </svg>
  );
}
