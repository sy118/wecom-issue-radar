import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle, RotateCcw } from "lucide-react";

interface Props {
  children: ReactNode;
  /** Changes when the visible page changes; clears a previous page's error. */
  resetKey?: string;
}

interface State {
  error: Error | null;
}

/**
 * Prevents a single crashing page from blanking the whole application. The
 * sidebar stays usable so the user can navigate elsewhere or reload.
 */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("页面渲染失败", error, info.componentStack);
  }

  componentDidUpdate(previous: Props): void {
    if (this.state.error && previous.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="page-content">
        <div className="wb-card" style={{ maxWidth: 720, margin: "48px auto" }}>
          <div className="workbench-card-actions" style={{ gap: 10 }}>
            <span className="wb-check-icon" style={{ color: "hsl(var(--warning-text))" }}>
              <AlertTriangle size={20} />
            </span>
            <div>
              <h2 style={{ margin: 0, fontSize: 18 }}>这个页面加载失败了</h2>
              <p style={{ margin: "6px 0 0", color: "hsl(var(--muted-foreground))", fontSize: 13 }}>
                其他页面仍可正常使用。可以先重试，或打开设置检查配置。
              </p>
            </div>
          </div>
          <pre
            style={{
              maxHeight: 160,
              overflow: "auto",
              padding: 12,
              borderRadius: 10,
              background: "hsl(var(--muted))",
              fontSize: 12,
              whiteSpace: "pre-wrap",
            }}
          >
            {this.state.error.message}
          </pre>
          <div className="workbench-card-actions">
            <button type="button" className="button button-primary" onClick={() => this.setState({ error: null })}>
              <RotateCcw size={15} /> 重试
            </button>
            <button type="button" className="button button-secondary" onClick={() => window.location.reload()}>
              重新加载应用
            </button>
          </div>
        </div>
      </div>
    );
  }
}
