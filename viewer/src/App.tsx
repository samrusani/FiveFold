import { Component, type ReactNode } from "react";

import { Compare } from "./components/Compare";
import { RunList } from "./components/RunList";
import { RunPage } from "./components/RunPage";
import { href, useRoute } from "./route";

class PageBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    return this.state.failed ? (
      <p className="error" role="alert">
        The viewer could not display this page. Reload to try again.
      </p>
    ) : this.props.children;
  }
}

export function App() {
  const route = useRoute();
  return (
    <>
      <nav className="top">
        <a href={href({ page: "runs" })} className="brand">
          FiveFold viewer
        </a>
        <span className="muted">read-only: runs, timelines, scorecards</span>
      </nav>
      <main>
        <PageBoundary key={href(route)}>
          {route.page === "runs" ? <RunList /> : null}
          {route.page === "run" ? <RunPage database={route.database} /> : null}
          {route.page === "compare" ? <Compare left={route.left} right={route.right} /> : null}
          {route.page === "missing" ? (
            <p className="error" role="alert">
              No page at {route.hash}. <a href={href({ page: "runs" })}>Back to the runs.</a>
            </p>
          ) : null}
        </PageBoundary>
      </main>
    </>
  );
}
