// Pages live in the URL's fragment, so the server needs only one page: the built
// index.html. The viewer has three pages: the run list, a run, and two runs side by side.

import { useSyncExternalStore } from "react";

export type Route =
  | { page: "runs" }
  | { page: "run"; database: string }
  | { page: "compare"; left: string; right: string }
  | { page: "missing"; hash: string };

export function parseRoute(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").filter((part) => part !== "");
  const names = parts.map((part) => decodeURIComponent(part));
  if (names.length === 0) return { page: "runs" };
  if (names[0] === "runs" && names.length === 2) return { page: "run", database: names[1]! };
  if (names[0] === "compare" && names.length === 3) {
    return { page: "compare", left: names[1]!, right: names[2]! };
  }
  return { page: "missing", hash };
}

export function href(route: Route): string {
  switch (route.page) {
    case "runs":
      return "#/";
    case "run":
      return `#/runs/${encodeURIComponent(route.database)}`;
    case "compare":
      return `#/compare/${encodeURIComponent(route.left)}/${encodeURIComponent(route.right)}`;
    case "missing":
      return route.hash;
  }
}

function subscribe(onChange: () => void): () => void {
  window.addEventListener("hashchange", onChange);
  return () => window.removeEventListener("hashchange", onChange);
}

export function useRoute(): Route {
  const hash = useSyncExternalStore(subscribe, () => window.location.hash);
  return parseRoute(hash);
}
