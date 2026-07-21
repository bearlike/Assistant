/**
 * The audit trail — sign-ins, key lifecycle, role changes, and refusals.
 *
 * The server types each entry as a discriminated union on `type`, every variant
 * carrying only its own fields. `summarize` is the ONE place that knows those
 * variants, so the table stays a plain read and a new event kind degrades to a
 * readable fallback rather than a blank cell or a crash. Deliberately not
 * mirrored as nine TypeScript interfaces: this surface renders one summary
 * line, and nine interfaces would be nine more things to keep in lockstep for
 * no rendering benefit. That trade flips the day a per-kind detail view exists.
 */
import { SettingsCard } from "../../SettingsCard";
import { IamCardState, Pill, TruncationHint } from "./state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useIamAudit } from "@/hooks/useIam";
import type { IamAuditEvent } from "@/api/iam";
import { formatDateTime } from "@/utils/time";

const DESCRIPTION =
  "What has happened to access on this deployment, newest first.\n\n" +
  "Each row is one event the identity layer recorded: somebody signed in or failed to, a key was " +
  "minted or revoked, roles or team membership changed, or a request was refused. Use it to " +
  "answer who changed something, and why somebody could not do what they expected.";

/** Which events read as a problem worth noticing at a glance. */
const NEGATIVE_TYPES = new Set(["login_failure", "access_denied", "scim_deprovisioned"]);

/** Read a variant-specific field without assuming it exists. */
function str(event: IamAuditEvent, field: string): string | null {
  const value = event[field];
  return typeof value === "string" && value ? value : null;
}

function list(event: IamAuditEvent, field: string): string[] {
  const value = event[field];
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

/** A human sentence for one event. Unknown kinds fall back to their type name. */
function summarize(event: IamAuditEvent): string {
  switch (event.type) {
    case "login_success": {
      const issuer = str(event, "issuer");
      const method = str(event, "method") ?? "an authenticator";
      return issuer ? `Signed in with ${method} via ${issuer}` : `Signed in with ${method}`;
    }
    case "login_failure": {
      const who = str(event, "attempted_identifier");
      const reason = str(event, "reason") ?? "rejected";
      return who ? `Sign-in rejected for ${who}: ${reason}` : `Sign-in rejected: ${reason}`;
    }
    case "key_minted": {
      const label = str(event, "label");
      return label ? `Created API key "${label}"` : "Created an API key";
    }
    case "key_revoked":
      return `Revoked API key ${str(event, "key_id") ?? ""}`.trim();
    case "role_changed": {
      const added = list(event, "added");
      const removed = list(event, "removed");
      const parts: string[] = [];
      if (added.length) parts.push(`granted ${added.join(", ")}`);
      if (removed.length) parts.push(`removed ${removed.join(", ")}`);
      return parts.length ? `Roles ${parts.join("; ")}` : "Roles changed";
    }
    case "team_changed":
      return `Team ${str(event, "team_id") ?? ""} membership ${str(event, "change") ?? "changed"}`;
    case "access_denied": {
      const need = str(event, "need") ?? str(event, "permission") ?? "a permission";
      const kind = str(event, "resource_kind");
      return kind ? `Refused: needs ${need} on ${kind}` : `Refused: needs ${need}`;
    }
    case "scim_provisioned":
      return `Provisioned through SCIM from ${str(event, "issuer") ?? "the provider"}`;
    case "scim_deprovisioned":
      return "Deprovisioned through SCIM";
    default:
      return event.type.replace(/_/g, " ");
  }
}

export function AuditCard() {
  const query = useIamAudit();

  return (
    <SettingsCard id="settings-identity-audit" title="Audit log" description={DESCRIPTION}>
      <IamCardState
        query={query}
        subject="the audit log"
        emptyLabel="Nothing has been recorded yet."
        isEmpty={(page) => page.items.length === 0}
      >
        {(page) => (
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>When</TableHead>
                <TableHead>Who</TableHead>
                <TableHead>What happened</TableHead>
                <TableHead>Where</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page.items.map((event, index) => (
                // Events carry no id on the wire; timestamp plus position is
                // stable for a list that only ever grows at the head.
                <TableRow key={`${event.ts}-${index}`}>
                  <TableCell className="whitespace-nowrap text-xs text-[hsl(var(--muted-foreground))]">
                    {formatDateTime(event.ts)}
                  </TableCell>
                  <TableCell className="font-mono text-xs">
                    {event.actor_subject ?? "—"}
                  </TableCell>
                  <TableCell>
                    <span className="text-sm">{summarize(event)}</span>
                  </TableCell>
                  <TableCell>
                    <Pill tone={NEGATIVE_TYPES.has(event.type) ? "danger" : "muted"}>
                      {event.source}
                    </Pill>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        )}
      </IamCardState>

      <TruncationHint page={query.data} noun="events" hint="These are the most recent." />
    </SettingsCard>
  );
}
