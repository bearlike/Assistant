/**
 * The Identity & Access pane — who exists, what they may do, and what happened.
 *
 * Five surfaces, each gated on the viewer's own permission from
 * `/api/auth/me`. **Gating here is UX, not security:** the API enforces every
 * one of these routes, and hiding a card the caller cannot read only keeps the
 * page honest. A hidden control is not a protected one.
 *
 * This pane is the ONE place that permission decision is expressed. The cards
 * take no permission prop, so there is no second copy to drift: a card renders
 * or it does not, and the card itself never re-asks. The whole-pane notice
 * below is deliberately not five per-card refusals — somebody with no identity
 * permissions wants one sentence, not five cards describing features they
 * cannot use.
 *
 * That does NOT make the cards' own `forbidden` branch dead. `can()` answers
 * optimistically when `/me` predates resolved permissions, so an out-of-date
 * API can render a card the server then refuses; the card explains it in place.
 *
 * The cards are ordered as an access review reads: who exists, how they are
 * grouped, what the groups grant, how those grants are decided, and what
 * actually happened. Each card owns its own query and its own non-data states
 * (see `identity/state.tsx`), so one unavailable surface never blanks the page.
 */
import { useAuthSession } from "@/hooks/useAuthSession";
import { SettingsCard } from "../SettingsCard";
import { AuditCard } from "./identity/AuditCard";
import { MappingsCard } from "./identity/MappingsCard";
import { Notice, Spinner } from "./identity/state";
import { RolesCard } from "./identity/RolesCard";
import { TeamsCard } from "./identity/TeamsCard";
import { UsersCard } from "./identity/UsersCard";

const KEYS_DESCRIPTION =
  "API keys are managed under Security & Access.\n\n" +
  "They sit beside git credentials and the secrets Mewbo already holds, because those answer the " +
  "same question: what can call Mewbo, and what can Mewbo reach. This page stays about who " +
  "people are and what their roles let them do.";

/**
 * A pointer, not a second keys surface.
 *
 * `ApiKeysView` is a Security-facet pane and nothing else — it used to be
 * dual-mounted as both a pane and a standalone route, and that duplication was
 * deliberately removed. Re-mounting it here to save an administrator one click
 * would walk it straight back.
 */
function KeysPointerCard() {
  return (
    <SettingsCard id="settings-identity-keys" title="API keys" description={KEYS_DESCRIPTION}>
      <a
        href="/settings?facet=security"
        className="text-sm font-medium text-[hsl(var(--primary-text))] underline underline-offset-2 hover:opacity-80"
      >
        Open Security &amp; Access
      </a>
    </SettingsCard>
  );
}

export function IdentityAccessPane() {
  const { can, loading } = useAuthSession();

  const mayAdminUsers = can("users.admin");
  const mayAdminTeams = can("teams.admin");
  const mayAdminRoles = can("roles.admin");
  const mayReadAudit = can("audit.read");
  const mayAdminKeys = can("keys.admin");
  const mayAdminAnything =
    mayAdminUsers || mayAdminTeams || mayAdminRoles || mayReadAudit || mayAdminKeys;

  if (loading) return <Spinner />;

  if (!mayAdminAnything) {
    return (
      <SettingsCard
        id="settings-identity-access"
        title="Identity and access"
        description="Managing people, teams, and roles requires an administrator role."
      >
        <Notice>
          Your role does not include any identity administration permissions. Ask an administrator
          if you need access to this page.
        </Notice>
      </SettingsCard>
    );
  }

  return (
    <div className="space-y-4">
      {mayAdminUsers && <UsersCard />}
      {mayAdminTeams && <TeamsCard />}
      {mayAdminRoles && <RolesCard />}
      {mayAdminRoles && <MappingsCard />}
      {mayReadAudit && <AuditCard />}
      {mayAdminKeys && <KeysPointerCard />}
    </div>
  );
}
