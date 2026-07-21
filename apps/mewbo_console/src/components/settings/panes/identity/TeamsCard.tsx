/**
 * Teams — the groups people's work is shared through.
 *
 * Membership is NOT shown, and that is the API's shape, not an omission here:
 * a team's members arrive from provider group mappings at sign-in rather than
 * being stored as a roster, so there is no membership list to render. The
 * mapping card below is where "who ends up in this team" is actually answered.
 *
 * `slug` is immutable after creation because mapping rules target a team BY
 * slug — renaming one would silently detach every rule pointing at it. The edit
 * form therefore offers name and description only, which mirrors the PATCH body.
 */
import { useState } from "react";
import { Plus, Trash2 } from "lucide-react";

import { SettingsCard } from "../../SettingsCard";
import { labelCls } from "../../styles";
import { IamCardState, TruncationHint } from "./state";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ErrorAlert } from "@/components/ErrorAlert";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCreateTeam, useDeleteTeam, useIamTeams, type IamTeam } from "@/hooks/useIam";

const DESCRIPTION =
  "Groups of people who share access to the same work.\n\n" +
  "A team is how several people come to own the same sessions, projects, and apps instead of " +
  "each holding their own. Who ends up in a team is decided by the provider group mapping " +
  "below, so creating a team here is step one and pointing a provider group at its slug is " +
  "step two. The slug cannot be changed afterwards, because those mapping rules refer to it.";

/** Suggest a slug from a name, the way a person would type it. */
function slugify(name: string): string {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

function CreateTeamDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const create = useCreateTeam();
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [slugTouched, setSlugTouched] = useState(false);
  const [description, setDescription] = useState("");

  const effectiveSlug = slugTouched ? slug : slugify(name);

  const reset = () => {
    setName("");
    setSlug("");
    setSlugTouched(false);
    setDescription("");
    create.reset();
  };

  const submit = () => {
    create.mutate(
      { slug: effectiveSlug, name: name.trim(), description: description.trim() },
      {
        onSuccess: () => {
          reset();
          onClose();
        },
      },
    );
  };

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) {
          reset();
          onClose();
        }
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>New team</DialogTitle>
          <DialogDescription>
            Give the team a name and a slug. Point a provider group at the slug to fill it.
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-3">
          <div className="space-y-1">
            <label htmlFor="team-name" className={labelCls}>
              Name
            </label>
            <Input
              id="team-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Platform Engineering"
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="team-slug" className={labelCls}>
              Slug
            </label>
            <Input
              id="team-slug"
              value={effectiveSlug}
              onChange={(event) => {
                setSlugTouched(true);
                setSlug(event.target.value);
              }}
              placeholder="platform-engineering"
            />
            <p className="text-xs text-[hsl(var(--muted-foreground))]">
              Lowercase words joined by dashes. This cannot be changed later.
            </p>
          </div>
          <div className="space-y-1">
            <label htmlFor="team-description" className={labelCls}>
              Description
            </label>
            <Input
              id="team-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="Optional"
            />
          </div>
          {create.error && <ErrorAlert error={create.error} fallback="Failed to create the team" />}
        </div>

        <DialogFooter>
          <Button variant="ghost" size="md" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={submit}
            disabled={create.isPending || !name.trim() || !effectiveSlug}
          >
            {create.isPending ? "Creating…" : "Create team"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function TeamsCard() {
  const query = useIamTeams();
  const remove = useDeleteTeam();
  const [creating, setCreating] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<IamTeam | null>(null);

  return (
    <SettingsCard
      id="settings-identity-teams"
      title="Teams"
      description={DESCRIPTION}
      actions={
        query.writable ? (
          <Button
            variant="primary"
            size="sm"
            onClick={() => setCreating(true)}
            leadingIcon={<Plus className="h-4 w-4" />}
          >
            New team
          </Button>
        ) : undefined
      }
    >
      {remove.error && <ErrorAlert error={remove.error} fallback="Failed to delete the team" />}

      <IamCardState
        query={query}
        subject="teams"
        emptyLabel="No teams yet. Create one, then point a provider group at its slug."
        isEmpty={(page) => page.items.length === 0}
      >
        {(page) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Team</TableHead>
                <TableHead>Slug</TableHead>
                <TableHead className="text-right">Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((team) => (
                <TableRow key={team.id}>
                  <TableCell>
                    <span className="font-medium text-[hsl(var(--foreground))]">{team.name}</span>
                    {team.description && (
                      <span className="block text-xs text-[hsl(var(--muted-foreground))]">
                        {team.description}
                      </span>
                    )}
                  </TableCell>
                  <TableCell className="font-mono text-xs text-[hsl(var(--muted-foreground))]">
                    {team.slug}
                  </TableCell>
                  <TableCell className="text-right">
                    <Button
                      variant="neutral"
                      size="sm"
                      tone="danger"
                      aria-label={`Delete ${team.name}`}
                      onClick={() => setDeleteTarget(team)}
                      disabled={remove.isPending}
                      leadingIcon={<Trash2 className="h-3.5 w-3.5" />}
                    >
                      Delete
                    </Button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </IamCardState>

      <TruncationHint page={query.data} noun="teams" />

      <CreateTeamDialog open={creating} onClose={() => setCreating(false)} />

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this team?"
        description={
          <>
            <span className="font-medium text-[hsl(var(--foreground))]">
              {deleteTarget?.name ?? "This team"}
            </span>{" "}
            will be removed. Any provider group mapping pointing at the slug{" "}
            <span className="font-mono text-xs">{deleteTarget?.slug}</span> stops conferring
            membership, and work owned by the team stays where it is. This cannot be undone.
          </>
        }
        confirmLabel="Delete team"
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="h-4 w-4" />}
        pending={remove.isPending}
        onConfirm={() => {
          if (deleteTarget) {
            remove.mutate(deleteTarget.id, { onSuccess: () => setDeleteTarget(null) });
          }
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </SettingsCard>
  );
}
