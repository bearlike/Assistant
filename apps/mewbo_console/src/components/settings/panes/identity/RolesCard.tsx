/**
 * Roles — the named permission bundles, and the editor for custom ones.
 *
 * The editor's checkbox list is built ENTIRELY from `GET /api/iam/permissions`,
 * grouped by the domains the server sends. The console keeps no copy of the
 * catalog: `mewbo_iam`'s `PermissionCatalog` is the source of truth, and a
 * hardcoded duplicate would go stale the first time a permission is added, then
 * quietly offer a role that grants nothing. This is the same trap the settings
 * surface hit once before with a hardcoded list, and the fix is the same one —
 * render whatever the server says exists.
 *
 * Built-in roles are read-only, which the API enforces by refusing their names
 * on write. They still render, because seeing what `operator` actually grants
 * is most of the reason to open this page.
 */
import { useEffect, useState } from "react";
import { Pencil, Plus, Trash2 } from "lucide-react";

import { SettingsCard } from "../../SettingsCard";
import { labelCls } from "../../styles";
import { IamCardState, Pill, PillRow, Spinner, TruncationHint } from "./state";
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
  useCreateRole,
  useDeleteRole,
  useIamPermissionCatalog,
  useIamRoles,
  useUpdateRole,
  type IamRole,
} from "@/hooks/useIam";

const DESCRIPTION =
  "The named bundles of permissions you hand out.\n\n" +
  "Mewbo ships five built-in roles that cannot be edited: admin holds everything, operator holds " +
  "everything except identity governance, member covers ordinary day-to-day work, viewer is " +
  "read-only, and service starts empty for machine accounts to build on. When none of those fit, " +
  "create a custom role and pick exactly the permissions it should grant. Roles reach people " +
  "through the provider group mapping below, or by assigning them directly on the People card.";

/** Human label for a permission id — the id itself, minus its domain prefix. */
function permissionLabel(id: string): string {
  const dot = id.indexOf(".");
  return dot === -1 ? id : id.slice(dot + 1);
}

interface EditorProps {
  /** The role being edited, or null when creating a new one. */
  role: IamRole | null;
  open: boolean;
  onClose: () => void;
}

function RoleEditorDialog({ role, open, onClose }: EditorProps) {
  const catalog = useIamPermissionCatalog(open);
  const create = useCreateRole();
  const update = useUpdateRole();
  const isEdit = role !== null;

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [selected, setSelected] = useState<Set<string>>(new Set());

  // Seed from the role each time the dialog opens for a different target;
  // keying on `open` too means reopening for a NEW role clears the last edit.
  useEffect(() => {
    if (!open) return;
    setName(role?.name ?? "");
    setDescription(role?.description ?? "");
    setSelected(new Set(role?.permissions ?? []));
    create.reset();
    update.reset();
    // `create`/`update` are stable mutation handles; re-seeding on their
    // identity would wipe the form mid-edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, role]);

  const toggle = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleDomain = (ids: string[], allOn: boolean) => {
    setSelected((prev) => {
      const next = new Set(prev);
      for (const id of ids) {
        if (allOn) next.delete(id);
        else next.add(id);
      }
      return next;
    });
  };

  const submit = () => {
    const permissions = [...selected].sort();
    const done = { onSuccess: onClose };
    if (isEdit) {
      update.mutate({ name: role.name, patch: { description, permissions } }, done);
    } else {
      create.mutate({ name: name.trim(), description, permissions }, done);
    }
  };

  const pending = create.isPending || update.isPending;
  const error = create.error ?? update.error;

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent className="flex max-h-[85vh] max-w-2xl flex-col">
        <DialogHeader>
          <DialogTitle>{isEdit ? `Edit ${role.name}` : "New role"}</DialogTitle>
          <DialogDescription>
            Pick the permissions this role grants. Anyone holding it can do exactly these things
            and nothing else.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
          {!isEdit && (
            <div className="space-y-1">
              <label htmlFor="role-name" className={labelCls}>
                Name
              </label>
              <Input
                id="role-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder="release-manager"
              />
              <p className="text-xs text-[hsl(var(--muted-foreground))]">
                Lowercase letters, digits, dashes or underscores. The name is permanent.
              </p>
            </div>
          )}
          <div className="space-y-1">
            <label htmlFor="role-description" className={labelCls}>
              Description
            </label>
            <Input
              id="role-description"
              value={description}
              onChange={(event) => setDescription(event.target.value)}
              placeholder="What this role is for"
            />
          </div>

          <div className="space-y-2">
            <p className="text-xs font-medium">Permissions ({selected.size} selected)</p>
            {catalog.loading && <Spinner />}
            {catalog.error && (
              <ErrorAlert error={catalog.error} fallback="Failed to load the permission catalog" />
            )}
            {catalog.data?.domains.map((domain) => {
              const allOn = domain.permissions.every((id) => selected.has(id));
              return (
                <div
                  key={domain.domain}
                  className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] p-3"
                >
                  <div className="mb-2 flex items-center justify-between">
                    <span className="text-xs font-medium text-[hsl(var(--foreground))]">
                      {domain.domain}
                    </span>
                    <button
                      type="button"
                      onClick={() => toggleDomain(domain.permissions, allOn)}
                      className="text-xs text-[hsl(var(--primary-text))] hover:underline"
                    >
                      {allOn ? "Clear all" : "Select all"}
                    </button>
                  </div>
                  <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
                    {domain.permissions.map((id) => (
                      <label
                        key={id}
                        className="flex cursor-pointer items-center gap-2 text-xs text-[hsl(var(--foreground))]"
                      >
                        <input
                          type="checkbox"
                          checked={selected.has(id)}
                          onChange={() => toggle(id)}
                          className="size-3.5 accent-[hsl(var(--primary))]"
                        />
                        <span className="font-mono">{permissionLabel(id)}</span>
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>

          {error && <ErrorAlert error={error} fallback="Failed to save the role" />}
        </div>

        <DialogFooter>
          <Button variant="ghost" size="md" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={submit}
            disabled={pending || (!isEdit && !name.trim())}
          >
            {pending ? "Saving…" : isEdit ? "Save role" : "Create role"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function RolesCard() {
  const query = useIamRoles();
  const remove = useDeleteRole();
  const [editing, setEditing] = useState<IamRole | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<IamRole | null>(null);

  const openEditor = (role: IamRole | null) => {
    setEditing(role);
    setEditorOpen(true);
  };

  return (
    <SettingsCard
      id="settings-identity-roles"
      title="Roles"
      description={DESCRIPTION}
      actions={
        query.writable ? (
          <Button
            variant="primary"
            size="sm"
            onClick={() => openEditor(null)}
            leadingIcon={<Plus className="h-4 w-4" />}
          >
            New role
          </Button>
        ) : undefined
      }
    >
      {remove.error && <ErrorAlert error={remove.error} fallback="Failed to delete the role" />}

      <IamCardState
        query={query}
        subject="roles"
        emptyLabel="No roles are defined."
        isEmpty={(page) => page.items.length === 0}
      >
        {(page) => (
          <div className="space-y-2">
            {page.items.map((role) => (
              <div
                key={role.name}
                className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--background))] px-4 py-3"
              >
                <div className="flex items-start justify-between gap-4">
                  <div className="min-w-0">
                    <div className="flex items-center gap-2">
                      <span className="text-sm font-medium text-[hsl(var(--foreground))]">
                        {role.name}
                      </span>
                      {role.builtin ? <Pill>Built in</Pill> : <Pill tone="accent">Custom</Pill>}
                    </div>
                    {role.description && (
                      <p className="mt-0.5 text-xs text-[hsl(var(--muted-foreground))]">
                        {role.description}
                      </p>
                    )}
                  </div>
                  {!role.builtin && (
                    <div className="flex shrink-0 gap-1.5">
                      <Button
                        variant="neutral"
                        size="sm"
                        onClick={() => openEditor(role)}
                        aria-label={`Edit ${role.name}`}
                        leadingIcon={<Pencil className="h-3.5 w-3.5" />}
                      >
                        Edit
                      </Button>
                      <Button
                        variant="neutral"
                        size="sm"
                        tone="danger"
                        onClick={() => setDeleteTarget(role)}
                        aria-label={`Delete ${role.name}`}
                        iconOnly
                      >
                        <Trash2 className="h-3.5 w-3.5" />
                      </Button>
                    </div>
                  )}
                </div>
                <div className="mt-2">
                  {role.permissions.length > 12 ? (
                    <p className="text-xs text-[hsl(var(--muted-foreground))]">
                      Grants {role.permissions.length} permissions across the whole product.
                    </p>
                  ) : (
                    <PillRow values={role.permissions} empty="Grants nothing yet" />
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </IamCardState>

      <TruncationHint page={query.data} noun="roles" />

      {query.writable && (
        <RoleEditorDialog
          role={editing}
          open={editorOpen}
          onClose={() => setEditorOpen(false)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete this role?"
        description={
          <>
            Anyone currently holding{" "}
            <span className="font-medium text-[hsl(var(--foreground))]">
              {deleteTarget?.name ?? "this role"}
            </span>{" "}
            loses every permission it granted, on their next request. If a provider group mapping
            points at it, that mapping stops conferring anything. This cannot be undone.
          </>
        }
        confirmLabel="Delete role"
        pendingLabel="Deleting…"
        confirmIcon={<Trash2 className="h-4 w-4" />}
        pending={remove.isPending}
        onConfirm={() => {
          if (deleteTarget) {
            remove.mutate(deleteTarget.name, { onSuccess: () => setDeleteTarget(null) });
          }
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </SettingsCard>
  );
}
