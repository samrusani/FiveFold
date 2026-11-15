import { Compare } from "./components/Compare";
import { RunList } from "./components/RunList";
import { RunPage } from "./components/RunPage";
import { href, useRoute } from "./route";

export function App() {
  const route = useRoute();
  return (
    <>
      <nav className="top">
        <a href={href({ page: "runs" })} className="brand">
          Fivefold viewer
        </a>
        <span className="muted">read-only: runs, timelines, scorecards</span>
      </nav>
      <main>
        {route.page === "runs" ? <RunList /> : null}
        {route.page === "run" ? <RunPage key={route.database} database={route.database} /> : null}
        {route.page === "compare" ? (
          <Compare key={`${route.left}/${route.right}`} left={route.left} right={route.right} />
        ) : null}
        {route.page === "missing" ? (
          <p className="error" role="alert">
            No page at {route.hash}. <a href={href({ page: "runs" })}>Back to the runs.</a>
          </p>
        ) : null}
      </main>
    </>
  );
}
