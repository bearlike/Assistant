/**
 * Provider group mapping — the answer to "why does this person have that role?".
 *
 * Read-only on purpose: these rules come from `api.auth` configuration, not
 * from a store, so the place to change them is the config file (Settings has no
 * schema section for them because they are auth-kernel settings, not product
 * ones). Showing them here anyway is the point of the card — an operator
 * debugging someone's access needs to see the rules and their ORDER, and
 * evaluation order is exactly what a config file read out of context loses.
 */
import { cn } from "@/lib/utils";
import { SettingsCard } from "../../SettingsCard";
import { subsectionTitleCls } from "../../styles";
import { IamCardState, Pill, PillRow } from "./state";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useIamMappings } from "@/hooks/useIam";
import type { MappingRule } from "@/api/iam";

const DESCRIPTION =
  "How groups from your identity provider turn into Mewbo roles and team membership.\n\n" +
  "When somebody signs in, Mewbo reads the groups their provider sent and applies the rules " +
  "below in order. This is the normal way access is granted: change someone's group at the " +
  "provider and their Mewbo access follows on their next sign-in. These rules live in the " +
  "server's auth configuration, so they are shown here but changed there.";

function RuleTable({ rules, targetLabel }: { rules: MappingRule[]; targetLabel: string }) {
  if (rules.length === 0) {
    return (
      <p className="text-xs text-[hsl(var(--muted-foreground))]">
        No rules configured, so no group confers {targetLabel.toLowerCase()}.
      </p>
    );
  }
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Provider group</TableHead>
          <TableHead>Match</TableHead>
          <TableHead>{targetLabel}</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rules.map((rule, index) => (
          // Rules are ORDERED and a match value may legitimately repeat across
          // rows, so position is the only stable identity here.
          <TableRow key={`${rule.match}-${rule.target}-${index}`}>
            <TableCell className="font-mono text-xs">{rule.match}</TableCell>
            <TableCell>
              <Pill>{rule.match_kind}</Pill>
            </TableCell>
            <TableCell className="font-mono text-xs">{rule.target}</TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

export function MappingsCard() {
  const query = useIamMappings();

  return (
    <SettingsCard
      id="settings-identity-mappings"
      title="Provider group mapping"
      description={DESCRIPTION}
    >
      <IamCardState query={query} subject="group mappings" emptyLabel="No mapping is configured.">
        {(mappings) => (
          <div className="space-y-5">
            <div>
              <h3 className={cn("mb-1.5", subsectionTitleCls)}>
                Default role
              </h3>
              <p className="mb-2 text-xs text-[hsl(var(--muted-foreground))]">
                Everyone who signs in gets this role when no rule below matches their groups.
              </p>
              <PillRow values={[mappings.default_role]} empty="None" tone="accent" />
            </div>

            <div>
              <h3 className={cn("mb-2", subsectionTitleCls)}>
                Group to role
              </h3>
              <RuleTable rules={mappings.role_rules} targetLabel="Grants role" />
            </div>

            <div>
              <h3 className={cn("mb-2", subsectionTitleCls)}>
                Group to team
              </h3>
              <RuleTable rules={mappings.team_rules} targetLabel="Joins team" />
            </div>

            {mappings.bootstrap &&
              (mappings.bootstrap.admin_group ||
                mappings.bootstrap.admin_subjects.length > 0) && (
                <div>
                  <h3 className={cn("mb-1.5", subsectionTitleCls)}>
                    Cold-start administrators
                  </h3>
                  <p className="mb-2 text-xs text-[hsl(var(--muted-foreground))]">
                    Configured so the very first sign-in has somebody able to administer the rest.
                    Worth removing once real administrators exist.
                  </p>
                  <div className="space-y-1.5">
                    {mappings.bootstrap.admin_group && (
                      <div className="flex items-center gap-2 text-xs">
                        <span className="text-[hsl(var(--muted-foreground))]">Group</span>
                        <Pill tone="accent">{mappings.bootstrap.admin_group}</Pill>
                      </div>
                    )}
                    {mappings.bootstrap.admin_subjects.length > 0 && (
                      <div className="flex items-start gap-2 text-xs">
                        <span className="pt-0.5 text-[hsl(var(--muted-foreground))]">Subjects</span>
                        <PillRow
                          values={mappings.bootstrap.admin_subjects}
                          empty="None"
                          tone="accent"
                        />
                      </div>
                    )}
                  </div>
                </div>
              )}
          </div>
        )}
      </IamCardState>
    </SettingsCard>
  );
}
