/**
 * People — everyone provisioned on this deployment, and the access they carry.
 *
 * Only two fields here are editable, and that is the API's rule rather than a
 * UI simplification: every other user field is provider-owned and is
 * overwritten on the next federated login or SCIM push, so offering to edit a
 * display name would be offering a change that silently reverts. Roles and
 * account status are the two things Mewbo itself owns.
 */
import { useState } from "react";
import { Search } from "lucide-react";

import { SettingsCard } from "../../SettingsCard";
import { IamCardState, Pill, PillRow, TruncationHint } from "./state";
import { UserAvatar } from "@/components/auth/UserAvatar";
import { ErrorAlert } from "@/components/ErrorAlert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useIamUsers, useUpdateUser, type IamUser } from "@/hooks/useIam";
import { avatarSourcesFrom, displayNameFrom, initialsFrom } from "@/hooks/useAuthSession";
import { formatDateTime } from "@/utils/time";

const DESCRIPTION =
  "Everyone who can sign in to this Mewbo, and the access each of them carries.\n\n" +
  "People appear here the first time they sign in through your identity provider, or when your " +
  "provider pushes them across with SCIM. Names and email addresses belong to the provider and " +
  "are refreshed on every sign-in, so the two things you change here are the roles somebody " +
  "holds and whether their account is still active. Disabling an account also revokes the keys " +
  "that account owns.";

/**
 * A row's name, derived by the same rules the rail uses for the signed-in
 * caller — `IamUser` carries the same triple under provider-shaped field names.
 */
function displayNameOf(user: IamUser): string {
  return displayNameFrom({ display: user.display_name, email: user.email, subject: user.id });
}

export function UsersCard() {
  const [search, setSearch] = useState("");
  const query = useIamUsers(search);
  const update = useUpdateUser();
  const [confirmTarget, setConfirmTarget] = useState<IamUser | null>(null);

  const toggleStatus = (user: IamUser) => {
    update.mutate({
      userId: user.id,
      patch: { status: user.status === "disabled" ? "active" : "disabled" },
    });
    setConfirmTarget(null);
  };

  return (
    <SettingsCard id="settings-identity-people" title="People" description={DESCRIPTION}>
      <div className="space-y-3">
        <div className="relative">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-[hsl(var(--muted-foreground))]" />
          <Input
            value={search}
            onChange={(event) => setSearch(event.target.value)}
            placeholder="Search by name or email"
            aria-label="Search people"
            className="pl-8"
          />
        </div>

        {update.error && (
          <ErrorAlert error={update.error} fallback="Failed to update the account" />
        )}

        <IamCardState
          query={query}
          subject="people"
          emptyLabel={
            search.trim()
              ? "Nobody matches that search."
              : "Nobody has signed in yet. People appear here after their first sign-in."
          }
          isEmpty={(page) => page.items.length === 0}
        >
          {(page) => (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Person</TableHead>
                  <TableHead>Roles</TableHead>
                  <TableHead>Provider</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Account</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {page.items.map((user) => {
                  const name = displayNameOf(user);
                  const disabled = user.status === "disabled";
                  return (
                    <TableRow key={user.id}>
                      <TableCell>
                        <div className="flex items-center gap-2.5">
                          <UserAvatar
                            sources={avatarSourcesFrom(user.avatar)}
                            initials={initialsFrom(name)}
                            name={name}
                          />
                          <div className="min-w-0">
                            <div className="flex items-center gap-1.5">
                              <span className="truncate font-medium text-[hsl(var(--foreground))]">
                                {name}
                              </span>
                              {disabled && <Pill tone="danger">Disabled</Pill>}
                            </div>
                            {user.email && (
                              <span className="block truncate text-xs text-[hsl(var(--muted-foreground))]">
                                {user.email}
                              </span>
                            )}
                          </div>
                        </div>
                      </TableCell>
                      <TableCell>
                        <PillRow values={user.roles} empty="None" />
                      </TableCell>
                      <TableCell>
                        <PillRow
                          values={user.external_identities.map((identity) => identity.issuer)}
                          empty="Local"
                        />
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-[hsl(var(--muted-foreground))]">
                        {formatDateTime(user.updated_at)}
                      </TableCell>
                      <TableCell className="text-right">
                        <Button
                          variant="neutral"
                          size="sm"
                          tone={disabled ? "default" : "danger"}
                          onClick={() => (disabled ? toggleStatus(user) : setConfirmTarget(user))}
                          disabled={update.isPending}
                        >
                          {disabled ? "Enable" : "Disable"}
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          )}
        </IamCardState>

        <TruncationHint
          page={query.data}
          noun="people"
          hint="Narrow the search to find someone specific."
        />
      </div>

      <ConfirmDialog
        open={confirmTarget !== null}
        title="Disable this account?"
        description={
          <>
            <span className="font-medium text-[hsl(var(--foreground))]">
              {confirmTarget ? displayNameOf(confirmTarget) : "This person"}
            </span>{" "}
            will be signed out and will not be able to sign in again. Every API key they own is
            revoked at the same time, so anything running as them stops working immediately. You
            can enable the account again later, but the revoked keys cannot be restored.
          </>
        }
        confirmLabel="Disable account"
        pendingLabel="Disabling…"
        pending={update.isPending}
        onConfirm={() => confirmTarget && toggleStatus(confirmTarget)}
        onCancel={() => setConfirmTarget(null)}
      />
    </SettingsCard>
  );
}
