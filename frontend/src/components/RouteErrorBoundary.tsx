import { Component, type ErrorInfo, type ReactNode } from 'react';
import { BRAND, SURFACE } from '../ui/brand';

interface RouteErrorBoundaryProps {
  readonly children: ReactNode;
}

interface RouteErrorBoundaryState {
  readonly error: Error | null;
}

/**
 * Contains a crashing section so the shell (sidebar, header, execution panel) stays usable.
 * Keyed by pathname in App.tsx, so navigating to another section clears the error.
 */
export class RouteErrorBoundary extends Component<RouteErrorBoundaryProps, RouteErrorBoundaryState> {
  state: RouteErrorBoundaryState = { error: null };

  static getDerivedStateFromError(error: Error): RouteErrorBoundaryState {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('Section crashed:', error, info.componentStack);
  }

  private readonly retry = (): void => this.setState({ error: null });

  render(): ReactNode {
    const { error } = this.state;
    if (error === null) return this.props.children;

    return (
      <div role="alert" className="flex min-h-[60vh] items-center justify-center px-8">
        <div className={`w-full max-w-md rounded-2xl p-8 text-center ${SURFACE.card}`}>
          <span className="material-symbols-outlined text-[32px] text-rose-500">error</span>
          <p className={`mt-3 ${SURFACE.eyebrow}`}>Section unavailable</p>
          <h2 className={`mt-2 text-lg font-semibold tracking-tight ${SURFACE.primary}`}>
            This section failed to render
          </h2>
          <p className={`mt-2 break-words font-mono text-xs ${SURFACE.muted}`}>{error.message}</p>
          <button
            type="button"
            onClick={this.retry}
            className={`mt-6 rounded-xl px-4 py-2 text-sm font-semibold text-white ${BRAND.gradient} ${BRAND.glow} ${BRAND.ring}`}
          >
            Retry
          </button>
        </div>
      </div>
    );
  }
}
