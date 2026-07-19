import { StrictMode, useEffect, useMemo, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import {
  Activity,
  BadgeCheck,
  CheckCircle2,
  CircleDot,
  CircleAlert,
  Code2,
  Cpu,
  ExternalLink,
  FlaskConical,
  GitMerge,
  GitPullRequest,
  ListChecks,
  RefreshCw,
  Rocket,
  RotateCcw,
  ShieldCheck,
  Target,
  Wrench,
  XCircle,
} from "lucide-react";
import "./styles.css";

type ModelRoute = { primary?: { model?: string; thinking?: string }; allow_fallback?: boolean };
type EventRecord = {
  event_type: string;
  summary: string;
  reason: string;
  created_at: string;
  evidence: Record<string, unknown>;
};
type UsageDetail = {
  model_totals: Array<{ model?: string; accounted_tokens?: number; calls?: number }>;
  token_hotspots: Array<{ model?: string; stage?: string; accounted_tokens?: number }>;
  efficiency: { repeated_prompt_tokens?: number; failed_attempt_tokens?: number; fallback_attempts?: number };
  failed_attempts?: number;
  warnings: string[];
  dimensions?: Array<{ date?: string; course_key?: string | null; work_package_key?: string | null; stage?: string; model?: string; tokens?: number; calls?: number; input_tokens?: number; cached_input_tokens?: number; cache_write_tokens?: number; reasoning_tokens?: number; output_tokens?: number; total_tokens?: number }>;
};
type WorkflowTimelineItem = {
  id?: string;
  stage?: string;
  status?: string;
  title?: string;
  summary?: string;
  agent?: string | null;
  model?: string | null;
  attempts?: number;
  workflow?: string;
  pr_url?: string | null;
  updated_at?: string | null;
  loop?: boolean;
  superseded_retry?: boolean;
};
type WorkflowRun = {
  workflow?: string;
  index?: number;
  title?: string;
  kind?: "build" | "review" | "completion" | string;
  status?: string;
  current?: boolean;
  cards?: number;
  loops?: number;
  blocked?: number;
  done?: number;
  historical_blockers?: number;
  superseded_retries?: number;
  updated_at?: string | null;
  timeline?: WorkflowTimelineItem[];
};
type StageHistory = {
  stage?: string;
  total?: number;
  done?: number;
  active?: number;
  blocked?: number;
  loops?: number;
  retry_attempts?: number;
  historical_blockers?: number;
  superseded_retries?: number;
  models?: string[];
};
type WorkboardStatus = {
  status?: string;
  board?: string;
  workflow?: string;
  cards?: number;
  counts?: Record<string, number>;
  active_cards?: number;
  current_stage?: string | null;
  stages?: Array<{ stage?: string; total?: number; done?: number; active?: number; blocked?: number; loops?: number }>;
  stage_history?: StageHistory[];
  timeline?: WorkflowTimelineItem[];
  workflow_runs?: WorkflowRun[];
  loop_count?: number;
  total_loop_count?: number;
  review_cycles?: number;
  reviews_passed?: number;
  review_status?: "not_run" | "in_review" | "passed" | "blocked" | string;
  test_status?: "not_run" | "running" | "passed" | "blocked" | string;
  blockers?: number;
  current_blockers?: number;
  historical_blockers?: number;
  historical_review_blockers?: number;
  superseded_retries?: number;
  terminal?: boolean;
  completion_status?: string;
  pr_count?: number;
  pr_numbers?: number[];
  pr_urls?: string[];
  milestones?: MilestoneEvidence[];
  usage_sync?: { status?: string; sessions_seen?: number; sessions_imported?: number; sessions_with_usage?: number; error?: string };
  message?: string;
  error?: string;
};
type EvidenceArtifact = { kind?: string; title?: string; url?: string; path?: string; mime_type?: string; viewport?: string; description?: string };
type MilestonePolicy = { required?: boolean; minimum_pass_rate?: number; require_command?: boolean; require_screenshot?: boolean; minimum_screenshots?: number };
type MilestoneEvidenceDetail = {
  status?: string;
  reason?: string;
  source_card_id?: string | null;
  head_sha?: string | null;
  current_head_sha?: string | null;
  pass_rate?: number | null;
  tests_total?: number | null;
  tests_passed?: number | null;
  tests_failed?: number | null;
  tests_skipped?: number | null;
  commands?: string[];
  screenshots?: EvidenceArtifact[];
  artifacts?: EvidenceArtifact[];
  model?: string | null;
  provider?: string | null;
  captured_at?: string | null;
  summary?: string | null;
};
type MilestoneEvidence = {
  course_key: string;
  work_package_key: string;
  title: string;
  objective: string;
  status: string;
  policy?: MilestonePolicy;
  evidence?: MilestoneEvidenceDetail;
  pr_url?: string | null;
};
type GitHubStatus = {
  status?: string;
  open_prs?: number;
  open_issues?: number;
  branches?: number;
  checks?: { recent?: number; failed?: number; pending?: number; passed?: number };
  prs?: Array<{ number?: number; title?: string; url?: string; headRefName?: string; isDraft?: boolean; mergeStateStatus?: string; reviewDecision?: string; updatedAt?: string }>;
  error?: string;
};
type Repo = {
  full_name: string;
  local_path: string;
  exists: boolean;
  dirty: boolean;
  operation_mode: string;
  completion_policy: string;
  schedule_enabled?: boolean;
  allow_autonomous_merge?: boolean;
  state: string;
  orchestrator?: string;
  orchestration_board?: string | null;
  worker_models?: Record<string, string>;
  worker_runtimes?: Record<string, string>;
  notification_route?: string | null;
  model_profiles?: Record<string, ModelRoute>;
  qa_profiles?: QAProfile[];
  surfaces?: string[];
  tokens: { total_tokens?: number; accounted_tokens?: number };
  usage_detail?: UsageDetail;
  active_work?: Record<string, unknown> | null;
  workboard_status?: WorkboardStatus | null;
  github_status?: GitHubStatus;
  telemetry?: { status?: string; measured_records?: number; total_records?: number };
  events?: EventRecord[];
  warnings: string[];
};
type QAProfile = {
  key: string;
  title: string;
  surfaces?: string[];
  checks?: string[];
  required_tools?: string[];
  reviewer_role?: string;
  enabled?: boolean;
};
type ReadinessRequirement = {
  key: string;
  category: string;
  question: string;
  status: string;
  answer?: string | null;
  owner_decision_required?: boolean;
};
type WorkPackage = { key: string; title: string; objective: string; status: string; dependencies?: string[]; acceptance_criteria?: string[]; checks?: string[]; qa_profiles?: string[]; test_evidence_policy?: MilestonePolicy; model_profiles?: Record<string, ModelRoute> };
type Checkpoint = { key: string; title: string; reason: string; status: string; blocks_work_packages?: string[] };
type Course = {
  key: string;
  title: string;
  kind: string;
  status: string;
  goal: string;
  plan_revision?: number;
  milestone_approval?: string;
  scope?: string[];
  acceptance_criteria?: string[];
  exit_criteria?: string[];
  readiness: ReadinessRequirement[];
  work_packages: WorkPackage[];
  checkpoints: Checkpoint[];
  model_profiles?: Record<string, ModelRoute>;
};
type CourseSummary = {
  repository: string;
  course: Course;
  readiness: { ready: boolean; unresolved?: string[]; owner_decisions?: string[]; verified?: string[] };
  number_one?: { session_id?: string; runtime?: string; model?: string; plan_revision?: number; last_review_at?: string; summary?: string | null } | null;
  milestone_changes?: MilestoneChangeProposal[];
  milestone_reviews?: Array<{ status?: string; summary?: string; next_action?: string; reviewed_at?: string; model?: string; number_one_session_id?: string }>;
};
type MilestoneChangeProposal = {
  proposal_id: string;
  summary: string;
  reason: string;
  status: string;
  impact?: string;
  base_revision?: number;
  changes?: Array<{ kind?: string; summary?: string; work_package_key?: string | null; work_package?: { key?: string; title?: string } | null }>;
};
type ModelConfig = {
  global_profiles: Record<string, ModelRoute>;
  runtime_profiles: Record<string, Record<string, ModelRoute>>;
  runtimes: string[];
  usage?: UsageConfig;
};
type UsageConfig = {
  daily_token_limit?: number | null;
  model_daily_token_limits?: Record<string, number>;
  block_on_unknown?: boolean;
  allow_incomplete_telemetry?: boolean;
  retention_days?: number;
};
type PlanningSession = { prompt: string; next_questions: string[]; interaction: string; mutation_requires_course_approval: boolean };
type Portfolio = { repos: Repo[] };
type Courses = { courses: CourseSummary[] };
type ScheduleJob = { name: string; every: string; id?: string | null; enabled: boolean; health: string; drift?: string[]; duplicates?: number };
type ScheduleStatus = { status: string; jobs: ScheduleJob[] };
type UpdatePayload = Record<string, unknown>;
type RegistrationResult = {
  status?: string;
  follow_up_required?: boolean;
  follow_up_message?: string;
  notification_status?: string;
  discovery?: {
    local_clone?: { path?: string; exists?: boolean; cloned?: boolean; remote_matches?: boolean | null };
    planning_document?: { path?: string; found?: boolean; source?: string; candidates?: string[]; reason?: string };
  };
};
type ModelPreset = "economy" | "balanced" | "maximum_quality" | "local_first";
type IntelligenceLevel = "economy" | "balanced" | "deep" | "maximum";

const CONTROL_UI_TOKEN = document
  .querySelector<HTMLMetaElement>('meta[name="make-it-so-control-token"]')
  ?.content ?? "";

const ROUTE_DEFAULTS = [
  { role: "number_one", label: "Number 1 leadership", model: "codex/gpt-5.6-sol", effort: "high" },
  { role: "strategist", label: "Course strategist", model: "codex/gpt-5.6-sol", effort: "high" },
  { role: "course_verifier", label: "Course verifier", model: "codex/gpt-5.6-sol", effort: "high" },
  { role: "baseline", label: "Baseline analyst", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "baseline_analyst", label: "Baseline gap analyst", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "planner", label: "Planning", model: "codex/gpt-5.6-terra", effort: "medium" },
  { role: "readiness_reviewer", label: "Readiness review", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "decomposer", label: "Work decomposition", model: "codex/gpt-5.6-terra", effort: "medium" },
  { role: "package_planner", label: "Package planning", model: "codex/gpt-5.6-terra", effort: "medium" },
  { role: "subsystem_analyst", label: "Subsystem analysis", model: "codex/gpt-5.6-luna", effort: "medium" },
  { role: "coder", label: "Coding", model: "codex/gpt-5.3-codex-spark", effort: "medium" },
  { role: "fast_coder", label: "Fast coding", model: "codex/gpt-5.3-codex-spark", effort: "medium" },
  { role: "focused_coder", label: "Focused coding", model: "codex/gpt-5.3-codex-spark", effort: "medium" },
  { role: "complex_coder", label: "Complex coding", model: "codex/gpt-5.6-sol", effort: "high" },
  { role: "local_coder", label: "Local coding", model: "ollama/qualified-local", effort: "medium" },
  { role: "tester", label: "Testing", model: "codex/gpt-5.6-luna", effort: "medium" },
  { role: "qa_assistant", label: "QA assistant", model: "codex/gpt-5.6-luna", effort: "medium" },
  { role: "reviewer", label: "Independent review", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "code_reviewer", label: "Code review", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "comment_adjudicator", label: "Comment adjudication", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "security_reviewer", label: "Security review", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "ux_reviewer", label: "UX review", model: "codex/gpt-5.6-terra", effort: "medium" },
  { role: "ui_qa_reviewer", label: "UI QA", model: "codex/gpt-5.6-terra", effort: "medium" },
  { role: "final_reviewer", label: "Final review", model: "codex/gpt-5.6-sol", effort: "high" },
  { role: "merger", label: "Merge gate", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "recovery_planner", label: "Recovery planning", model: "codex/gpt-5.6-terra", effort: "high" },
  { role: "summarizer", label: "Summaries", model: "codex/gpt-5.6-luna", effort: "low" },
  { role: "verifier", label: "Post-merge verification", model: "codex/gpt-5.6-terra", effort: "high" },
] as const;
type EditableRoute = { model: string; effort: string };

function callGateway<T>(path: string, params: Record<string, unknown> = {}): Promise<T> {
  return fetch(`/make-it-so/api/${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-make-it-so-control-token": CONTROL_UI_TOKEN,
    },
    body: JSON.stringify(params),
  }).then(async (response) => {
    const payload = await response.json().catch(() => null) as unknown;
    if (!response.ok) {
      const detail = payload && typeof payload === "object" && "error" in payload
        ? String((payload as { error?: unknown }).error ?? "")
        : "";
      throw new Error(detail || `Gateway request failed: ${response.status}`);
    }
    return payload as T;
  });
}

function normalizeGithubRepository(value: string): string {
  const trimmed = value.trim();
  const sshMatch = trimmed.match(/^git@github\.com:([^/\s]+\/[^/\s]+?)(?:\.git)?\/?$/i);
  if (sshMatch) return sshMatch[1];
  try {
    const parsed = new URL(trimmed);
    if (parsed.protocol === "http:" || parsed.protocol === "https:") {
      if (!/^(?:www\.)?github\.com$/i.test(parsed.hostname) || parsed.search || parsed.hash) {
        throw new Error("Use a GitHub repository URL or owner/repository.");
      }
      const parts = parsed.pathname.replace(/\.git\/?$/i, "").split("/").filter(Boolean);
      if (parts.length === 2) return `${parts[0]}/${parts[1]}`;
    }
  } catch (reason) {
    if (reason instanceof Error && reason.message !== "Invalid URL") throw reason;
  }
  if (/^[^/\s]+\/[^/\s]+$/.test(trimmed)) return trimmed;
  throw new Error("Enter owner/repository or a GitHub repository URL.");
}

function stageLabel(stage?: string | null): string {
  return (stage || "unknown").split("_").join(" ");
}

function compactTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return value.toLocaleString();
}

function shortModel(value?: string | null): string {
  return (value || "model unknown").replace(/^codex\//, "").replace(/^openai-codex\//, "");
}

const EXECUTION_LANES = [
  { stage: "implementation", label: "Build", icon: Code2 },
  { stage: "review", label: "Review", icon: ShieldCheck },
  { stage: "repair", label: "Repair", icon: Wrench },
  { stage: "test", label: "Test", icon: FlaskConical },
  { stage: "final_review", label: "Final", icon: BadgeCheck },
  { stage: "merge", label: "Merge", icon: GitMerge },
  { stage: "post_merge", label: "Verify", icon: Rocket },
] as const;

function stageTelemetry(repo: Repo, stage: string) {
  const rows = (repo.usage_detail?.dimensions ?? []).filter((row) => row.stage === stage);
  return {
    tokens: rows.reduce((total, row) => total + (row.tokens ?? 0), 0),
    models: [...new Set(rows.map((row) => row.model).filter((model): model is string => Boolean(model)))],
  };
}

function stageTone(stage?: StageHistory, terminal = false): string {
  if (!stage) return "idle";
  if (terminal && (stage.done ?? 0) > 0) return "done";
  if ((stage.active ?? 0) > 0) return "active";
  if ((stage.blocked ?? 0) > 0 && (stage.done ?? 0) === 0) return "blocked";
  if ((stage.done ?? 0) > 0) return "done";
  return "idle";
}

function stageCountLabel(stage?: StageHistory): string {
  if (!stage) return "--";
  const done = stage.done ?? 0;
  const active = stage.active ?? 0;
  const historicalBlockers = stage.historical_blockers ?? 0;
  if (done > 0 && active === 0) return "Complete";
  if (historicalBlockers > 0 && done === 0) return "Blocked";
  return `${done}/${stage.total ?? 0}`;
}

function stageCountDetail(stage?: StageHistory): string | undefined {
  if (!stage) return undefined;
  const details = [`${stage.done ?? 0} successful`, `${stage.total ?? 0} recorded`];
  if (stage.retry_attempts) details.push(`${stage.retry_attempts} retry attempt${stage.retry_attempts === 1 ? "" : "s"}`);
  if (stage.superseded_retries) details.push(`${stage.superseded_retries} superseded`);
  if (stage.historical_blockers) details.push(`${stage.historical_blockers} historical blocker${stage.historical_blockers === 1 ? "" : "s"}`);
  return details.join("; ");
}

function statusLabel(value?: string | null): string {
  return (value || "not available").split("_").join(" ");
}

function checkStatus(github?: GitHubStatus): string {
  if (!github || github.status !== "available") return "not available";
  if ((github.checks?.failed ?? 0) > 0) return "failing";
  if ((github.checks?.pending ?? 0) > 0) return "running";
  if ((github.checks?.passed ?? 0) > 0) return "passing";
  return "not run";
}

function Metric({ label, value, tone = "neutral" }: { label: string; value: string | number; tone?: string }) {
  return <div className={`metric metric-${tone}`}><strong>{value}</strong><span>{label}</span></div>;
}

function PortfolioSummary({ repos }: { repos: Repo[] }) {
  const openPrs = repos.reduce((total, repo) => total + (repo.github_status?.open_prs ?? 0), 0);
  const trackedPrs = repos.reduce((total, repo) => total + (repo.workboard_status?.pr_count ?? 0), 0);
  const reviewCycles = repos.reduce((total, repo) => total + (repo.workboard_status?.review_cycles ?? 0), 0);
  const failedChecks = repos.reduce((total, repo) => total + (repo.github_status?.checks?.failed ?? 0), 0);
  const pendingChecks = repos.reduce((total, repo) => total + (repo.github_status?.checks?.pending ?? 0), 0);
  const blockers = repos.reduce((total, repo) => total + (repo.workboard_status?.blockers ?? repo.workboard_status?.counts?.blocked ?? 0), 0);
  const tokens = repos.reduce((total, repo) => total + (repo.tokens.accounted_tokens ?? repo.tokens.total_tokens ?? 0), 0);
  const active = repos.filter((repo) => repo.operation_mode !== "disabled" && repo.state !== "merged").length;
  return <section className="portfolio-summary" aria-label="Portfolio summary">
    <div className="summary-heading"><div><p className="eyebrow">COMMAND DECK</p><h3>Fleet at a glance</h3></div><span className="summary-note">Live GitHub and Workboard facts</span></div>
    <div className="portfolio-kpis" tabIndex={0} aria-label="Portfolio metrics">
      <Metric label="registered" value={repos.length} />
      <Metric label="active courses" value={active} tone={active ? "accent" : "neutral"} />
      <Metric label="open PRs" value={openPrs} tone={openPrs ? "accent" : "neutral"} />
      <Metric label="PRs in workflow" value={trackedPrs} />
      <Metric label="review cycles" value={reviewCycles} />
      <Metric label="checks" value={failedChecks ? `${failedChecks} failing` : pendingChecks ? `${pendingChecks} running` : "passing"} tone={failedChecks ? "danger" : pendingChecks ? "warn" : "good"} />
      <Metric label="blockers" value={blockers} tone={blockers ? "danger" : "good"} />
      <Metric label="tokens recorded" value={tokens.toLocaleString()} />
    </div>
  </section>;
}

type GoalState = "complete" | "in_progress" | "ready" | "blocked" | "paused" | "not_started";

function goalStateLabel(state: GoalState): string {
  return {
    complete: "Complete",
    in_progress: "In progress",
    ready: "Ready to engage",
    blocked: "Blocked",
    paused: "Paused",
    not_started: "Not started",
  }[state];
}

function courseGoalState(item: CourseSummary, repo: Repo): GoalState {
  const workboard = repo.workboard_status;
  const terminal = Boolean(workboard?.terminal || workboard?.status === "completed" || repo.state === "merged");
  const blockers = workboard?.current_blockers ?? workboard?.blockers ?? 0;
  if (item.course.status === "blocked" || blockers > 0) return "blocked";
  if (item.course.status === "completed" || terminal) return "complete";
  if (item.course.status === "paused") return "paused";
  if (item.course.status === "engaged" || workboard?.status === "in_progress") return "in_progress";
  if (item.readiness.ready || item.course.status === "awaiting_approval") return "ready";
  return "not_started";
}

function ExecutiveSummary({ repo, courses }: { repo: Repo; courses: Courses | null }) {
  const workboard = repo.workboard_status;
  const terminal = Boolean(workboard?.terminal || workboard?.status === "completed" || repo.state === "merged");
  const blockers = workboard?.current_blockers ?? workboard?.blockers ?? 0;
  const checks = checkStatus(repo.github_status);
  const courseItems = courses?.courses.filter((item) => item.repository === repo.full_name) ?? [];
  const goals = courseItems.map((item) => ({ item, state: courseGoalState(item, repo) }));
  const workPackages = courseItems.flatMap((item) => item.course.work_packages);
  const completedPackages = workPackages.filter((item) => item.status === "complete").length;
  const completeGoals = goals.filter((goal) => goal.state === "complete").length;
  const milestones = workboard?.milestones ?? [];
  const passedEvidence = milestones.filter((item) => item.evidence?.status === "passed").length;
  const screenshotCount = milestones.reduce((total, item) => total + (item.evidence?.screenshots?.length ?? 0), 0);
  const hasBlockedGoal = goals.some((goal) => goal.state === "blocked");
  const overallState: GoalState | "loading" = courses === null
    ? "loading"
    : hasBlockedGoal
    ? "blocked"
    : goals.length > 0 && completeGoals === goals.length
    ? "complete"
    : goals.some((goal) => goal.state === "in_progress")
    ? "in_progress"
    : goals.some((goal) => goal.state === "ready")
    ? "ready"
    : "not_started";
  const overallLabel = overallState === "loading" ? "Loading charter" : goalStateLabel(overallState);
  const narrative = courses === null
    ? "Loading the project charter and its declared goals."
    : !goals.length
    ? "No high-level goal is registered for this repository yet."
    : overallState === "complete"
    ? "The implementation course is merged and post-merge verified. Production deployment remains a separate outcome."
    : overallState === "blocked"
    ? "The course has work that needs attention before it can advance."
    : overallState === "in_progress"
    ? `${completedPackages} of ${workPackages.length || 0} delivery milestones are marked complete.`
    : "The project has a declared direction and is waiting for the next course transition.";
  return <section className="executive-summary" aria-labelledby="executive-summary-title">
    <div className="executive-heading"><div><p className="eyebrow">EXECUTIVE BRIEF</p><h3 id="executive-summary-title">Project at a glance</h3><span>{repo.full_name}</span></div><span className={`executive-state ${overallState}`}>{overallLabel}</span></div>
    <div className="executive-grid">
      <div className="executive-narrative"><div className="executive-signal"><Target size={20} aria-hidden="true" /><div><strong>{terminal ? "Implementation verified" : repo.state.split("_").join(" ")}</strong><span>{narrative}</span></div></div><div className="executive-metrics" tabIndex={0} aria-label="Project metrics"><Metric label="goals complete" value={courses === null ? "-" : `${completeGoals}/${goals.length}`} tone={overallState === "complete" ? "good" : "neutral"} /><Metric label="milestones" value={courses === null ? "-" : `${completedPackages}/${workPackages.length}`} /><Metric label="evidence passed" value={milestones.length ? `${passedEvidence}/${milestones.length}` : "-"} tone={milestones.length && passedEvidence === milestones.length ? "good" : "neutral"} /><Metric label="screenshots" value={screenshotCount} /><Metric label="PRs" value={workboard?.pr_count ?? repo.github_status?.open_prs ?? 0} /><Metric label="blockers" value={blockers} tone={blockers ? "danger" : "good"} /><Metric label="checks" value={checks} tone={checks === "failing" ? "danger" : checks === "passing" ? "good" : checks === "running" ? "warn" : "neutral"} /></div></div>
      <div className="goal-board" aria-label={`High-level goals for ${repo.full_name}`}><div className="goal-board-heading"><div><ListChecks size={17} aria-hidden="true" /><strong>Project goals</strong></div><span>{courses === null ? "Loading" : `${goals.length} tracked`}</span></div>{courses === null ? <p className="muted">Loading declared goals...</p> : goals.length ? goals.map(({ item, state }) => <div className="goal-item" key={`${item.repository}:${item.course.key}`}><div className="goal-item-marker">{state === "complete" ? <CheckCircle2 size={16} aria-hidden="true" /> : state === "blocked" ? <CircleAlert size={16} aria-hidden="true" /> : <Target size={16} aria-hidden="true" />}</div><div className="goal-item-copy"><strong>{item.course.title}</strong><span>{item.course.goal}</span><small>{item.course.work_packages.length ? `${item.course.work_packages.length} delivery milestone${item.course.work_packages.length === 1 ? "" : "s"}` : "No delivery milestones recorded"}</small></div><em className={`goal-status ${state}`}>{goalStateLabel(state)}</em></div>) : <p className="muted">Register a course charter to track a high-level goal.</p>}</div>
    </div>
  </section>;
}

function ExecutionPath({ repo }: { repo: Repo }) {
  const workboard = repo.workboard_status;
  if (!workboard || workboard.status === "unknown" || workboard.status === "unavailable") return null;
  const runs = workboard.workflow_runs ?? [];
  const stageHistory: StageHistory[] = workboard.stage_history ?? workboard.stages ?? [];
  const terminal = workboard.terminal || workboard.status === "completed" || repo.state === "merged";
  const totalLoops = workboard.total_loop_count ?? workboard.loop_count ?? 0;
  const coderRoute = repo.worker_models?.coder;
  const coderRuntime = repo.worker_runtimes?.coder ?? "openclaw";
  return <section className="execution-console" aria-label={`Execution evidence for ${repo.full_name}`}>
    <div className="console-heading">
      <div><p className="eyebrow">MISSION TELEMETRY</p><h4>How the work moved</h4><span>Every recorded build, review, repair, test, and completion workflow.</span></div>
      <div className={`completion-beacon ${terminal ? "complete" : "active"}`}><Activity size={15} aria-hidden="true" /><span>{terminal ? "Course verified" : workboard.current_stage ? `Active: ${stageLabel(workboard.current_stage)}` : "Awaiting next transition"}</span></div>
    </div>
    <div className="stage-map" role="list" aria-label="SDLC stage evidence" tabIndex={0}>
      {EXECUTION_LANES.map(({ stage, label, icon: Icon }, index) => {
        const history = stageHistory.find((item) => item.stage === stage);
        const telemetry = stageTelemetry(repo, stage);
        const models = telemetry.models.length ? telemetry.models : history?.models ?? [];
        const tone = stageTone(history, terminal);
        return <div className={`stage-node ${tone}`} role="listitem" key={stage}>
          <div className="stage-node-top"><span className="stage-icon"><Icon size={16} aria-hidden="true" /></span><span className="stage-count" title={stageCountDetail(history)}>{stageCountLabel(history)}</span></div>
          <strong>{label}</strong>
          <span className="stage-model" title={models.join(", ")}>{models.length ? models.map(shortModel).join(", ") : "No recorded model"}</span>
          <span className="stage-tokens">{telemetry.tokens ? `${compactTokens(telemetry.tokens)} tokens` : history ? `${history.retry_attempts ?? history.loops ?? 0} retry attempt${(history.retry_attempts ?? history.loops ?? 0) === 1 ? "" : "s"}` : "No run"}</span>
          {index < EXECUTION_LANES.length - 1 && <span className="stage-connector" aria-hidden="true" />}
        </div>;
      })}
    </div>
    <div className="feedback-band">
      <div className="feedback-icon"><RotateCcw size={18} aria-hidden="true" /></div>
      <div><strong>{totalLoops} feedback loop{totalLoops === 1 ? "" : "s"}</strong><span>Review findings and failed gates route work back through repair, then forward through independent verification.</span></div>
      <div className="feedback-counts"><span><b>{workboard.reviews_passed ?? 0}</b> reviews passed</span><span><b>{workboard.superseded_retries ?? 0}</b> superseded retr{(workboard.superseded_retries ?? 0) === 1 ? "y" : "ies"}</span>{(workboard.historical_blockers ?? 0) > 0 && <span><b>{workboard.historical_blockers ?? 0}</b> historical blockers</span>}<span><b>{workboard.current_blockers ?? workboard.blockers ?? 0}</b> current blockers</span></div>
    </div>
    {coderRoute && <div className="model-route-note"><Cpu size={17} aria-hidden="true" /><div><strong>Coding route: {shortModel(coderRoute)} via {coderRuntime === "codex" ? "direct Codex" : "OpenClaw"}</strong><span>{coderRuntime === "codex" ? "OpenClaw owns the Workboard lifecycle; the coding card executes through your ChatGPT-authenticated Codex CLI and records provider token telemetry." : "This coding card executes through the OpenClaw agent route configured for the repository."}</span></div></div>}
    <div className="run-history">
      <div className="run-history-heading"><div><p className="eyebrow">FLIGHT RECORDER</p><h5>Workflow runs</h5></div><span>{runs.length} recorded run{runs.length === 1 ? "" : "s"}</span></div>
      {runs.length ? runs.map((run) => <article className={`workflow-run ${run.status ?? "unknown"}`} key={run.workflow ?? run.index}>
        <div className="run-label"><span>RUN {String(run.index ?? 0).padStart(2, "0")}</span><strong>{run.kind ?? "workflow"}</strong><em>{statusLabel(run.status)}</em></div>
        <div className="run-body">
          <div className="run-title"><div><strong>{run.title ?? "Workboard workflow"}</strong><span>{run.status === "completed" ? "Complete" : `${run.done ?? 0}/${run.cards ?? 0} cards complete`}{run.superseded_retries ? ` | ${run.superseded_retries} superseded retr${run.superseded_retries === 1 ? "y" : "ies"}` : run.loops ? ` | ${run.loops} feedback loop${run.loops === 1 ? "" : "s"}` : ""}</span></div>{run.current && <span className="current-run">current</span>}</div>
          <div className="run-steps" role="list">
            {(run.timeline ?? []).slice(-12).map((item, index) => {
              const supersededRetry = item.superseded_retry || (item.status === "blocked" && item.loop && run.status === "completed");
              return <div className={`run-step ${supersededRetry ? "superseded" : item.status ?? "unknown"} ${item.loop ? "loop" : ""}`} role="listitem" key={item.id ?? `${run.workflow}-${item.stage}-${index}`}>
              <span className="run-step-marker" aria-hidden="true">{item.status === "done" || supersededRetry ? <CheckCircle2 size={14} /> : item.status === "blocked" ? <XCircle size={14} /> : <CircleDot size={14} />}</span>
              <div><strong>{stageLabel(item.stage)}{supersededRetry ? " - superseded retry" : ""}</strong><span>{item.model ? shortModel(item.model) : item.agent ?? "deterministic"}</span><small title={item.summary ?? item.title}>{supersededRetry ? "Retry preserved as audit history; successful path completed." : item.summary ?? item.title ?? "Workboard transition"}</small></div>
              {item.pr_url && <a href={item.pr_url} target="_blank" rel="noreferrer" aria-label={`Open PR for ${item.title ?? item.stage}`}><GitPullRequest size={14} /><span>PR</span><ExternalLink size={11} /></a>}
            </div>;
            })}
          </div>
        </div>
      </article>) : <p className="muted">No durable Workboard workflow history has been recorded yet.</p>}
    </div>
    {terminal && <div className="completion-strip"><BadgeCheck size={18} aria-hidden="true" /><div><strong>Implementation complete</strong><span>Merged and post-merge verification passed. Superseded retry cards above are audit history, not failed work. Production deployment remains a separately verified outcome.</span></div></div>}
  </section>;
}

function routeValue(repo: Repo, role: string, fallbackModel: string, fallbackEffort: string) {
  const route = repo.model_profiles?.[role]?.primary;
  return { model: route?.model ?? fallbackModel, effort: route?.thinking ?? fallbackEffort };
}

function initialRoutesFromProfiles(profiles?: Record<string, ModelRoute>): Record<string, EditableRoute> {
  return Object.fromEntries(ROUTE_DEFAULTS.map(({ role, model, effort }) => {
    const route = profiles?.[role]?.primary;
    return [role, { model: route?.model ?? model, effort: route?.thinking ?? effort }];
  }));
}

const STAGE_ROUTE_DEFAULTS: Record<string, EditableRoute> = {
  baseline: { model: "codex/gpt-5.6-terra", effort: "high" },
  planning: { model: "codex/gpt-5.6-terra", effort: "medium" },
  decomposition: { model: "codex/gpt-5.6-terra", effort: "medium" },
  implementation: { model: "codex/gpt-5.3-codex-spark", effort: "medium" },
  repair: { model: "codex/gpt-5.3-codex-spark", effort: "medium" },
  review: { model: "codex/gpt-5.6-terra", effort: "high" },
  comment_adjudication: { model: "codex/gpt-5.6-terra", effort: "high" },
  test: { model: "codex/gpt-5.6-luna", effort: "medium" },
  ux_review: { model: "codex/gpt-5.6-terra", effort: "medium" },
  final_review: { model: "codex/gpt-5.6-sol", effort: "high" },
  merge: { model: "codex/gpt-5.6-terra", effort: "medium" },
  post_merge: { model: "codex/gpt-5.6-terra", effort: "high" },
};

function initialStageRoute(profile?: ModelRoute, stageName = "implementation"): EditableRoute {
  const fallback = STAGE_ROUTE_DEFAULTS[stageName] ?? STAGE_ROUTE_DEFAULTS.implementation;
  return { model: profile?.primary?.model ?? fallback.model, effort: profile?.primary?.thinking ?? fallback.effort };
}

function effectiveRoute(repo: Repo | undefined, course: Course, workPackage: WorkPackage | undefined, role: string, fallbackModel: string, fallbackEffort: string, stageName = "implementation") {
  const stageKey = `stage:${stageName}`;
  const candidates: Array<[string, ModelRoute | undefined]> = [
    [`work package ${stageKey}`, workPackage?.model_profiles?.[stageKey]],
    [`course ${stageKey}`, course.model_profiles?.[stageKey]],
    ["work package", workPackage?.model_profiles?.[role]],
    ["course", course.model_profiles?.[role]],
    ["repository", repo?.model_profiles?.[role]],
  ];
  const selected = candidates.find(([, profile]) => profile?.primary?.model);
  return {
    model: selected?.[1]?.primary?.model ?? fallbackModel,
    effort: selected?.[1]?.primary?.thinking ?? fallbackEffort,
    source: selected?.[0] ?? "default",
  };
}

function modelProfilesForRoutes(routes: Record<string, EditableRoute>): Record<string, ModelRoute> {
  return Object.fromEntries(ROUTE_DEFAULTS.map(({ role }) => [role, {
    primary: { model: routes[role].model, thinking: routes[role].effort },
    ...(role === "reviewer" || role === "final_reviewer" ? { allow_fallback: false } : {}),
  }]));
}

function initialRoutes(repo: Repo): Record<string, EditableRoute> {
  return Object.fromEntries(ROUTE_DEFAULTS.map(({ role, model, effort }) => {
    const configured = routeValue(repo, role, model, effort);
    return [role, configured];
  }));
}

const MODEL_PRESET_LABELS: Record<ModelPreset, string> = {
  economy: "Economy",
  balanced: "Balanced",
  maximum_quality: "Maximum quality",
  local_first: "Local first",
};

const INTELLIGENCE_LEVEL_LABELS: Record<IntelligenceLevel, string> = {
  economy: "Economy - one step lighter",
  balanced: "Balanced - recommended per role",
  deep: "Deep - one step stronger",
  maximum: "Maximum - highest supported",
};

function routesForIntelligence(level: IntelligenceLevel, routes: Record<string, EditableRoute>): Record<string, EditableRoute> {
  const efforts = ["low", "medium", "high", "xhigh"];
  const delta = level === "economy" ? -1 : level === "deep" ? 1 : 0;
  return Object.fromEntries(ROUTE_DEFAULTS.map(({ role, effort }) => {
    const current = routes[role];
    if (level === "maximum") return [role, { ...current, effort: "xhigh" }];
    const defaultIndex = efforts.indexOf(effort);
    return [role, { ...current, effort: efforts[Math.max(0, Math.min(efforts.length - 1, defaultIndex + delta))] }];
  }));
}

function presetRoutes(preset: ModelPreset): Record<string, EditableRoute> {
  const expensiveRoles = new Set([
    "strategist", "course_verifier", "baseline", "baseline_analyst", "planner",
    "readiness_reviewer", "reviewer", "code_reviewer", "comment_adjudicator",
    "security_reviewer", "final_reviewer", "merger", "recovery_planner", "verifier",
  ]);
  const localRoles = new Set(["coder", "fast_coder", "focused_coder", "local_coder", "tester", "qa_assistant", "ux_reviewer", "summarizer"]);
  return Object.fromEntries(ROUTE_DEFAULTS.map(({ role, model, effort }) => {
    if (preset === "balanced") return [role, { model, effort }];
    if (preset === "maximum_quality") return [role, { model: "codex/gpt-5.6-sol", effort: "high" }];
    if (preset === "local_first") return [role, { model: localRoles.has(role) ? "ollama/qualified-local" : model, effort: localRoles.has(role) ? "medium" : effort }];
    return [role, { model: expensiveRoles.has(role) ? "codex/gpt-5.6-sol" : "codex/gpt-5.3-codex-spark", effort: expensiveRoles.has(role) ? "medium" : "low" }];
  }));
}

function profileForEditing(profile: QAProfile): QAProfile {
  return {
    key: profile.key,
    title: profile.title,
    surfaces: [...(profile.surfaces ?? [])],
    checks: [...(profile.checks ?? [])],
    required_tools: [...(profile.required_tools ?? [])],
    reviewer_role: profile.reviewer_role ?? "qa_assistant",
    enabled: profile.enabled !== false,
  };
}

function repoActivity(repo: Repo): string {
  const runs = repo.workboard_status?.workflow_runs ?? [];
  return runs.at(-1)?.updated_at ?? repo.events?.[0]?.created_at ?? "";
}

function RepoSelector({ repos, selected, onSelect }: { repos: Repo[]; selected: string; onSelect: (name: string) => void }) {
  return <nav className="repo-switcher" aria-label="Registered repositories" role="tablist">
    <div className="repo-switcher-heading"><span>COURSE INDEX</span><strong>{repos.length}</strong></div>
    {repos.map((repo) => {
      const workboard = repo.workboard_status;
      const checks = checkStatus(repo.github_status);
      const loops = workboard?.total_loop_count ?? workboard?.loop_count ?? 0;
      return <button className={`repo-tab ${selected === repo.full_name ? "selected" : ""}`} type="button" role="tab" aria-selected={selected === repo.full_name} onClick={() => onSelect(repo.full_name)} key={repo.full_name}>
        <span className="repo-tab-title"><strong>{repo.full_name}</strong><em className={repo.state}>{statusLabel(repo.state)}</em></span>
        <span className="repo-tab-facts"><span><GitPullRequest size={13} aria-hidden="true" />{repo.github_status?.open_prs ?? 0} open</span><span><RotateCcw size={13} aria-hidden="true" />{loops} loops</span><span className={`check-${checks.replace(" ", "-")}`}>{checks}</span></span>
      </button>;
    })}
  </nav>;
}

function RepoPanel({ repo, onSave }: { repo: Repo; onSave: (name: string, payload: UpdatePayload) => Promise<void> }) {
  const [mode, setMode] = useState(repo.operation_mode);
  const [completion, setCompletion] = useState(repo.completion_policy);
  const [allowMerge, setAllowMerge] = useState(repo.allow_autonomous_merge ?? false);
  const [channel, setChannel] = useState(repo.notification_route ?? "");
  const [scheduleEnabled, setScheduleEnabled] = useState(repo.schedule_enabled !== false);
  const [qaProfiles, setQaProfiles] = useState<QAProfile[]>(() => (repo.qa_profiles ?? []).map(profileForEditing));
  const [routes, setRoutes] = useState<Record<string, EditableRoute>>(() => initialRoutes(repo));
  const [preset, setPreset] = useState<ModelPreset>("balanced");
  const [intelligence, setIntelligence] = useState<IntelligenceLevel>("balanced");
  const [surface, setSurface] = useState(repo.surfaces?.[0] ?? "custom");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    setMode(repo.operation_mode); setCompletion(repo.completion_policy); setAllowMerge(repo.allow_autonomous_merge ?? false);
    setChannel(repo.notification_route ?? ""); setScheduleEnabled(repo.schedule_enabled !== false); setQaProfiles((repo.qa_profiles ?? []).map(profileForEditing));
    setRoutes(initialRoutes(repo)); setPreset("balanced"); setIntelligence("balanced"); setSurface(repo.surfaces?.[0] ?? "custom");
  }, [repo]);
  const save = async () => {
    setSaving(true);
    try {
      const modelProfiles = modelProfilesForRoutes(routes);
      const validation = await callGateway<{ can_save?: boolean; warnings?: Array<{ warning?: string }> }>("models/validate", { full_name: repo.full_name, model_profiles: modelProfiles });
      if (validation.can_save === false) throw new Error("One or more model routes are invalid; correct them before saving.");
      await onSave(repo.full_name, {
        operation_mode: mode, completion_policy: completion, allow_autonomous_merge: allowMerge,
        notification_route: channel, surfaces: surface ? [surface] : [],
        schedule_enabled: scheduleEnabled,
        qa_profiles: qaProfiles,
        model_profiles: modelProfiles,
      });
      if (validation.warnings?.length) window.alert("Routes saved. Run a harness route test before autonomous use.");
    } finally { setSaving(false); }
  };
  const recordedTokens = repo.tokens.accounted_tokens ?? repo.tokens.total_tokens ?? 0;
  const usageSync = repo.workboard_status?.usage_sync;
  const usagePending = recordedTokens === 0 && usageSync?.status !== "ok" && (usageSync?.sessions_with_usage ?? 0) === 0;
  const usageLabel = usagePending ? "Usage not correlated" : `${recordedTokens.toLocaleString()} tokens recorded`;
  const github = repo.github_status;
  const workboard = repo.workboard_status;
  const trackedPrs = workboard?.pr_count ?? workboard?.pr_urls?.length ?? 0;
  const reviewCycles = workboard?.review_cycles ?? 0;
  const reviewStatus = statusLabel(workboard?.review_status);
  const testStatus = statusLabel(workboard?.test_status);
  const uxStage = workboard?.stage_history?.find((item) => item.stage === "ux_review");
  const needsUiAcceptance = repo.surfaces?.includes("web_ui") || Boolean(uxStage);
  const uiAcceptance = !needsUiAcceptance ? "n/a" : (uxStage?.blocked ?? 0) > 0 && (uxStage?.done ?? 0) === 0 ? "blocked" : (uxStage?.done ?? 0) > 0 ? "passed" : "required";
  const checks = checkStatus(github);
  const terminal = workboard?.terminal || workboard?.status === "completed" || repo.state === "merged";
  const blockers = workboard?.current_blockers ?? workboard?.blockers ?? (terminal ? 0 : workboard?.counts?.blocked ?? 0);
  const historicalBlockers = workboard?.historical_blockers ?? 0;
  const supersededRetries = workboard?.superseded_retries ?? 0;
  return <section className="repo-panel">
    <div className="repo-heading"><div><h3>{repo.full_name}</h3><p>{repo.local_path}</p></div><span className={`mode ${repo.operation_mode}`}>{repo.operation_mode}</span></div>
    <div className="repo-meta"><span>{repo.state.split("_").join(" ")}</span><span className={usagePending ? "usage-pending" : ""}>{usageLabel}</span><span>{repo.dirty ? "Uncommitted changes" : "Clean checkout"}</span></div>
    <div className="repo-stats" aria-label={`Repository facts for ${repo.full_name}`} tabIndex={0}>
      <Metric label="open PRs" value={github?.open_prs ?? "-"} />
      <Metric label="PRs in workflow" value={trackedPrs} />
      <Metric label="review cycles" value={reviewCycles} />
      <Metric label="reviews" value={reviewStatus} tone={reviewStatus === "blocked" ? "danger" : reviewStatus === "passed" ? "good" : "neutral"} />
      <Metric label="tests" value={testStatus} tone={testStatus === "blocked" ? "danger" : testStatus === "passed" ? "good" : "neutral"} />
      {needsUiAcceptance && <Metric label="UI acceptance" value={uiAcceptance} tone={uiAcceptance === "blocked" ? "danger" : uiAcceptance === "passed" ? "good" : "warn"} />}
      <Metric label="checks" value={checks} tone={checks === "failing" ? "danger" : checks === "passing" ? "good" : checks === "running" ? "warn" : "neutral"} />
      <Metric label="blockers" value={blockers} tone={blockers ? "danger" : "good"} />
    </div>
    {terminal && <p className="completion-note">Complete: merged and post-merge verified. {supersededRetries ? `${supersededRetries} superseded retry attempt${supersededRetries === 1 ? "" : "s"} retained as audit history. ` : ""}{historicalBlockers ? `${historicalBlockers} historical blocker${historicalBlockers === 1 ? "" : "s"} recorded during the run. ` : "No unresolved historical blockers recorded. "}Production deployment is not implied.</p>}
    <ExecutionPath repo={repo} />
    {github?.prs?.length ? <div className="pr-list"><h4>Open pull requests</h4>{github.prs.slice(0, 4).map((pr) => <a href={pr.url} target="_blank" rel="noreferrer" key={pr.number}><strong>#{pr.number}</strong><span>{pr.title ?? "Untitled PR"}</span><small>{pr.isDraft ? "draft" : pr.reviewDecision ?? pr.mergeStateStatus ?? "open"}</small></a>)}</div> : null}
    {repo.warnings[0] && <p className="warning">{repo.warnings[0]}</p>}
    <details className="settings"><summary>Repository controls</summary>
      <div className="settings-grid">
        <label>Autonomy<select value={mode} onChange={(event) => setMode(event.target.value)}><option value="disabled">Disabled</option><option value="advisory">Advisory</option><option value="supervised">Supervised</option><option value="autonomous">Autonomous</option></select></label>
        <label>Completion<select value={completion} onChange={(event) => setCompletion(event.target.value)}><option value="owner_approval">Owner approval</option><option value="control_plane_complete">Control plane complete</option><option value="auto_merge">Auto merge</option></select></label>
        <label>Discord route<input value={channel} onChange={(event) => setChannel(event.target.value)} placeholder="notifications or channel id" /></label>
        <label>Application surface<select value={surface} onChange={(event) => setSurface(event.target.value)}><option value="web_ui">Web UI</option><option value="cli">CLI</option><option value="api">API</option><option value="library">Library</option><option value="data_pipeline">Data pipeline</option><option value="infrastructure_release">Infrastructure/release</option><option value="custom">Custom</option></select></label>
        <label className="check-label"><input type="checkbox" checked={allowMerge} onChange={(event) => setAllowMerge(event.target.checked)} /> Allow autonomous merge</label>
        <label className="check-label"><input type="checkbox" checked={scheduleEnabled} onChange={(event) => setScheduleEnabled(event.target.checked)} /> Include in scheduled runs</label>
      </div>
      <div className="settings-grid"><label>Route preset<select aria-label="Route preset" value={preset} onChange={(event) => setPreset(event.target.value as ModelPreset)}>{Object.entries(MODEL_PRESET_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(presetRoutes(preset))}>Apply preset</button><label>Intelligence level<select aria-label="Intelligence level" value={intelligence} onChange={(event) => setIntelligence(event.target.value as IntelligenceLevel)}>{Object.entries(INTELLIGENCE_LEVEL_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(routesForIntelligence(intelligence, routes))}>Apply intelligence</button></div>
      <section className="detail-section" aria-labelledby={`qa-profiles-${repo.full_name}`}><div className="section-heading"><h4 id={`qa-profiles-${repo.full_name}`}>QA profiles</h4><button className="secondary compact" type="button" onClick={() => setQaProfiles([...qaProfiles, profileForEditing({ key: `qa-${qaProfiles.length + 1}`, title: "New QA profile", surfaces: [surface], checks: [], required_tools: [], reviewer_role: "qa_assistant", enabled: true })])}>Add QA profile</button></div>{qaProfiles.length ? <div className="package-list">{qaProfiles.map((profile, index) => <fieldset className="qa-profile" key={`${profile.key}-${index}`}><legend>{profile.title || profile.key}</legend><div className="settings-grid"><label>Profile key<input value={profile.key} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, key: event.target.value } : item))} /></label><label>Title<input value={profile.title} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, title: event.target.value } : item))} /></label><label>Surface<select value={profile.surfaces?.[0] ?? "custom"} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, surfaces: [event.target.value] } : item))}><option value="web_ui">Web UI</option><option value="cli">CLI</option><option value="api">API</option><option value="library">Library</option><option value="data_pipeline">Data pipeline</option><option value="custom">Custom</option></select></label><label>Reviewer role<input value={profile.reviewer_role ?? "qa_assistant"} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, reviewer_role: event.target.value } : item))} /></label><label className="wide">Checks<textarea value={(profile.checks ?? []).join("\n")} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, checks: splitLines(event.target.value) } : item))} placeholder="One deterministic check per line" /></label><label className="wide">Required tools<textarea value={(profile.required_tools ?? []).join("\n")} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, required_tools: splitLines(event.target.value) } : item))} placeholder="One tool per line" /></label><label className="check-label"><input type="checkbox" checked={profile.enabled !== false} onChange={(event) => setQaProfiles(qaProfiles.map((item, itemIndex) => itemIndex === index ? { ...item, enabled: event.target.checked } : item))} /> Enabled</label><button className="secondary compact" type="button" onClick={() => setQaProfiles(qaProfiles.filter((_item, itemIndex) => itemIndex !== index))}>Remove profile</button></div></fieldset>)}</div> : <p className="muted">No repository-specific QA profiles configured.</p>}</section>
      <div className="route-grid">{ROUTE_DEFAULTS.map(({ role, label }) => <fieldset key={role}><legend>{label}</legend><label>Model<input value={routes[role].model} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], model: event.target.value } })} /></label><label>Intelligence<select value={routes[role].effort} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], effort: event.target.value } })}><option>low</option><option>medium</option><option>high</option><option>xhigh</option></select></label></fieldset>)}</div>
      <button className="primary compact" onClick={save} disabled={saving}>{saving ? "Saving..." : "Save controls"}</button>
    </details>
  </section>;
}

function ModelPolicyPanel({ config, onSaved }: { config: ModelConfig; onSaved: () => void }) {
  const [scope, setScope] = useState<"global" | "runtime">("global");
  const [runtime, setRuntime] = useState(config.runtimes[0] ?? "");
  const [preset, setPreset] = useState<ModelPreset>("balanced");
  const [intelligence, setIntelligence] = useState<IntelligenceLevel>("balanced");
  const [routes, setRoutes] = useState<Record<string, EditableRoute>>(() => initialRoutesFromProfiles(config.global_profiles));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedProfiles = scope === "global" ? config.global_profiles : (config.runtime_profiles[runtime] ?? config.global_profiles);
  useEffect(() => {
    if (scope === "runtime" && !runtime && config.runtimes[0]) setRuntime(config.runtimes[0]);
    setRoutes(initialRoutesFromProfiles(selectedProfiles));
    setPreset("balanced"); setIntelligence("balanced");
    setError(null);
  }, [config, scope, runtime]);
  const save = async () => {
    if (scope === "runtime" && !runtime) return;
    setSaving(true); setError(null);
    try {
      const modelProfiles = modelProfilesForRoutes(routes);
      const validation = await callGateway<{ can_save?: boolean; warnings?: Array<{ warning?: string }> }>("models/validate", { model_profiles: modelProfiles });
      if (validation.can_save === false) throw new Error("One or more model routes are invalid; correct them before saving.");
      await callGateway("models/update", { scope, ...(scope === "runtime" ? { runtime } : {}), model_profiles: modelProfiles });
      onSaved();
      if (validation.warnings?.length) window.alert("Routes saved. Run a harness route test before autonomous use.");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  };
  return <section className="model-policy-panel" aria-labelledby="model-policy-title"><div className="section-heading"><div><p className="eyebrow">MODEL CONTROL</p><h2 id="model-policy-title">Global and runtime routes</h2></div></div>
    <p className="muted">Runtime routes override global routes. Repository, course, package, and stage routes can refine them further.</p>
    <details className="settings"><summary>Configure global/runtime routes</summary>
      <div className="settings-grid"><label>Configuration layer<select value={scope} onChange={(event) => setScope(event.target.value as "global" | "runtime")}><option value="global">Global defaults</option><option value="runtime">Runtime override</option></select></label>{scope === "runtime" && <label>Runtime<select value={runtime} onChange={(event) => setRuntime(event.target.value)} disabled={!config.runtimes.length}>{config.runtimes.map((item) => <option key={item} value={item}>{item}</option>)}</select></label>}</div>
      <div className="settings-grid"><label>Route preset<select aria-label="Route preset" value={preset} onChange={(event) => setPreset(event.target.value as ModelPreset)}>{Object.entries(MODEL_PRESET_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(presetRoutes(preset))}>Apply preset</button><label>Intelligence level<select aria-label="Intelligence level" value={intelligence} onChange={(event) => setIntelligence(event.target.value as IntelligenceLevel)}>{Object.entries(INTELLIGENCE_LEVEL_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(routesForIntelligence(intelligence, routes))}>Apply intelligence</button></div>
      <div className="route-grid">{ROUTE_DEFAULTS.map(({ role, label }) => <fieldset key={role}><legend>{label}</legend><label>Model<input value={routes[role].model} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], model: event.target.value } })} /></label><label>Intelligence<select value={routes[role].effort} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], effort: event.target.value } })}><option>low</option><option>medium</option><option>high</option><option>xhigh</option></select></label></fieldset>)}</div>
      <button className="primary compact" onClick={save} disabled={saving || (scope === "runtime" && !runtime)}>{saving ? "Saving..." : "Save global/runtime routes"}</button>{error && <p className="warning" role="alert">{error}</p>}
    </details>
  </section>;
}

function UsagePolicyPanel({ config, onSaved }: { config: ModelConfig; onSaved: () => void }) {
  const configured = config.usage ?? {};
  const [dailyLimit, setDailyLimit] = useState(configured.daily_token_limit?.toString() ?? "");
  const [blockOnUnknown, setBlockOnUnknown] = useState(configured.block_on_unknown !== false);
  const [limits, setLimits] = useState<Array<{ model: string; limit: string }>>(
    Object.entries(configured.model_daily_token_limits ?? {}).map(([model, limit]) => ({ model, limit: String(limit) })),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const next = config.usage ?? {};
    setDailyLimit(next.daily_token_limit?.toString() ?? "");
    setBlockOnUnknown(next.block_on_unknown !== false);
    setLimits(Object.entries(next.model_daily_token_limits ?? {}).map(([model, limit]) => ({ model, limit: String(limit) })));
  }, [config]);
  const save = async () => {
    setSaving(true); setError(null);
    try {
      const modelLimits: Record<string, number> = {};
      for (const item of limits) {
        const model = item.model.trim();
        if (!model && !item.limit.trim()) continue;
        const limit = Number(item.limit);
        if (!model || !Number.isInteger(limit) || limit < 0) throw new Error("Each model token limit needs a model name and a non-negative integer.");
        modelLimits[model] = limit;
      }
      const daily = dailyLimit.trim() ? Number(dailyLimit) : null;
      if (daily !== null && (!Number.isInteger(daily) || daily < 0)) throw new Error("Daily token limit must be a non-negative integer.");
      await callGateway("usage/update", { daily_token_limit: daily, model_daily_token_limits: modelLimits, block_on_unknown: blockOnUnknown });
      onSaved();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  };
  return <section className="model-policy-panel" aria-labelledby="usage-policy-title"><div className="section-heading"><div><p className="eyebrow">TOKEN CONTROL</p><h2 id="usage-policy-title">Token safeguards</h2></div></div><p className="muted">Limits use provider-reported tokens only. Unknown telemetry remains unknown and can block autonomous work.</p><div className="settings-grid"><label>Daily token limit<input inputMode="numeric" value={dailyLimit} onChange={(event) => setDailyLimit(event.target.value)} placeholder="No limit" /></label><label className="check-label"><input type="checkbox" checked={blockOnUnknown} onChange={(event) => setBlockOnUnknown(event.target.checked)} /> Block when telemetry is unknown</label></div><div className="package-list">{limits.map((item, index) => <fieldset className="qa-profile" key={`${item.model}-${index}`}><legend>Model limit</legend><div className="settings-grid"><label>Model<input value={item.model} onChange={(event) => setLimits(limits.map((row, rowIndex) => rowIndex === index ? { ...row, model: event.target.value } : row))} placeholder="codex/gpt-5.3-codex-spark" /></label><label>Daily tokens<input inputMode="numeric" value={item.limit} onChange={(event) => setLimits(limits.map((row, rowIndex) => rowIndex === index ? { ...row, limit: event.target.value } : row))} /></label><button className="secondary compact" type="button" onClick={() => setLimits(limits.filter((_row, rowIndex) => rowIndex !== index))}>Remove limit</button></div></fieldset>)}</div><div className="action-row"><button className="secondary compact" type="button" onClick={() => setLimits([...limits, { model: "", limit: "" }])}>Add model limit</button><button className="primary compact" type="button" onClick={save} disabled={saving}>{saving ? "Saving..." : "Save token safeguards"}</button></div>{error && <p className="warning" role="alert">{error}</p>}</section>;
}

function RegisterPanel({ onRegistered }: { onRegistered: () => void }) {
  const [open, setOpen] = useState(false); const [fullName, setFullName] = useState(""); const [channel, setChannel] = useState("notifications"); const [saving, setSaving] = useState(false); const [error, setError] = useState<string | null>(null); const [followUp, setFollowUp] = useState<string | null>(null);
  const register = async () => { if (saving) return; setSaving(true); setError(null); setFollowUp(null); try { const result = await callGateway<RegistrationResult>("repos/register", { full_name: normalizeGithubRepository(fullName), notification_route: channel.trim() || "notifications" }); setFullName(""); setOpen(false); setFollowUp(result.follow_up_message ?? "Repository registered. Number 1 will follow up in chat before work begins."); onRegistered(); } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); } finally { setSaving(false); } };
  const submit = (event: FormEvent) => { event.preventDefault(); void register(); };
  return <section className="register-panel" aria-labelledby="register-title"><div className="section-heading"><div><p className="eyebrow">REPOSITORY REGISTRY</p><h3 id="register-title">Add a repository</h3></div><button className="secondary" onClick={() => setOpen(!open)}>{open ? "Close" : "Register repository"}</button></div>
    <p className="muted">Make It So will locate the local clone and planning document. Number 1 will follow up in chat for anything it cannot verify.</p>
    {open && <form className="register-form registration-form" onSubmit={submit}><label>GitHub repository<input required value={fullName} onChange={(event) => setFullName(event.target.value)} placeholder="owner/repository or GitHub URL" /></label><label>Discord route<input required value={channel} onChange={(event) => setChannel(event.target.value)} placeholder="notifications or channel ID" /></label><button className="primary" type="button" onClick={() => void register()} disabled={saving}>{saving ? "Registering and inspecting..." : "Register and inspect"}</button>{error && <p className="warning" role="alert">{error}</p>}</form>}
    {followUp && <p className="inline-status" role="status">{followUp}</p>}
  </section>;
}

function GreenfieldPanel({ onCreated }: { onCreated: () => void }) {
  const [open, setOpen] = useState(false);
  const [fullName, setFullName] = useState("");
  const [localPath, setLocalPath] = useState("");
  const [key, setKey] = useState("first-course");
  const [title, setTitle] = useState("");
  const [goal, setGoal] = useState("");
  const [description, setDescription] = useState("");
  const [visibility, setVisibility] = useState("private");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError(null);
    try {
      await callGateway("repos/create", {
        full_name: fullName,
        local_path: localPath,
        description,
        visibility,
        course: {
          key,
          title,
          kind: "greenfield",
          goal,
          status: "readiness_review",
          readiness: [
            { key: "users", category: "product", question: "Who is this for and what must they accomplish?", required: true, owner_decision_required: true },
            { key: "success", category: "goal", question: "What observable outcome defines success?", required: true, owner_decision_required: true },
            { key: "access", category: "operations", question: "What permissions, secrets, environments, or test data are required?", required: true, owner_decision_required: true },
          ],
          work_packages: [{ key: "discovery", title: "Discovery", objective: "Establish the verified course charter and implementation foundation.", status: "planned" }],
          checkpoints: [],
        },
      });
      setOpen(false); onCreated();
    } catch (reason) { setError(String(reason)); } finally { setSaving(false); }
  };
  return <section className="register-panel greenfield-panel" aria-labelledby="greenfield-title">
    <div className="section-heading"><div><p className="eyebrow">GREENFIELD BRIDGE</p><h2 id="greenfield-title">Create from the Chair</h2></div><button className="secondary" onClick={() => setOpen(!open)}>{open ? "Close" : "New greenfield repo"}</button></div>
    <p className="muted">The GitHub repository is created only after the course passes readiness and you explicitly engage it.</p>
    {open && <form className="register-form course-form" onSubmit={submit}>
      <label>GitHub repository<input required pattern="[^/\s]+/[^/\s]+" value={fullName} onChange={(event) => setFullName(event.target.value)} placeholder="owner/repository" /></label>
      <label>Local path<input required value={localPath} onChange={(event) => setLocalPath(event.target.value)} placeholder="/workspace/repository" /></label>
      <label>Course key<input required pattern="[A-Za-z0-9][A-Za-z0-9._-]*" value={key} onChange={(event) => setKey(event.target.value)} /></label>
      <label>Course title<input required value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <label>Visibility<select value={visibility} onChange={(event) => setVisibility(event.target.value)}><option value="private">Private</option><option value="public">Public</option></select></label>
      <label>Repository description<input value={description} onChange={(event) => setDescription(event.target.value)} /></label>
      <label className="wide">Goal<textarea required minLength={10} value={goal} onChange={(event) => setGoal(event.target.value)} /></label>
      <button className="primary" type="submit" disabled={saving}>{saving ? "Preparing course..." : "Create readiness review"}</button>
      {error && <p className="warning" role="alert">{error}</p>}
    </form>}
  </section>;
}

function splitLines(value: string): string[] { return value.split(/[\n,]/).map((item) => item.trim()).filter(Boolean); }

function CourseCreatePanel({ repos, onCreated }: { repos: Repo[]; onCreated: () => void }) {
  const [open, setOpen] = useState(false); const [repository, setRepository] = useState(repos[0]?.full_name ?? ""); const [key, setKey] = useState("new-course"); const [title, setTitle] = useState(""); const [kind, setKind] = useState("feature"); const [goal, setGoal] = useState("");
  const [users, setUsers] = useState(""); const [scope, setScope] = useState(""); const [nonGoals, setNonGoals] = useState(""); const [acceptance, setAcceptance] = useState(""); const [exitCriteria, setExitCriteria] = useState(""); const [questions, setQuestions] = useState("What does success look like?\nWhat permissions, environments, or test data are required?"); const [saving, setSaving] = useState(false); const [error, setError] = useState<string | null>(null);
  const [packages, setPackages] = useState("discovery | Establish a verified understanding of the repository | Read the current implementation, docs, checks, and GitHub state.");
  useEffect(() => { if (!repository && repos[0]) setRepository(repos[0].full_name); }, [repos, repository]);
  const submit = async (event: FormEvent) => { event.preventDefault(); setSaving(true); setError(null); try {
    const readiness = splitLines(questions).map((question, index) => ({ key: `readiness-${index + 1}`, category: "planning", question, required: true, owner_decision_required: true }));
    const workPackages = splitLines(packages).map((line, index) => {
      const [packageKey, packageTitle, objective] = line.split("|").map((item) => item.trim());
      return { key: packageKey || `package-${index + 1}`, title: packageTitle || `Work package ${index + 1}`, objective: objective || packageTitle || line, status: "planned", acceptance_criteria: splitLines(acceptance) };
    });
    await callGateway("course/create", { full_name: repository, course: { key, title, kind, goal, users: splitLines(users), scope: splitLines(scope), non_goals: splitLines(nonGoals), acceptance_criteria: splitLines(acceptance), exit_criteria: splitLines(exitCriteria), readiness, status: "readiness_review", work_packages: workPackages, checkpoints: [] } });
    setOpen(false); onCreated();
  } catch (reason) { setError(String(reason)); } finally { setSaving(false); } };
  return <section className="course-builder"><div className="section-heading"><div><p className="eyebrow">COURSE CHARTER</p><h2>Start a planning session</h2></div><button className="secondary" onClick={() => setOpen(!open)}>{open ? "Close" : "New course"}</button></div>
     {open && <form className="register-form course-form" onSubmit={submit}><label>Repository<select required value={repository} onChange={(event) => setRepository(event.target.value)}>{repos.map((repo) => <option key={repo.full_name}>{repo.full_name}</option>)}</select></label><label>Course key<input required pattern="[A-Za-z0-9][A-Za-z0-9._-]*" value={key} onChange={(event) => setKey(event.target.value)} /></label><label>Title<input required value={title} onChange={(event) => setTitle(event.target.value)} /></label><label>Mode<select value={kind} onChange={(event) => setKind(event.target.value)}><option value="greenfield">Greenfield</option><option value="takeover">In-progress takeover</option><option value="feature">Shipped-product feature</option></select></label><label className="wide">Goal<textarea required minLength={10} value={goal} onChange={(event) => setGoal(event.target.value)} /></label><label>Users<textarea value={users} onChange={(event) => setUsers(event.target.value)} /></label><label>Scope<textarea value={scope} onChange={(event) => setScope(event.target.value)} /></label><label>Non-goals<textarea value={nonGoals} onChange={(event) => setNonGoals(event.target.value)} /></label><label>Acceptance criteria<textarea value={acceptance} onChange={(event) => setAcceptance(event.target.value)} /></label><label>Exit criteria<textarea value={exitCriteria} onChange={(event) => setExitCriteria(event.target.value)} /></label><label className="wide">Initial work packages<textarea value={packages} onChange={(event) => setPackages(event.target.value)} /></label><label className="wide">Readiness questions<textarea value={questions} onChange={(event) => setQuestions(event.target.value)} /></label><button className="primary" type="submit" disabled={saving}>{saving ? "Creating..." : "Create readiness review"}</button>{error && <p className="warning" role="alert">{error}</p>}</form>}
  </section>;
}

function CourseModelSettings({ repository, repo, course, onSaved }: { repository: string; repo?: Repo; course: Course; onSaved: () => void }) {
  const [layer, setLayer] = useState<"course" | "work_package" | "stage">("course");
  const [stageScope, setStageScope] = useState<"course" | "work_package">("course");
  const [stageName, setStageName] = useState("implementation");
  const [packageKey, setPackageKey] = useState(course.work_packages[0]?.key ?? "");
  const [preset, setPreset] = useState<ModelPreset>("balanced");
  const [intelligence, setIntelligence] = useState<IntelligenceLevel>("balanced");
  const [routes, setRoutes] = useState<Record<string, EditableRoute>>(() => initialRoutesFromProfiles(course.model_profiles));
  const [stageRoute, setStageRoute] = useState<EditableRoute>(() => initialStageRoute(course.model_profiles?.["stage:implementation"], stageName));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const selectedPackage = course.work_packages.find((item) => item.key === packageKey);
  const selectedProfiles = layer === "course" ? course.model_profiles : selectedPackage?.model_profiles;
  const stageProfiles = stageScope === "course" ? course.model_profiles : selectedPackage?.model_profiles;
  useEffect(() => {
    if (layer === "stage") setStageRoute(initialStageRoute(stageProfiles?.[`stage:${stageName}`], stageName));
    else setRoutes(initialRoutesFromProfiles(selectedProfiles));
    setPreset("balanced"); setIntelligence("balanced");
    setError(null);
  }, [course, layer, packageKey, stageName, stageScope]);
  const save = async () => {
    if ((layer === "work_package" || (layer === "stage" && stageScope === "work_package")) && !selectedPackage) return;
    setSaving(true); setError(null);
    try {
      const stageKey = `stage:${stageName}`;
      const modelProfiles = layer === "stage"
        ? { [stageKey]: { primary: { model: stageRoute.model, thinking: stageRoute.effort } } }
        : modelProfilesForRoutes(routes);
      const validation = await callGateway<{ can_save?: boolean; warnings?: Array<{ warning?: string }> }>("models/validate", { full_name: repository, model_profiles: modelProfiles });
      if (validation.can_save === false) throw new Error("One or more model routes are invalid; correct them before saving.");
      await callGateway("course/models", {
        full_name: repository,
        course_key: course.key,
        layer: layer === "stage" ? "stage" : layer,
        ...(layer === "stage" ? { stage_name: stageName, stage_scope: stageScope, stage_profile: modelProfiles[stageKey] } : {}),
        ...(layer === "work_package" || (layer === "stage" && stageScope === "work_package") ? { work_package_key: packageKey } : {}),
        ...(layer === "stage" ? {} : { model_profiles: modelProfiles }),
      });
      onSaved();
      if (validation.warnings?.length) window.alert("Routes saved. Run a harness route test before autonomous use.");
    } catch (reason) {
      setError(String(reason));
    } finally {
      setSaving(false);
    }
  };
  const previewPackage = selectedPackage ?? course.work_packages[0];
  return <details className="settings"><summary>Course and package model routes</summary>
    <p className="muted">Stage routes override package routes, which override course routes, repository routes, and runtime defaults.</p>
    <div className="settings-grid">
      <label>Override layer<select value={layer} onChange={(event) => setLayer(event.target.value as "course" | "work_package" | "stage")}><option value="course">Course</option><option value="work_package">Work package</option><option value="stage">Workflow stage</option></select></label>
      {layer === "stage" && <><label>Stage scope<select value={stageScope} onChange={(event) => setStageScope(event.target.value as "course" | "work_package")}><option value="course">Course stage</option><option value="work_package">Work package stage</option></select></label><label>Stage name<input value={stageName} onChange={(event) => setStageName(event.target.value)} pattern="[A-Za-z0-9._-]+" /></label></>}
      {(layer === "work_package" || (layer === "stage" && stageScope === "work_package")) && <label>Work package<select value={packageKey} onChange={(event) => setPackageKey(event.target.value)} disabled={!course.work_packages.length}>{course.work_packages.map((item) => <option key={item.key} value={item.key}>{item.key}</option>)}</select></label>}
    </div>
    {layer !== "stage" && <div className="settings-grid"><label>Route preset<select aria-label="Course route preset" value={preset} onChange={(event) => setPreset(event.target.value as ModelPreset)}>{Object.entries(MODEL_PRESET_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(presetRoutes(preset))}>Apply course preset</button><label>Intelligence level<select aria-label="Course intelligence level" value={intelligence} onChange={(event) => setIntelligence(event.target.value as IntelligenceLevel)}>{Object.entries(INTELLIGENCE_LEVEL_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label><button className="secondary compact" type="button" onClick={() => setRoutes(routesForIntelligence(intelligence, routes))}>Apply course intelligence</button></div>}
    {layer === "stage" ? <fieldset className="stage-route"><legend>{stageName || "Workflow stage"} route</legend><label>Model<input value={stageRoute.model} onChange={(event) => setStageRoute({ ...stageRoute, model: event.target.value })} /></label><label>Intelligence<select value={stageRoute.effort} onChange={(event) => setStageRoute({ ...stageRoute, effort: event.target.value })}><option>low</option><option>medium</option><option>high</option><option>xhigh</option></select></label></fieldset> : <div className="route-grid">{ROUTE_DEFAULTS.map(({ role, label }) => <fieldset key={role}><legend>{label}</legend><label>Model<input value={routes[role].model} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], model: event.target.value } })} /></label><label>Intelligence<select value={routes[role].effort} onChange={(event) => setRoutes({ ...routes, [role]: { ...routes[role], effort: event.target.value } })}><option>low</option><option>medium</option><option>high</option><option>xhigh</option></select></label></fieldset>)}</div>}
    <section className="route-preview" aria-labelledby={`route-preview-${course.key}`}><h3 id={`route-preview-${course.key}`}>Effective route preview</h3>{ROUTE_DEFAULTS.map(({ role, label, model, effort }) => { const route = effectiveRoute(repo, course, previewPackage, role, model, effort, stageName); return <div key={role}><span>{label}</span><strong>{route.model}</strong><small>{route.effort} / {route.source}</small></div>; })}</section>
    <button className="primary compact" onClick={save} disabled={saving || ((layer === "work_package" || (layer === "stage" && stageScope === "work_package")) && !selectedPackage)}>{saving ? "Saving..." : "Save model routes"}</button>{error && <p className="warning" role="alert">{error}</p>}
  </details>;
}

function SchedulePanel({ status, onRefresh }: { status: ScheduleStatus | null; onRefresh: () => void }) {
  const [reconcileEvery, setReconcileEvery] = useState(status?.jobs.find((job) => job.name === "make-it-so-reconcile")?.every ?? "5m");
  const [reviewEvery, setReviewEvery] = useState(status?.jobs.find((job) => job.name === "make-it-so-course-review")?.every ?? "2h");
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    setReconcileEvery(status?.jobs.find((job) => job.name === "make-it-so-reconcile")?.every ?? "5m");
    setReviewEvery(status?.jobs.find((job) => job.name === "make-it-so-course-review")?.every ?? "2h");
  }, [status]);
  const action = async (path: string, params: Record<string, unknown> = {}) => {
    setMessage(null);
    try {
      const result = await callGateway<{ status?: string }>(`schedule/${path}`, params);
      setMessage(`Schedule ${result.status ?? path}.`);
      onRefresh();
    } catch (reason) { setMessage(String(reason)); }
  };
  return <section className="schedule-section" aria-labelledby="schedule-title"><div className="section-heading"><div><p className="eyebrow">AUTOMATION</p><h2 id="schedule-title">Managed schedules</h2></div><button className="secondary compact" onClick={() => action("install")}>Reconcile</button></div>
    <div className="schedule-grid">{status?.jobs?.map((job) => <div className="schedule-row" key={job.name}><div><strong>{job.name}</strong><span>Every {job.every}</span></div><span className={`health ${job.health}`}>{job.health}</span><div className="action-row"><button className="secondary compact" onClick={() => action(job.enabled ? "pause" : "resume", { name: job.name })}>{job.enabled ? "Pause" : "Resume"}</button><button className="secondary compact danger" onClick={() => action("remove", { name: job.name })}>Remove</button></div></div>) ?? <p className="muted">Schedule state is unavailable.</p>}</div>
    <div className="settings-grid schedule-editor"><label>Reconcile cadence<input aria-label="Reconcile cadence" value={reconcileEvery} onChange={(event) => setReconcileEvery(event.target.value)} pattern="[1-9][0-9]*(s|m|h|d)" /></label><label>Course review cadence<input aria-label="Course review cadence" value={reviewEvery} onChange={(event) => setReviewEvery(event.target.value)} pattern="[1-9][0-9]*(s|m|h|d)" /></label><button className="primary compact" onClick={() => action("edit", { reconcile_every: reconcileEvery, review_every: reviewEvery })}>Save cadence</button></div>
    {message && <p className="inline-status" role="status">{message}</p>}
  </section>;
}

function milestoneStatusTone(status?: string): string {
  if (status === "passed") return "good";
  if (status === "not_run" || status === "optional") return "neutral";
  return "danger";
}

function artifactLabel(artifact: EvidenceArtifact): string {
  return artifact.title || artifact.viewport || artifact.kind || artifact.url || artifact.path || "Evidence artifact";
}

function MilestoneEvidencePanel({ item, repo }: { item: CourseSummary; repo?: Repo }) {
  const initialRows = repo?.workboard_status?.milestones?.filter((row) => row.course_key === item.course.key) ?? [];
  const [loaded, setLoaded] = useState<Record<string, MilestoneEvidence>>({});
  const [loadingKey, setLoadingKey] = useState<string | null>(null);
  const rows = item.course.work_packages.map((pkg) => loaded[pkg.key] ?? initialRows.find((row) => row.work_package_key === pkg.key) ?? {
    course_key: item.course.key,
    work_package_key: pkg.key,
    title: pkg.title,
    objective: pkg.objective,
    status: pkg.status,
    policy: pkg.test_evidence_policy,
    evidence: { status: "not_run", reason: "No Workboard test evidence recorded yet", screenshots: [], artifacts: [], commands: [] },
  });
  const loadDetails = async (packageKey: string) => {
    if (!repo || loaded[packageKey] || loadingKey === packageKey) return;
    setLoadingKey(packageKey);
    try {
      const result = await callGateway<{ milestones?: MilestoneEvidence[] }>("course/milestone-evidence", { full_name: item.repository, course_key: item.course.key, work_package_key: packageKey });
      const row = result.milestones?.[0];
      if (row) setLoaded((current) => ({ ...current, [packageKey]: row }));
    } finally {
      setLoadingKey(null);
    }
  };
  const passed = rows.filter((row) => row.evidence?.status === "passed").length;
  const screenshots = rows.reduce((total, row) => total + (row.evidence?.screenshots?.length ?? 0), 0);
  return <section className="detail-section milestone-evidence" aria-labelledby={`milestone-evidence-${item.course.key}`}>
    <div className="detail-heading"><div><p className="eyebrow">PROOF OF DONE</p><h3 id={`milestone-evidence-${item.course.key}`}>Milestone test evidence</h3></div><span className="evidence-summary">{passed}/{rows.length} passing · {screenshots} screenshot{screenshots === 1 ? "" : "s"}</span></div>
    <p className="muted">Each milestone carries its own pass-rate, current-head, command, and artifact contract. Expand a row to inspect the proof.</p>
    {rows.length ? <div className="milestone-list">{rows.map((row) => {
      const evidence = row.evidence ?? { status: "not_run", reason: "No evidence recorded", screenshots: [], artifacts: [], commands: [] };
      const policy = row.policy ?? {};
      const artifacts = [...(evidence.screenshots ?? []), ...(evidence.artifacts ?? []).filter((artifact) => !(evidence.screenshots ?? []).some((shot) => shot.url && shot.url === artifact.url))];
      const rate = typeof evidence.pass_rate === "number" ? `${evidence.pass_rate}%` : "--";
      return <details className={`milestone-row ${milestoneStatusTone(evidence.status)}`} key={row.work_package_key} onToggle={(event) => { if (event.currentTarget.open) void loadDetails(row.work_package_key); }}>
        <summary><span className="milestone-title"><strong>{row.title}</strong><small>{row.work_package_key} · {row.status}</small></span><span className="milestone-stats"><em>{statusLabel(evidence.status)}</em><b>{rate}</b><small>{evidence.screenshots?.length ?? 0} shots</small></span></summary>
        <div className="milestone-body"><p>{row.objective}</p>{loadingKey === row.work_package_key && <p className="muted">Loading latest evidence...</p>}<div className="evidence-facts"><span><b>{evidence.tests_passed ?? 0}</b> passed</span><span><b>{evidence.tests_failed ?? 0}</b> failed</span><span><b>{evidence.tests_skipped ?? 0}</b> skipped</span><span><b>{evidence.tests_total ?? 0}</b> total</span><span><b>{policy.minimum_pass_rate ?? 100}%</b> required</span></div>{evidence.reason && <p className={`evidence-reason ${milestoneStatusTone(evidence.status)}`}>{evidence.reason}</p>}{evidence.commands?.length ? <div className="evidence-block"><strong>Commands</strong>{evidence.commands.map((command) => <code key={command}>{command}</code>)}</div> : null}<dl className="evidence-meta">{evidence.head_sha && <><dt>Evidence head</dt><dd><code>{evidence.head_sha}</code></dd></>}{evidence.current_head_sha && <><dt>Current head</dt><dd><code>{evidence.current_head_sha}</code></dd></>}{evidence.model && <><dt>Model</dt><dd>{shortModel(evidence.model)}{evidence.provider ? ` via ${evidence.provider}` : ""}</dd></>}</dl>{artifacts.length ? <div className="evidence-artifacts"><strong>Artifacts</strong><div>{artifacts.map((artifact, index) => { const href = artifact.url || artifact.path; const image = Boolean(artifact.mime_type?.startsWith("image/") || (artifact.url && /\.(png|jpe?g|webp|gif)$/i.test(artifact.url))); return <div className="evidence-artifact" key={`${href ?? artifactLabel(artifact)}-${index}`}>{image && artifact.url?.startsWith("http") && <img src={artifact.url} alt={artifact.description || artifactLabel(artifact)} loading="lazy" />}{href?.startsWith("http") ? <a href={href} target="_blank" rel="noreferrer">{artifactLabel(artifact)} <ExternalLink size={12} aria-hidden="true" /></a> : <span>{artifactLabel(artifact)}{href ? <code>{href}</code> : null}</span>}</div>; })}</div></div> : <p className="muted">No screenshot or artifact links were recorded.</p>}{row.pr_url && <a className="evidence-pr" href={row.pr_url} target="_blank" rel="noreferrer"><GitPullRequest size={14} aria-hidden="true" /> Open linked PR <ExternalLink size={12} aria-hidden="true" /></a>}</div>
      </details>;
    })}</div> : <p className="muted">No delivery milestones are defined for this course.</p>}
  </section>;
}

function MilestoneGovernancePanel({ item, onAction }: { item: CourseSummary; onAction: (path: string, params: Record<string, unknown>) => Promise<void> }) {
  const proposals = item.milestone_changes ?? [];
  const pending = proposals.filter((proposal) => proposal.status === "proposed");
  const latestReview = item.milestone_reviews?.[0];
  return <section className="detail-section milestone-governance" aria-labelledby={`milestone-governance-${item.course.key}`}>
    <div className="detail-heading"><div><p className="eyebrow">NUMBER 1</p><h3 id={`milestone-governance-${item.course.key}`}>Course corrections</h3></div><span className="evidence-summary">Revision {item.course.plan_revision ?? 1} · {pending.length} awaiting decision</span></div>
    <p className="muted">Number 1 owns the course direction. Supervised mode pauses here before a milestone graph change is applied.</p>
    {item.number_one && <div className="number-one-context"><strong>Leadership session</strong><span>{item.number_one.model ?? "configured leadership route"} · {item.number_one.last_review_at ? new Date(item.number_one.last_review_at).toLocaleString() : "not reviewed yet"}</span>{item.number_one.summary && <small>{item.number_one.summary}</small>}</div>}
    {latestReview && <div className={`number-one-review ${latestReview.status ?? "on_track"}`}><strong>Latest course review: {(latestReview.status ?? "on track").split("_").join(" ")}</strong><span>{latestReview.summary}</span><small>Next: {latestReview.next_action}</small></div>}
    {pending.length ? <div className="proposal-list">{pending.map((proposal) => <div className="proposal-row" key={proposal.proposal_id}><div><strong>{proposal.summary}</strong><span>{proposal.reason}</span><small>{proposal.impact ?? "routine"} · base revision {proposal.base_revision ?? "?"} · {proposal.changes?.map((change) => `${change.kind ?? "change"} ${change.work_package_key ?? change.work_package?.key ?? "milestone"}`).join(", ")}</small></div><div className="action-row"><button className="primary compact" onClick={() => onAction("course/milestone-change-approve", { full_name: item.repository, course_key: item.course.key, proposal_id: proposal.proposal_id, approved_by: "owner" })}>Approve</button><button className="secondary compact danger" onClick={() => onAction("course/milestone-change-reject", { full_name: item.repository, course_key: item.course.key, proposal_id: proposal.proposal_id })}>Reject</button></div></div>)}</div> : <p className="muted">No milestone changes are awaiting a decision.</p>}
  </section>;
}

function CoursePanel({ item, repo, onAction, onRefresh }: { item: CourseSummary; repo?: Repo; onAction: (path: string, params: Record<string, unknown>) => Promise<void>; onRefresh: () => void }) {
  const [open, setOpen] = useState(false); const [actor, setActor] = useState("owner"); const [answers, setAnswers] = useState<Record<string, string>>({}); const [planning, setPlanning] = useState<PlanningSession | null>(null); const [planningLoading, setPlanningLoading] = useState(false);
  const { course, repository, readiness } = item;
  const params = { full_name: repository, course_key: course.key };
  const openPlanning = async () => { setPlanningLoading(true); try { setPlanning(await callGateway<PlanningSession>("course/planning-session", params)); } finally { setPlanningLoading(false); } };
  return <article className="course-card"><div className="course-heading"><div><strong>{course.title}</strong><span>{repository} / {course.kind} / {course.status}</span></div><span className={`readiness ${readiness.ready ? "ready" : "waiting"}`}>{readiness.ready ? "Ready for approval" : `${readiness.unresolved?.length ?? 0} readiness items`}</span><button className="icon-button" aria-label={`${open ? "Collapse" : "Expand"} ${course.title}`} onClick={() => setOpen(!open)}>{open ? "-" : "+"}</button></div>
    {open && <div className="course-detail"><p className="course-goal">{course.goal}</p><div className="course-actions"><label>Decision owner<input value={actor} onChange={(event) => setActor(event.target.value)} /></label><button className="secondary" onClick={openPlanning} disabled={planningLoading}>{planningLoading ? "Preparing..." : "Open planning brief"}</button>{readiness.ready && course.status !== "engaged" && course.status !== "paused" && <button className="primary" onClick={() => onAction("course/approve", { ...params, approved_by: actor })}>Engage course</button>}{course.status === "engaged" && <button className="secondary" onClick={() => onAction("course/pause", params)}>Pause</button>}{course.status === "paused" && <button className="primary" onClick={() => onAction("course/resume", params)}>Resume</button>}</div>
      {planning && <section className="detail-section planning-brief"><h3>Plan and charter review</h3><p>{planning.prompt}</p><dl className="plan-diff"><div><dt>Goal</dt><dd>{course.goal}</dd></div><div><dt>Readiness</dt><dd>{readiness.unresolved?.length ?? 0} unresolved, {readiness.verified?.length ?? 0} verified</dd></div><div><dt>Delivery plan</dt><dd>{course.work_packages.length} work packages, {course.checkpoints.length} checkpoints</dd></div></dl>{planning.next_questions.length ? <><strong>Next questions</strong><ul>{planning.next_questions.map((question) => <li key={question}>{question}</li>)}</ul></> : <p className="muted">The charter is ready for owner review. Approval is still required before mutation.</p>}</section>}
      <section className="detail-section"><h3>Readiness</h3>{course.readiness.length ? course.readiness.map((requirement) => <div className="requirement" key={requirement.key}><div><strong>{requirement.key}</strong><span>{requirement.question}</span></div><span className="status-text">{requirement.status}</span>{requirement.status !== "verified" && <><textarea aria-label={`Answer ${requirement.key}`} value={answers[requirement.key] ?? requirement.answer ?? ""} onChange={(event) => setAnswers({ ...answers, [requirement.key]: event.target.value })} placeholder="Answer or evidence" /><button className="secondary compact" onClick={() => onAction("course/requirement", { ...params, requirement_key: requirement.key, status: "answered", answer: answers[requirement.key] ?? requirement.answer ?? "", evidence: ["owner-dashboard-answer"] })}>Submit answer</button></>}</div>) : <p className="muted">No readiness questions recorded.</p>}</section>
      <section className="detail-section"><h3>Work-package dependency map</h3>{course.work_packages.length ? <div className="package-list">{course.work_packages.map((pkg) => <div key={pkg.key}><strong>{pkg.key}</strong><span>{pkg.title}{pkg.dependencies?.length ? ` | after ${pkg.dependencies.join(", ")}` : " | ready when engaged"}</span><em>{pkg.status}</em></div>)}</div> : <p className="muted">Number 1 will decompose work after course approval.</p>}</section>
      <MilestoneGovernancePanel item={item} onAction={onAction} />
      <MilestoneEvidencePanel item={item} repo={repo} />
      <section className="detail-section"><h3>Checkpoints</h3>{course.checkpoints.length ? course.checkpoints.map((checkpoint) => <div className="checkpoint" key={checkpoint.key}><div><strong>{checkpoint.title}</strong><span>{checkpoint.reason}</span></div><span className="status-text">{checkpoint.status}</span>{checkpoint.status === "pending" && <button className="secondary compact" onClick={() => onAction("course/checkpoint", { ...params, checkpoint_key: checkpoint.key, status: "resolved", resolved_by: actor, evidence: ["dashboard"] })}>Resolve</button>}</div>) : <p className="muted">No checkpoints are currently defined.</p>}</section>
      <CourseModelSettings repository={repository} repo={repo} course={course} onSaved={onRefresh} />
    </div>}
  </article>;
}

const ATTENTION_TYPES = new Set(["ATTENTION_REQUIRED", "COMPLETION_READY", "REVIEW_BLOCKED", "FINAL_REVIEW_BLOCKED", "PR_CHECKS_WAITING", "STALLED", "QUEUE_DEGRADED"]);
function evidenceLink(evidence: Record<string, unknown>): string | null {
  const value = evidence.pr_url ?? evidence.html_url ?? evidence.github_url ?? evidence.url;
  return typeof value === "string" && value.startsWith("https://github.com/") ? value : null;
}
function ActivityPanel({ repos, onRefresh }: { repos: Repo[]; onRefresh: () => void }) {
  const events = useMemo(() => repos.flatMap((repo) => (repo.events ?? []).map((event) => ({ ...event, repo: repo.full_name }))).sort((a, b) => b.created_at.localeCompare(a.created_at)), [repos]);
  const attention = events.filter((event) => ATTENTION_TYPES.has(event.event_type)).slice(0, 8);
  const crew = events.filter((event) => !ATTENTION_TYPES.has(event.event_type)).slice(0, 8);
  const models = repos.flatMap((repo) => repo.usage_detail?.model_totals ?? []).reduce<Record<string, number>>((totals, item) => { const model = item.model ?? "unknown"; totals[model] = (totals[model] ?? 0) + (item.accounted_tokens ?? 0); return totals; }, {});
  const acknowledge = async (event: typeof attention[number]) => {
    const fingerprint = String(event.evidence.fingerprint ?? "");
    if (!fingerprint) return;
    await callGateway("attention/ack", { full_name: event.repo, fingerprint, event_type: event.event_type });
    onRefresh();
  };
  return <section className="activity-section">
    <div className="section-heading"><div><p className="eyebrow">SHIP STATUS</p><h2>Attention and crew activity</h2></div></div>
    <div className="activity-grid">
      <div className="activity-panel"><h3>Attention queue</h3>{attention.length ? attention.map((event) => <div className="event-row attention" key={`${event.repo}:${event.created_at}:${event.event_type}`}><strong>{event.event_type.split("_").join(" ")}</strong><span>{event.repo} | {event.summary}</span><small>{event.reason}</small><div className="event-actions">{evidenceLink(event.evidence) && <a href={evidenceLink(event.evidence)!} target="_blank" rel="noreferrer">Open on GitHub</a>}{Boolean(event.evidence.fingerprint) && <button className="secondary compact" onClick={() => acknowledge(event)}>Acknowledge</button>}</div></div>) : <p className="muted">No blocking decisions.</p>}</div>
      <div className="activity-panel"><h3>PR review and crew activity</h3>{crew.length ? crew.map((event) => <div className="event-row" key={`${event.repo}:${event.created_at}:${event.event_type}`}><strong>{event.event_type.split("_").join(" ")}</strong><span>{event.repo} | {event.summary}</span><small>{String(event.evidence.worker ?? event.evidence.model ?? "Number 1")}</small>{evidenceLink(event.evidence) && <a href={evidenceLink(event.evidence)!} target="_blank" rel="noreferrer">Review evidence</a>}</div>) : <p className="muted">No recent crew events.</p>}</div>
      <div className="activity-panel"><h3>Token efficiency by course, package, stage, model, and date</h3>{Object.keys(models).length ? Object.entries(models).sort((a, b) => b[1] - a[1]).map(([model, tokens]) => <div className="token-row" key={model}><span>{model}</span><strong>{tokens.toLocaleString()}</strong></div>) : <p className="muted">Provider token telemetry is not available yet.</p>}{repos.flatMap((repo) => repo.usage_detail?.dimensions ?? []).slice(0, 8).map((row, index) => <div className="token-row token-dimension" key={`${row.date}:${row.course_key}:${row.work_package_key}:${row.stage}:${row.model}:${index}`}><span>{row.date ?? "date unknown"} | {row.course_key ?? "portfolio"} / {row.work_package_key ?? "unscoped"} | {row.stage ?? "stage unknown"} | {row.model ?? "model unknown"}</span><strong>{(row.tokens ?? 0).toLocaleString()}</strong></div>)}</div>
    </div>
  </section>;
}

export function App() {
  const [portfolio, setPortfolio] = useState<Portfolio | null>(null); const [courses, setCourses] = useState<Courses | null>(null); const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null); const [scheduleStatus, setScheduleStatus] = useState<ScheduleStatus | null>(null); const [error, setError] = useState<string | null>(null); const [refreshing, setRefreshing] = useState(false);
  const [selectedRepoName, setSelectedRepoName] = useState("");
  const refresh = () => {
    setRefreshing(true);
    setError(null);
    const reportError = (reason: unknown) => setError(String(reason));
    const requests = [
      callGateway<Portfolio>("portfolio/status", { fast: true }).then(setPortfolio).catch(reportError),
      callGateway<Courses>("courses/list").then(setCourses).catch(reportError),
      callGateway<ModelConfig>("models/config").then(setModelConfig).catch(reportError),
      callGateway<ScheduleStatus>("schedule/status").then(setScheduleStatus).catch(reportError),
    ];
    void Promise.allSettled(requests).finally(() => setRefreshing(false));
  };
  const updateRepo = async (fullName: string, payload: UpdatePayload) => { await callGateway("repos/update", { full_name: fullName, ...payload }); refresh(); };
  const courseAction = async (path: string, params: Record<string, unknown>) => { try { await callGateway(path, params); refresh(); } catch (reason) { setError(String(reason)); } };
  useEffect(refresh, []);
  const repos = portfolio?.repos ?? [];
  useEffect(() => {
    if (!repos.length || repos.some((repo) => repo.full_name === selectedRepoName)) return;
    const latest = [...repos].sort((left, right) => repoActivity(right).localeCompare(repoActivity(left)))[0];
    setSelectedRepoName(latest.full_name);
  }, [repos, selectedRepoName]);
  const selectedRepo = repos.find((repo) => repo.full_name === selectedRepoName) ?? repos[0];
  return <main className="shell"><header className="topbar"><div><p className="eyebrow">FLIGHT CONTROL</p><h1>Make It So</h1><p className="subtitle">Set the course. Engage the crew.</p></div><div className="action-row"><button className="secondary icon-label" onClick={refresh} disabled={refreshing} aria-label="Refresh portfolio"><RefreshCw size={16} aria-hidden="true" className={refreshing ? "spinning" : ""} />{refreshing ? "Refreshing" : "Refresh"}</button></div></header>
    {error && <div className="alert" role="alert">{error}</div>}
     <section className="overview" aria-labelledby="overview-title"><div className="section-heading"><div><p className="eyebrow">MISSION OVERVIEW</p><h2 id="overview-title">Current courses</h2></div><span className="status-pill">{portfolio ? `${repos.length} registered` : "Loading"}</span></div><RegisterPanel onRegistered={refresh} />{portfolio === null ? <div className="loading-state" role="status"><strong>Loading fleet status...</strong><span>Course charters and repository facts are arriving independently.</span></div> : repos.length ? <><PortfolioSummary repos={repos} />{selectedRepo && <ExecutiveSummary repo={selectedRepo} courses={courses} />}<div className="mission-layout"><RepoSelector repos={repos} selected={selectedRepo?.full_name ?? ""} onSelect={setSelectedRepoName} />{selectedRepo && <RepoPanel key={selectedRepo.full_name} repo={selectedRepo} onSave={updateRepo} />}</div></> : <div className="empty"><h3>No repositories registered</h3><p>Register a repository to begin a readiness review.</p></div>}</section>
    <SchedulePanel status={scheduleStatus} onRefresh={refresh} />
    {modelConfig && <><ModelPolicyPanel config={modelConfig} onSaved={refresh} /><UsagePolicyPanel config={modelConfig} onSaved={refresh} /></>}
     <section className="courses" aria-labelledby="courses-title"><div className="section-heading"><div><p className="eyebrow">COURSE CHARTER</p><h2 id="courses-title">Readiness and work packages</h2></div><span className="status-pill">{courses ? `${courses.courses.length} courses` : "Loading"}</span></div>{courses === null ? <div className="loading-state" role="status"><strong>Loading course charters...</strong><span>This stays independent of GitHub and token reconciliation.</span></div> : courses.courses.length ? <div className="course-list">{courses.courses.map((item) => <CoursePanel key={`${item.repository}:${item.course.key}`} item={item} repo={repos.find((repo) => repo.full_name === item.repository)} onAction={courseAction} onRefresh={refresh} />)}</div> : <p className="muted">No course charter has been saved yet.</p>}</section>
    <CourseCreatePanel repos={repos} onCreated={refresh} /><ActivityPanel repos={repos} onRefresh={refresh} /><GreenfieldPanel onCreated={refresh} />
  </main>;
}

const rootElement = document.getElementById("root");
if (rootElement) createRoot(rootElement).render(<StrictMode><App /></StrictMode>);
