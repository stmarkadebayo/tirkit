import type { ReactNode } from "react";
import { Route } from "react-router-dom";
import { DatasetView, Datasets } from "./pages/Datasets";
import { Compare, ExperimentView, Experiments } from "./pages/Experiments";
import { Annotation, Labeller } from "./pages/Annotation";
import { PromptView, Prompts } from "./pages/Prompts";
import Playground from "./pages/Playground";
import { AlertsPage, JudgeReliability, OnlineEvals } from "./pages/Online";
import Audit from "./pages/Audit";

export interface NavItem {
  to: string;
  label: string;
  /** Group heading in the navigation; Overview and Settings sit outside any group. */
  section: "" | "Observe" | "Evaluate" | "Build";
}

export const extraRoutes: ReactNode[] = [
  <Route key="datasets" path="datasets" element={<Datasets />} />,
  <Route key="dataset" path="datasets/:name" element={<DatasetView />} />,
  <Route key="experiments" path="experiments" element={<Experiments />} />,
  <Route key="experiment" path="experiments/:eid" element={<ExperimentView />} />,
  <Route key="compare" path="compare" element={<Compare />} />,
  <Route key="annotation" path="annotation" element={<Annotation />} />,
  <Route key="annotate" path="annotate/:qid" element={<Labeller />} />,
  <Route key="prompts" path="prompts" element={<Prompts />} />,
  <Route key="prompt" path="prompts/:name" element={<PromptView />} />,
  <Route key="playground" path="playground" element={<Playground />} />,
  <Route key="online" path="online-evals" element={<OnlineEvals />} />,
  <Route key="alerts" path="alerts" element={<AlertsPage />} />,
  <Route key="judge" path="judge" element={<JudgeReliability />} />,
  <Route key="audit" path="audit" element={<Audit />} />,
];

/** Navigation in display order (docs/DECISIONS.md 66): Overview first, then Observe, Evaluate, Build. */
export const NAV: NavItem[] = [
  { to: "overview", label: "Overview", section: "" },
  { to: "traces", label: "Traces", section: "Observe" },
  { to: "sessions", label: "Sessions", section: "Observe" },
  { to: "users", label: "Users", section: "Observe" },
  { to: "alerts", label: "Alerts", section: "Observe" },
  { to: "dashboard", label: "Dashboard", section: "Observe" },
  { to: "datasets", label: "Datasets", section: "Evaluate" },
  { to: "experiments", label: "Experiments", section: "Evaluate" },
  { to: "judge", label: "Judge reliability", section: "Evaluate" },
  { to: "annotation", label: "Annotation", section: "Evaluate" },
  { to: "online-evals", label: "Online evals", section: "Evaluate" },
  { to: "prompts", label: "Prompts", section: "Build" },
  { to: "playground", label: "Playground", section: "Build" },
  { to: "audit", label: "Audit log", section: "" },
  { to: "settings", label: "Settings", section: "" },
];
