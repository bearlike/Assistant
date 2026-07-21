import { createContext, useContext } from "react";

/**
 * Lets in-pane surfaces (the SessionHeader hamburger, the landings' floating
 * hamburger) open the mobile navigation Sheet without prop-drilling through the
 * route tree. `AppLayout` owns the Sheet state and provides this; every consumer
 * only ever needs `openMobileRail`.
 */
export interface RailControls {
  /** Opens the mobile navigation Sheet. A no-op on desktop. */
  openMobileRail: () => void;
}

const RailControlsContext = createContext<RailControls>({
  // No-op default — the real opener is supplied by AppLayout's provider.
  openMobileRail: () => undefined,
});

export const RailControlsProvider = RailControlsContext.Provider;

export function useRailControls(): RailControls {
  return useContext(RailControlsContext);
}
