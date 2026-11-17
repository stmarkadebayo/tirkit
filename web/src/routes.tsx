import type { ReactNode } from "react";
import { Route } from "react-router-dom";
import { DatasetView, Datasets } from "./pages/Datasets";
import { Compare, ExperimentView, Experiments } from "./pages/Experiments";
import { Annotation, Labeller } from "./pages/Annotation";
import { PromptView, Prompts } from "./pages/Prompts";
import Playground from "./pages/Playground";
import { AlertsPage, JudgeReliability, OnlineEvals } from "./pages/Online";

export interface NavItem {
  to: string;
  label: string;
  section: "Observe" | "Evaluate" | "Iterate" | "Project";
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
];

export const extraNav: NavItem[] = [
  { to: "alerts", label: "Alerts", section: "Observe" },
  { to: "datasets", label: "Datasets", section: "Evaluate" },
  { to: "experiments", label: "Experiments", section: "Evaluate" },
  { to: "annotation", label: "Annotation", section: "Evaluate" },
  { to: "online-evals", label: "Online evals", section: "Evaluate" },
  { to: "judge", label: "Judge reliability", section: "Evaluate" },
  { to: "prompts", label: "Prompts", section: "Iterate" },
  { to: "playground", label: "Playground", section: "Iterate" },
];
