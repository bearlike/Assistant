import { useState } from "react";
import { useLocation } from "wouter";
import {
  Bell,
  BookOpen,
  ExternalLink,
  Github,
  LogOut,
  Moon,
  PanelLeft,
  Settings,
  Sun,
} from "lucide-react";

import { version as appVersion } from "../../../package.json";
import { cn } from "@/lib/utils";
import { BrandMark } from "@/components/BrandMark";
import { NotificationPanel } from "@/components/NotificationPanel";
import { useIsMobile } from "@/hooks/useIsMobile";
import { useAuthSession, type AuthSession } from "@/hooks/useAuthSession";
import { logout } from "@/api/auth";
import { UserAvatar } from "@/components/auth/UserAvatar";
import { Button } from "@/components/ui/button";
import { Sheet, SheetContent, SheetTitle } from "@/components/ui/sheet";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { TooltipProvider } from "@/components/ui/tooltip";
import type { NotificationItem } from "@/types";
import { ProductSection } from "./sections";
import { PRODUCTS, type ActiveProduct } from "./products";
import { FOCUS_RING, RAIL_GUTTER, RailActionRow, railActionRowCls } from "./rows";

const GITHUB_URL = "https://github.com/bearlike/Assistant";

type Navigate = (to: string) => void;

export interface NavRailProps {
  /** Which product is current (drives the switcher highlight + section). */
  activeProduct: ActiveProduct;
  /** Desktop collapse state (icon rail vs expanded). */
  collapsed: boolean;
  onToggleCollapsed: () => void;
  /** Mobile off-canvas Sheet state. */
  mobileOpen: boolean;
  onMobileOpenChange: (open: boolean) => void;
  theme: "dark" | "light";
  onToggleTheme?: () => void;
  notifications: NotificationItem[];
  onDismissNotification?: (id: string) => void;
  onClearNotifications?: () => void;
}

function VersionBadge() {
  // `text-2xs` is the sanctioned state-pill exception to the rail's single-size
  // law (a version chip is an identity pill, not reading text — see rows.tsx).
  // Text rides `--primary-text`, the darkened-on-tint token, NOT `--primary`:
  // `--primary` over this primary/15 fill measures ~2.36:1 in light, an AA fail.
  return (
    <span className="ml-1 rounded border border-[hsl(var(--primary))]/20 bg-[hsl(var(--primary))]/15 px-1.5 py-0.5 text-2xs font-normal leading-none text-[hsl(var(--primary-text))]">
      v{appVersion}
    </span>
  );
}

function RailHeader({
  collapsed,
  onToggleCollapsed,
  navigate,
}: {
  collapsed: boolean;
  onToggleCollapsed?: () => void;
  navigate: Navigate;
}) {
  const brand = (
    <button
      type="button"
      onClick={() => navigate("/")}
      aria-label="Mewbo home"
      className={cn(
        "flex items-center rounded-lg transition-opacity hover:opacity-80",
        collapsed ? "justify-center" : "gap-2 px-1",
        FOCUS_RING,
      )}
    >
      <BrandMark size={20} className="shrink-0 text-[hsl(var(--primary))]" />
      {!collapsed && (
        <>
          <span className="text-sm font-medium text-[hsl(var(--foreground))]">
            Mewbo
          </span>
          <VersionBadge />
        </>
      )}
    </button>
  );

  const toggle = onToggleCollapsed ? (
    <Button
      variant="ghost"
      size="sm"
      iconOnly
      onClick={onToggleCollapsed}
      aria-label={collapsed ? "Expand navigation" : "Collapse navigation"}
      title={collapsed ? "Expand navigation" : "Collapse navigation"}
      className="shrink-0"
    >
      {/* ONE neutral glyph for both directions. A pair of directional icons
          restates what the label already says, and the label is the half a
          screen reader actually announces — so the icon marks the control and
          `aria-label`/`title` carry the direction. */}
      <PanelLeft className="size-4" />
    </Button>
  ) : null;

  if (collapsed) {
    return (
      <div className="flex flex-col items-center gap-1.5 px-1 pb-2 pt-3">
        {brand}
        {toggle}
      </div>
    );
  }
  return (
    <div className="flex items-center justify-between gap-2 px-2 pb-2 pt-3">
      {brand}
      {toggle}
    </div>
  );
}

function ProductSwitcher({
  activeProduct,
  collapsed,
  navigate,
}: {
  activeProduct: ActiveProduct;
  collapsed: boolean;
  navigate: Navigate;
}) {
  return (
    <nav
      aria-label="Products"
      // `RAIL_GUTTER` (the shared 4px inset) floats the switcher rows' fills off
      // the rail edges — a rounded active/hover pill must never kiss the viewport
      // — and, with each row's own `RAIL_PAD_X`, lands the product icons on the
      // brand-mark column. No rule lives on this wrapper, so nothing is inset by
      // it. It is the same value collapsed and expanded, which also keeps the
      // centered glyphs off the rail's own right border in the 48px mode.
      className={cn("flex flex-col pt-1", RAIL_GUTTER)}
    >
      {PRODUCTS.map((product) => (
        <RailActionRow
          key={product.id}
          icon={product.icon}
          label={product.label}
          current={activeProduct === product.id}
          collapsed={collapsed}
          onClick={() => navigate(product.path)}
        />
      ))}
    </nav>
  );
}

/**
 * Unread count overlapping the bell's top-right corner. This is a numeric
 * STATE-CONTAINER pill: `rounded-full` is sanctioned by the console shape law,
 * and `text-2xs tabular-nums` is the ONE sanctioned exception to the rail's
 * single-size law (a 13px numeral balloons an overlay pill — see rows.tsx).
 * Hidden entirely at zero and capped at `9+` for width; the REAL count stays in
 * the bell's accessible name (below), so AT never loses it to the cap. Anchored
 * to the icon, not the button, so it rides the bell's corner in both the
 * compact square (expanded) and the full-width collapsed trigger WITHOUT
 * widening the flex item, which keeps the collapsed bell dead-centered.
 */
function UnreadBadge({ count }: { count: number }) {
  if (count <= 0) return null;
  return (
    <span className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-[hsl(var(--primary))] px-1 text-2xs font-medium leading-none tabular-nums text-[hsl(var(--primary-foreground))]">
      {count > 9 ? "9+" : count}
    </span>
  );
}

/**
 * The footer notification bell — an icon-only trigger that opens the shared
 * `NotificationPanel` popover. There is ONE notification surface; this never
 * forks a second one. Module-scope so its open-state and the Radix popover
 * survive a parent re-render (the NavBar-era lesson). Collapsed it fills the
 * 48px icon rail; expanded it is a compact square sitting beside the account
 * controls, so the feed costs an icon instead of a whole footer row.
 */
function NotificationBell({
  collapsed,
  notifications,
  onClose,
  onClearAll,
}: {
  collapsed: boolean;
  notifications: NotificationItem[];
  onClose?: (id: string) => void;
  onClearAll?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const unread = notifications.length;
  // The badge caps at 9+ for width; the accessible name carries the true count.
  const label = unread > 0 ? `Notifications, ${unread} unread` : "Notifications";

  const trigger = (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={cn(
        // Collapsed: the SAME box the product switcher uses (`railActionRowCls`
        // + centered), so the bell shares the 48px column's one centerline.
        // Expanded: a compact square matched to the Settings gear beside it.
        collapsed
          ? cn(railActionRowCls, "justify-center px-0")
          : cn(railActionRowCls, "size-7 shrink-0 justify-center"),
        "text-[hsl(var(--muted-foreground))] hover:bg-[hsl(var(--rail-selected))]/60 hover:text-[hsl(var(--foreground))]",
        open && "bg-[hsl(var(--rail-selected))] text-[hsl(var(--foreground))]",
        FOCUS_RING,
      )}
    >
      <span className="relative flex">
        <Bell className="size-4" />
        <UnreadBadge count={unread} />
      </span>
    </button>
  );

  return (
    <NotificationPanel
      notifications={notifications}
      onClose={(id) => onClose?.(id)}
      onClearAll={() => onClearAll?.()}
      open={open}
      onOpenChange={setOpen}
      side="right"
      align="end"
      trigger={trigger}
    />
  );
}

/**
 * The identity block at the top of the account menu.
 *
 * Not a menu ROW and not a destination — it is a read-only header, which is
 * why it does not count against the four-row law below. It states who the
 * console currently acts as, and when auth is off it says exactly that instead
 * of inventing a user: a deployment with no sign-in has no signed-in person,
 * and dressing the legacy principal up as one would be a lie the operator
 * cannot correct.
 *
 * Three type steps and no more: `text-sm` for the name, `text-xs` for the
 * supporting line, `text-2xs` for the role chips. Hierarchy here comes from
 * weight and colour, never from a further size step — a fourth one would only
 * shave a pixel off the smallest text, which reads as noise rather than rank.
 */
function AccountIdentity({ session }: { session: AuthSession | null }) {
  if (!session) return null;

  if (session.isLegacyPrincipal) {
    return (
      <div className="px-2 py-1.5">
        <p className="text-sm font-medium text-[hsl(var(--foreground))]">Local access</p>
        <p className="mt-0.5 text-xs font-normal text-[hsl(var(--muted-foreground))]">
          Sign-in is off for this deployment, so every request runs with full access.
        </p>
      </div>
    );
  }

  return (
    <div className="flex items-start gap-2.5 px-2 py-1.5">
      <UserAvatar
        sources={session.avatarSources}
        initials={session.initials}
        name={session.displayName}
        className="mt-0.5 size-7"
      />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-[hsl(var(--foreground))]">
          {session.displayName}
        </p>
        {session.email && (
          <p className="truncate text-xs font-normal text-[hsl(var(--muted-foreground))]">
            {session.email}
          </p>
        )}
        {session.roles.length > 0 && (
          <div className="mt-1.5 flex flex-wrap gap-1">
            {session.roles.map((role) => (
              <span
                key={role}
                className="rounded-full border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 px-1.5 py-0.5 text-2xs font-normal leading-none text-[hsl(var(--muted-foreground))]"
              >
                {role}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function AvatarMenuItems({
  theme,
  onToggleTheme,
  navigate,
  session,
}: {
  theme: "dark" | "light";
  onToggleTheme?: () => void;
  navigate: Navigate;
  session: AuthSession | null;
}) {
  // EXACTLY four rows — Settings, Documentation, GitHub, theme toggle. This menu
  // is deliberately NOT a page index: the product switcher owns primary nav.
  // The identity header above and the sign-out action below are neither
  // destinations nor navigation, so the law they enforce still holds.
  const showSignOut = Boolean(session && !session.isLegacyPrincipal);

  return (
    <>
      <AccountIdentity session={session} />
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={() => navigate("/settings")}>
        <Settings className="mr-2 size-4" />
        Settings
      </DropdownMenuItem>
      <DropdownMenuItem asChild>
        <a href="https://docs.mewbo.com" target="_blank" rel="noopener noreferrer">
          <BookOpen className="mr-2 size-4" />
          Documentation
          <ExternalLink className="ml-auto size-4 text-[hsl(var(--muted-foreground))]" />
        </a>
      </DropdownMenuItem>
      <DropdownMenuItem asChild>
        <a href={GITHUB_URL} target="_blank" rel="noopener noreferrer">
          <Github className="mr-2 size-4" />
          View on GitHub
          <ExternalLink className="ml-auto size-4 text-[hsl(var(--muted-foreground))]" />
        </a>
      </DropdownMenuItem>
      <DropdownMenuSeparator />
      <DropdownMenuItem onSelect={() => onToggleTheme?.()}>
        {theme === "dark" ? (
          <Sun className="mr-2 size-4" />
        ) : (
          <Moon className="mr-2 size-4" />
        )}
        {theme === "dark" ? "Switch to Light mode" : "Switch to Dark mode"}
      </DropdownMenuItem>
      {showSignOut && (
        <>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            onSelect={() => {
              // Clear the cookie, then hard-navigate: a full reload drops every
              // cached query belonging to the signed-out principal, which a
              // client-side route change would leave sitting in memory.
              void logout().finally(() => window.location.assign("/"));
            }}
          >
            <LogOut className="mr-2 size-4" />
            Sign out
          </DropdownMenuItem>
        </>
      )}
    </>
  );
}

/**
 * The rail's avatar. Falls back to the brand glyph until `/me` resolves, so the
 * footer never flashes a placeholder identity that belongs to nobody.
 */
function RailAvatar({
  session,
  className,
}: {
  session: AuthSession | null;
  /** Size override. Collapsed passes `size-4` so the avatar shrinks to the
   *  bell's glyph box and the icon column reads as one centerline; expanded
   *  keeps the default `size-6` beside the name. */
  className?: string;
}) {
  if (!session) {
    return (
      <span
        className={cn(
          "flex size-6 shrink-0 items-center justify-center rounded-full bg-[hsl(var(--muted))]",
          className,
        )}
      >
        <BrandMark size={12} className="text-[hsl(var(--muted-foreground))]" />
      </span>
    );
  }
  return (
    <UserAvatar
      sources={session.avatarSources}
      initials={session.initials}
      name={session.displayName}
      className={cn("shrink-0", className)}
    />
  );
}

/**
 * The pinned footer — ONE zone holding the account menu, the notification bell
 * and (expanded) the Settings gear. Merging the old notifications row into this
 * cluster reclaims a full row of vertical space: the feed is now an icon next
 * to Settings rather than its own full-width row.
 */
function RailFooter({
  collapsed,
  theme,
  onToggleTheme,
  navigate,
  notifications,
  onDismissNotification,
  onClearNotifications,
}: {
  collapsed: boolean;
  theme: "dark" | "light";
  onToggleTheme?: () => void;
  navigate: Navigate;
  notifications: NotificationItem[];
  onDismissNotification?: (id: string) => void;
  onClearNotifications?: () => void;
}) {
  // Self-contained, per the rail's section contract: the row calls the hook
  // itself rather than taking identity as a prop, and the TanStack cache
  // dedupes it against every other `/me` reader.
  const { session } = useAuthSession();
  const label = session && !session.isLegacyPrincipal ? session.displayName : "Account";

  const bell = (
    <NotificationBell
      collapsed={collapsed}
      notifications={notifications}
      onClose={onDismissNotification}
      onClearAll={onClearNotifications}
    />
  );

  if (collapsed) {
    // 48px icon rail: the bell keeps its own icon above the avatar; there is no
    // standalone gear here, Settings rides the avatar menu. Both footer icons
    // sit in the SAME box the product switcher uses (`railActionRowCls` +
    // centered) so brand, product glyphs, bell and avatar share one centerline.
    return (
      <div className="flex flex-col items-center gap-1.5">
        {bell}
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <button
              type="button"
              aria-label="Account menu"
              title="Account"
              className={cn(
                railActionRowCls,
                "justify-center px-0 hover:bg-[hsl(var(--rail-selected))]/60",
                FOCUS_RING,
              )}
            >
              <RailAvatar session={session} className="size-4" />
            </button>
          </DropdownMenuTrigger>
          <DropdownMenuContent side="right" align="end" className="w-56">
            <AvatarMenuItems
              theme={theme}
              onToggleTheme={onToggleTheme}
              navigate={navigate}
              session={session}
            />
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    );
  }
  return (
    // The account bleeds to the left rail edge like every row above it (fill
    // full-bleed, content on `RAIL_PAD_X`); `pr-2` sets the trailing bell + gear
    // the same 8px off the right edge that the header's collapse toggle keeps.
    <div className="flex items-center gap-1.5 pr-2">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            aria-label="Account menu"
            className={cn(
              // Compose the kit's row geometry (its w-full → w-auto so the
              // button grows via flex-1 instead of forcing full width).
              railActionRowCls,
              "w-auto flex-1 px-2 text-left hover:bg-[hsl(var(--rail-selected))]/60",
              FOCUS_RING,
            )}
          >
            <RailAvatar session={session} />
            {/* Foreground colour, not weight, is what separates the signed-in
                name from the muted rows above it — the footer keeps one even
                rhythm. */}
            <span className="flex-1 truncate text-sm font-normal text-[hsl(var(--foreground))]">
              {label}
            </span>
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent side="right" align="end" className="w-56">
          <AvatarMenuItems
            theme={theme}
            onToggleTheme={onToggleTheme}
            navigate={navigate}
            session={session}
          />
        </DropdownMenuContent>
      </DropdownMenu>
      {/* Bell sits directly beside the Settings gear — the two icon controls
          read as a matched pair at the row's trailing edge. */}
      {bell}
      <Button
        variant="ghost"
        size="sm"
        iconOnly
        onClick={() => navigate("/settings")}
        aria-label="Settings"
        title="Settings"
        className="shrink-0"
      >
        <Settings className="size-4" />
      </Button>
    </div>
  );
}

function RailContent({
  collapsed,
  onToggleCollapsed,
  activeProduct,
  theme,
  onToggleTheme,
  notifications,
  onDismissNotification,
  onClearNotifications,
}: Omit<NavRailProps, "mobileOpen" | "onMobileOpenChange" | "onToggleCollapsed"> & {
  onToggleCollapsed?: () => void;
}) {
  const [, navigate] = useLocation();

  const body = (
    <div className="flex h-full flex-col">
      {/* Header + switcher + section scroll as one region; the footer stays
          pinned (per the Aura drawer convention). */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <RailHeader
          collapsed={collapsed}
          onToggleCollapsed={onToggleCollapsed}
          navigate={navigate}
        />
        <ProductSwitcher
          activeProduct={activeProduct}
          collapsed={collapsed}
          navigate={navigate}
        />
        {!collapsed && (
          // NO horizontal padding here: it would sit OUTSIDE the kit's
          // `RailSectionHeader` rules (the "New task"/"All wikis" dividers) and
          // inset them from the rail edges. The section's own rows carry
          // `RAIL_PAD_X`, so every rule inside spans the full width.
          <div className="mt-2 border-t border-[hsl(var(--rail-border))] pt-1">
            <ProductSection product={activeProduct} />
          </div>
        )}
      </div>
      {/* Footer = ONE bordered zone. Notifications collapsed from a full-width
          row into a bell beside the account controls, so the account cluster
          and the feed now share one enclosure with a single rule above it. The
          rule rides `--rail-border`, not `--border`: the latter carries an alpha
          tuned for hairlines INSIDE a card, which on the rail canvas reads as no
          line at all. `RAIL_GUTTER` (the shared 4px inset) sits on THIS div: the
          `border-t` is outside padding so the rule still spans edge to edge,
          while the account/bell/gear cluster inside floats off the edges and its
          avatar lands on the brand-mark column — the rules-vs-fills split. Same
          value collapsed and expanded, matching the switcher. */}
      <div
        className={cn(
          "border-t border-[hsl(var(--rail-border))] py-2",
          RAIL_GUTTER,
        )}
      >
        <RailFooter
          collapsed={collapsed}
          theme={theme}
          onToggleTheme={onToggleTheme}
          navigate={navigate}
          notifications={notifications}
          onDismissNotification={onDismissNotification}
          onClearNotifications={onClearNotifications}
        />
      </div>
    </div>
  );

  // The collapsed rail's product/action rows show a right-side Tooltip.
  return collapsed ? (
    <TooltipProvider delayDuration={200}>{body}</TooltipProvider>
  ) : (
    body
  );
}

/**
 * The unified left navigation rail. Owns ALL site navigation: brand → Tasks
 * landing, the four-product switcher, a product-scoped recents section, and a
 * pinned footer (notifications + account). Desktop renders an inline aside
 * (expanded 272px / collapsed 48px icon rail); mobile renders the same content
 * in an off-canvas Sheet. The responsive swap is the `useIsMobile` JS hook, not
 * a parallel Tailwind breakpoint.
 */
export function NavRail({
  activeProduct,
  collapsed,
  onToggleCollapsed,
  mobileOpen,
  onMobileOpenChange,
  theme,
  onToggleTheme,
  notifications,
  onDismissNotification,
  onClearNotifications,
}: NavRailProps) {
  const isMobile = useIsMobile();

  if (isMobile) {
    return (
      <Sheet open={mobileOpen} onOpenChange={onMobileOpenChange}>
        <SheetContent
          side="left"
          className="w-[300px] border-r border-[hsl(var(--border-strong))] bg-[hsl(var(--rail-bg))] p-0"
        >
          <SheetTitle className="sr-only">Navigation</SheetTitle>
          {/* Mobile drawer is never collapsed and has no collapse toggle. */}
          <RailContent
            collapsed={false}
            activeProduct={activeProduct}
            theme={theme}
            onToggleTheme={onToggleTheme}
            notifications={notifications}
            onDismissNotification={onDismissNotification}
            onClearNotifications={onClearNotifications}
          />
        </SheetContent>
      </Sheet>
    );
  }

  return (
    <aside
      aria-label="Primary navigation"
      className={cn(
        "h-full shrink-0 bg-[hsl(var(--rail-bg))] transition-[width] duration-200",
        collapsed ? "w-12" : "w-[272px]",
      )}
    >
      <RailContent
        collapsed={collapsed}
        onToggleCollapsed={onToggleCollapsed}
        activeProduct={activeProduct}
        theme={theme}
        onToggleTheme={onToggleTheme}
        notifications={notifications}
        onDismissNotification={onDismissNotification}
        onClearNotifications={onClearNotifications}
      />
    </aside>
  );
}
