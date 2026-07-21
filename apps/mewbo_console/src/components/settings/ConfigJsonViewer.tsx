/**
 * ConfigJsonViewer — the read-only "View as JSON" escape hatch at the bottom
 * of the Settings shell. Lifted out of `SettingsView`: a self-contained
 * collapsible `<details>` over whatever `config` object it's handed, with its
 * own Copy affordance.
 */
import { CopyButton } from "../CopyButton";
import { sectionTitleCls } from "./styles";

export function ConfigJsonViewer({ config }: { config: unknown }) {
  return (
    <details className="rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))]">
      <summary className={`cursor-pointer select-none px-4 py-3 ${sectionTitleCls}`}>
        View as JSON (read-only)
      </summary>
      <div className="relative border-t border-[hsl(var(--border))]">
        <div className="absolute right-2 top-2 z-10">
          <CopyButton text={JSON.stringify(config, null, 2)}>Copy</CopyButton>
        </div>
        <pre className="overflow-x-auto px-4 py-3 text-xs font-mono text-[hsl(var(--code-fg))] bg-[hsl(var(--code-body))]">
          {JSON.stringify(config, null, 2)}
        </pre>
      </div>
    </details>
  );
}
