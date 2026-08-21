import { type ErrorInfo, type ReactNode, Component } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

import { Button } from "@/components/ui/button";

interface WikiErrorBoundaryProps {
  children: ReactNode;
}

interface WikiErrorBoundaryState {
  error: Error | null;
}

export class WikiErrorBoundary extends Component<
  WikiErrorBoundaryProps,
  WikiErrorBoundaryState
> {
  state: WikiErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): WikiErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, errorInfo: ErrorInfo): void {
    console.error("Wiki screen render failed", error, errorInfo);
  }

  private reload = (): void => {
    window.location.reload();
  };

  render(): ReactNode {
    const { error } = this.state;
    if (!error) return this.props.children;

    return (
      <main className="flex min-h-full items-center justify-center p-6">
        <section
          className="w-full max-w-2xl rounded-lg border border-[hsl(var(--border))] bg-[hsl(var(--card))] p-6 shadow-sm"
          role="alert"
        >
          <div className="flex items-start gap-3">
            <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-[hsl(var(--destructive)/0.12)] text-[hsl(var(--destructive-text))]">
              <AlertTriangle aria-hidden="true" className="h-5 w-5" />
            </div>
            <div className="min-w-0 space-y-1">
              <h1 className="text-lg font-semibold text-[hsl(var(--foreground))]">
                This wiki screen failed to load
              </h1>
              <p className="text-sm text-[hsl(var(--muted-foreground))]">
                Reload this page to try again.
              </p>
            </div>
          </div>
          <pre className="mt-5 max-h-32 overflow-auto whitespace-pre-wrap break-words rounded-md border border-[hsl(var(--border))] bg-[hsl(var(--muted))]/40 p-3 text-xs text-[hsl(var(--muted-foreground))]">
            {error.message || "An unexpected render error occurred."}
          </pre>
          <div className="mt-5">
            <Button
              leadingIcon={<RefreshCw aria-hidden="true" className="h-4 w-4" />}
              onClick={this.reload}
              variant="primary"
            >
              Reload page
            </Button>
          </div>
        </section>
      </main>
    );
  }
}
