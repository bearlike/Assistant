import { Check } from 'lucide-react';
interface SummaryBlockProps {
  summary: string[];
  testing: {
    command: string;
    passed: boolean;
  }[];
}
export function SummaryBlock({ summary, testing }: SummaryBlockProps) {
  return (
    <div className="mt-4 space-y-4">
      <div>
        <h4 className="text-sm font-bold text-[hsl(var(--foreground))] mb-2">
          Summary
        </h4>
        <ul className="space-y-2">
          {summary.map((item, i) =>
          <li
            key={i}
            className="flex items-start gap-2 text-sm text-[hsl(var(--muted-foreground))] leading-relaxed">

              <span className="mt-1.5 w-1 h-1 rounded-full bg-[hsl(var(--muted-foreground))] shrink-0" />
              <span>{item}</span>
            </li>
          )}
        </ul>
      </div>

      <div>
        <h4 className="text-sm font-bold text-[hsl(var(--foreground))] mb-2">
          Testing
        </h4>
        <div className="space-y-2">
          {testing.map((test, i) =>
          <div key={i} className="flex items-center gap-2">
              <span className="mt-0.5 w-1 h-1 rounded-full bg-[hsl(var(--muted-foreground))] shrink-0" />
              <div
              className={`flex items-center justify-center w-4 h-4 rounded ${test.passed ? 'bg-green-500/15 text-green-600 dark:text-green-400' : 'bg-red-500/15 text-red-600 dark:text-red-400'}`}>

                {test.passed && <Check className="w-3 h-3" />}
              </div>
              <code className="px-1.5 py-0.5 rounded bg-[hsl(var(--muted))] text-[hsl(var(--foreground))] text-xs font-mono">
                {test.command}
              </code>
            </div>
          )}
        </div>
      </div>
    </div>);

}