import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App } from "../ui/src/main";

const repo = {
  full_name: "example/project",
  local_path: "/workspace/example-project",
  exists: true,
  dirty: false,
  operation_mode: "advisory",
  completion_policy: "owner_approval",
  state: "ready",
  schedule_enabled: true,
  notification_route: "notifications",
  surfaces: ["web_ui"],
  tokens: { total_tokens: 1200, accounted_tokens: 1200 },
  worker_models: { coder: "codex/gpt-5.3-codex-spark", reviewer: "codex/gpt-5.6-terra" },
  worker_runtimes: { coder: "codex", reviewer: "openclaw" },
  github_status: { status: "available", open_prs: 0, checks: { passed: 4, pending: 0, failed: 0 }, prs: [] },
  usage_detail: {
    dimensions: [
      { stage: "implementation", model: "codex/gpt-5.6-terra", tokens: 800 },
      { stage: "review", model: "codex/gpt-5.6-terra", tokens: 400 },
    ],
  },
  workboard_status: {
    status: "completed",
    terminal: true,
    pr_count: 1,
    review_cycles: 1,
    reviews_passed: 1,
    review_status: "passed",
    test_status: "passed",
    blockers: 0,
    current_blockers: 0,
    historical_blockers: 0,
    superseded_retries: 1,
    total_loop_count: 1,
    stage_history: [
      { stage: "implementation", total: 1, done: 1, active: 0, blocked: 0, loops: 0, models: ["codex/gpt-5.6-terra"] },
      { stage: "review", total: 2, done: 1, active: 0, blocked: 1, loops: 1, retry_attempts: 1, superseded_retries: 1, historical_blockers: 0, models: ["codex/gpt-5.6-terra"] },
      { stage: "repair", total: 1, done: 1, active: 0, blocked: 0, loops: 1, models: ["codex/gpt-5.6-terra"] },
      { stage: "test", total: 1, done: 1, active: 0, blocked: 0, loops: 0, models: ["codex/gpt-5.6-luna"] },
      { stage: "final_review", total: 1, done: 1, active: 0, blocked: 0, loops: 0, models: ["codex/gpt-5.6-sol"] },
      { stage: "merge", total: 1, done: 1, active: 0, blocked: 0, loops: 0, models: [] },
      { stage: "post_merge", total: 1, done: 1, active: 0, blocked: 0, loops: 0, models: ["codex/gpt-5.6-terra"] },
    ],
    workflow_runs: [
      {
        workflow: "build-run",
        index: 1,
        title: "Implement search",
        kind: "build",
        status: "superseded",
        cards: 2,
        done: 2,
        loops: 0,
        timeline: [{ id: "build", stage: "implementation", status: "done", summary: "Implemented search", model: "codex/gpt-5.6-terra", pr_url: "https://github.com/example/project/pull/1" }],
      },
      {
        workflow: "review-run",
        index: 2,
        title: "Review and repair",
        kind: "review",
        status: "completed",
        current: true,
        cards: 3,
        done: 3,
        loops: 1,
        superseded_retries: 1,
        timeline: [{ id: "repair", stage: "repair", status: "done", summary: "Addressed review finding", model: "codex/gpt-5.6-terra", loop: true, pr_url: "https://github.com/example/project/pull/1" }],
      },
    ],
  },
  warnings: [],
};

const course = {
  key: "feature-search",
  title: "Search feature",
  kind: "feature",
  status: "readiness_review",
  goal: "Make search useful for customers.",
  readiness: [],
  work_packages: [{ key: "search", title: "Search", objective: "Implement search", status: "planned" }],
  checkpoints: [],
};

function response(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as Response;
}

describe("shared dashboard components", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((request: RequestInfo | URL) => {
        const path = String(request);
        if (path.includes("portfolio/status")) return Promise.resolve(response({ repos: [repo] }));
        if (path.includes("courses/list")) return Promise.resolve(response({ courses: [{ repository: repo.full_name, course, readiness: { ready: true } }] }));
        if (path.includes("models/config")) return Promise.resolve(response({ global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"], usage: { daily_token_limit: null, model_daily_token_limits: {}, block_on_unknown: true } }));
        if (path.includes("schedule/status")) return Promise.resolve(response({ status: "inspected", jobs: [{ name: "make-it-so-course-review", every: "2h", enabled: true, health: "healthy" }, { name: "make-it-so-reconcile", every: "5m", enabled: true, health: "healthy" }] }));
        if (path.includes("schedule/install")) return Promise.resolve(response({ jobs: [{ name: "make-it-so-course-review" }] }));
        return Promise.resolve(response({ status: "updated" }));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("renders loaded course state and exercises schedule installation", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));
    expect(screen.getByText("Fleet at a glance")).toBeTruthy();
    expect(screen.getAllByText("open PRs").length).toBeGreaterThan(0);
    expect(screen.getByText("Search feature")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Reconcile" }));

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Schedule install"));
  });

  it("shows implementation evidence, feedback loops, models, and workflow runs", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("How the work moved")).toBeTruthy());
    expect(screen.getByText("FLIGHT RECORDER")).toBeTruthy();
    expect(screen.getByText("Workflow runs")).toBeTruthy();
    expect(screen.getByText("1 feedback loop")).toBeTruthy();
    expect(screen.getAllByText(/1 superseded retry/).length).toBeGreaterThan(0);
    expect(screen.getByText("Coding route: gpt-5.3-codex-spark via direct Codex")).toBeTruthy();
    expect(screen.getByText(/OpenClaw owns the Workboard lifecycle/)).toBeTruthy();
    expect(screen.getByText("Implemented search")).toBeTruthy();
    expect(screen.getByText("Addressed review finding")).toBeTruthy();
    expect(screen.getAllByText("Build").length).toBeGreaterThan(0);
  });

  it("opens repository registration and sends the submitted fields", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole("button", { name: "Register repository" }));
    fireEvent.change(screen.getByLabelText("GitHub repository"), { target: { value: "example/second" } });
    fireEvent.change(screen.getByLabelText("Local path"), { target: { value: "/workspace/second" } });
    fireEvent.submit(screen.getByRole("button", { name: "Register repository" }).closest("form")!);

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("repos/register"))).toBe(true));
    const registration = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/register"));
    expect(String((registration?.[1] as RequestInit | undefined)?.body)).toContain("example/second");
  });

  it("registers a greenfield course without claiming remote creation", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByRole("button", { name: "New greenfield repo" }));
    fireEvent.change(screen.getByLabelText("GitHub repository"), { target: { value: "example/new-project" } });
    fireEvent.change(screen.getByLabelText("Local path"), { target: { value: "/workspace/new-project" } });
    fireEvent.change(screen.getByLabelText("Course title"), { target: { value: "New project" } });
    fireEvent.change(screen.getByLabelText("Goal"), { target: { value: "Deliver a useful new product." } });
    fireEvent.submit(screen.getByRole("button", { name: "Create readiness review" }).closest("form")!);

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("repos/create"))).toBe(true));
    const creation = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/create"));
    expect(String((creation?.[1] as RequestInit | undefined)?.body)).toContain("greenfield");
  });

  it("saves repository autonomy, channel, and model controls", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByText("Repository controls"));
    fireEvent.change(screen.getByLabelText("Autonomy"), { target: { value: "autonomous" } });
    fireEvent.change(screen.getByLabelText("Discord route"), { target: { value: "project-room" } });
    const modelInputs = screen.getAllByDisplayValue("codex/gpt-5.6-sol");
    fireEvent.change(modelInputs[0], { target: { value: "codex/gpt-5.3-codex-spark" } });
    fireEvent.click(screen.getByRole("button", { name: "Add QA profile" }));
    fireEvent.change(screen.getByLabelText("Profile key"), { target: { value: "ui-qa" } });
    fireEvent.click(screen.getByRole("button", { name: "Save controls" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("repos/update"))).toBe(true));
    const update = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/update"));
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("autonomous");
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("project-room");
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("gpt-5.3-codex-spark");
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("ui-qa");
  });

  it("applies a local-first route preset before manual edits", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByText("Repository controls"));
    fireEvent.change(screen.getAllByLabelText("Route preset")[0], { target: { value: "local_first" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Apply preset" })[0]);

    expect(screen.getAllByDisplayValue("ollama/qualified-local").length).toBeGreaterThan(0);
  });

  it("saves token safeguards and model-specific limits", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getByText("Token safeguards")).toBeTruthy());

    fireEvent.change(screen.getByLabelText("Daily token limit"), { target: { value: "1000" } });
    fireEvent.click(screen.getByRole("button", { name: "Add model limit" }));
    fireEvent.change(screen.getAllByLabelText("Model").at(-1)!, { target: { value: "codex/gpt-5.3-codex-spark" } });
    fireEvent.change(screen.getByLabelText("Daily tokens"), { target: { value: "600" } });
    fireEvent.click(screen.getByRole("button", { name: "Save token safeguards" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("usage/update"))).toBe(true));
    const update = fetchMock.mock.calls.find(([request]) => String(request).includes("usage/update"));
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("1000");
    expect(String((update?.[1] as RequestInit | undefined)?.body)).toContain("gpt-5.3-codex-spark");
  });
});
