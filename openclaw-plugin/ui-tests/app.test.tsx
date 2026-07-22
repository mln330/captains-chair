import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
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
    milestones: [
      {
        course_key: "feature-search",
        work_package_key: "search",
        title: "Search",
        objective: "Implement search",
        status: "complete",
        policy: { required: true, minimum_pass_rate: 100, require_command: true, require_screenshot: true, minimum_screenshots: 1 },
        evidence: {
          status: "passed",
          reason: "test evidence passed",
          head_sha: "abcdef1",
          current_head_sha: "abcdef1",
          pass_rate: 100,
          tests_total: 8,
          tests_passed: 8,
          tests_failed: 0,
          tests_skipped: 0,
          commands: ["pytest -q"],
          screenshots: [{ kind: "screenshot", title: "desktop flow", url: "https://example.test/desktop.png" }],
          artifacts: [{ kind: "screenshot", title: "desktop flow", url: "https://example.test/desktop.png" }],
          model: "codex/gpt-5.6-luna",
          provider: "codex",
        },
        pr_url: "https://github.com/example/project/pull/1",
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

let mockRepo = repo;

function response(payload: unknown): Response {
  return { ok: true, status: 200, json: async () => payload } as Response;
}

describe("shared dashboard components", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn((request: RequestInfo | URL) => {
        const path = String(request);
        if (path.includes("portfolio/status")) return Promise.resolve(response({ repos: [mockRepo] }));
        if (path.includes("courses/list")) return Promise.resolve(response({ courses: [{ repository: repo.full_name, course: { ...course, plan_revision: 3 }, readiness: { ready: true }, number_one: { session_id: "number-one-feature-search", model: "codex/gpt-5.6-sol", last_review_at: "2026-07-18T22:00:00Z", summary: "The course is on track." }, milestone_reviews: [{ status: "on_track", summary: "The course is on track.", next_action: "Continue with search.", model: "codex/gpt-5.6-sol" }], milestone_changes: [{ proposal_id: "proposal-1", summary: "Split search validation", reason: "The current milestone needs an explicit validation step.", status: "proposed", impact: "routine", base_revision: 3, changes: [{ kind: "add", work_package: { key: "validation", title: "Validation" } }] }] }] }));
        if (path.includes("models/config")) return Promise.resolve(response({ global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"], usage: { daily_token_limit: null, model_daily_token_limits: {}, block_on_unknown: true } }));
        if (path.includes("schedule/status")) return Promise.resolve(response({ status: "inspected", jobs: [{ name: "make-it-so-course-review", every: "2h", enabled: true, health: "healthy" }, { name: "make-it-so-reconcile", every: "5m", enabled: true, health: "healthy" }] }));
        if (path.includes("schedule/install")) return Promise.resolve(response({ jobs: [{ name: "make-it-so-course-review" }] }));
        if (path.includes("run/start")) return Promise.resolve(response({ status: "started", kind: "review" }));
        if (path.includes("registration/options")) return Promise.resolve(response({
          local_clones: [{ full_name: "example/local", local_path: "/workspace/local", branch: "main", dirty: false, registered: false }],
          discord_routes: [
            { route: "channel:111111111111111111", channel_id: "111111111111111111", name: "notifications", label: "#notifications", alias: "notifications" },
            { route: "channel:200", channel_id: "200", name: "project-room", label: "#project-room" },
          ],
          default_discord_route: "channel:111111111111111111",
        }));
        if (path.includes("repos/inspect")) return Promise.resolve(response({ status: "inspected", mutation_started: false, discovery: { local_clone: { path: "/workspace/second", exists: false, cloned: false }, planning_document: { path: "docs/IMPLEMENTATION_PLAN.md", found: false, candidates: [] }, git: { branch: null, dirty: null } } }));
        if (path.includes("repos/register")) return Promise.resolve(response({ status: "registered", follow_up_required: true, follow_up_message: "Repository registered. Number One will begin initial planning.", notification_status: "sent", notification_delivery: "number_one_agent" }));
        return Promise.resolve(response({ status: "updated" }));
      }),
    );
  });

  afterEach(() => {
    cleanup();
    mockRepo = repo;
    vi.unstubAllGlobals();
  });

  it("configures a role-separated crew through first-run setup", async () => {
    const roles = {
      captain: { agent_id: "github-captain", model: "codex/gpt-5.6-terra", runtime: "openclaw" },
      coder: { agent_id: "github-coder", model: "codex/gpt-5.3-codex-spark", runtime: "openclaw" },
      reviewer: { agent_id: "github-reviewer", model: "codex/gpt-5.6-terra", runtime: "openclaw" },
      tester: { agent_id: "github-tester", model: "codex/gpt-5.6-luna", runtime: "openclaw" },
      ux_reviewer: { agent_id: "github-ux", model: "codex/gpt-5.6-terra", runtime: "openclaw" },
      final_reviewer: { agent_id: "github-final", model: "codex/gpt-5.6-sol", runtime: "openclaw" },
      merger: { agent_id: "github-merge", model: "codex/gpt-5.6-terra", runtime: "openclaw" },
      verifier: { agent_id: "github-verify", model: "codex/gpt-5.6-terra", runtime: "openclaw" },
    };
    let configured = false;
    const fetchMock = vi.fn((request: RequestInfo | URL) => {
      const path = String(request);
      if (path.includes("bootstrap/status")) return Promise.resolve(response(configured ? { configured: true, setup_required: false } : {
        configured: false,
        setup_required: true,
        runtime_available: true,
        openclaw_executable: "openclaw",
        codex_executable: "codex",
        codex_available: true,
        config_path: "/home/test/.config/make-it-so/config.yaml",
        workspace_root: "/home/test/.openclaw/make-it-so/workers",
        agents: [{ id: "main", model: "ollama/test", workspace: "/home/test/.openclaw/workspace" }],
        workers: roles,
        actions: Object.entries(roles).map(([role, worker]) => ({ role, ...worker, workspace: `/workers/${worker.agent_id}`, action: "create" })),
        schedules: { reconcile_every: "5m", review_every: "2h" },
      }));
      if (path.includes("bootstrap/apply")) { configured = true; return Promise.resolve(response({ configured: true, setup_required: false })); }
      if (path.includes("portfolio/status")) return Promise.resolve(response({ repos: [] }));
      if (path.includes("courses/list")) return Promise.resolve(response({ courses: [] }));
      if (path.includes("models/config")) return Promise.resolve(response({ global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"] }));
      if (path.includes("schedule/status")) return Promise.resolve(response({ status: "inspected", jobs: [] }));
      return Promise.resolve(response({ status: "ok" }));
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    await waitFor(() => expect(screen.getByRole("heading", { name: "Your command deck is installed" })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(screen.getByLabelText("Coder model")).toBeTruthy();
    fireEvent.change(screen.getByLabelText("Coder model"), { target: { value: "ollama/kimi-code:cloud" } });
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    fireEvent.click(screen.getByRole("button", { name: "Configure Make It So" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("bootstrap/apply"))).toBe(true));
    const applyCall = fetchMock.mock.calls.find(([request]) => String(request).includes("bootstrap/apply"));
    const applyBody = String((applyCall?.[1] as RequestInit | undefined)?.body);
    expect(applyBody).toContain("ollama/kimi-code:cloud");
    expect(applyBody).toContain('"openclaw_executable":"openclaw"');
    expect(applyBody).toContain('"codex_executable":"codex"');
    await waitFor(() => expect(screen.getByRole("heading", { name: "Current courses" })).toBeTruthy());
  });

  it("renders loaded course state and starts an immediate course review", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));
    expect(screen.getByText("Fleet at a glance")).toBeTruthy();
    expect(screen.getByText("Project at a glance")).toBeTruthy();
    expect(screen.getByText("Project goals")).toBeTruthy();
    expect(screen.getAllByText("Complete").length).toBeGreaterThan(0);
    expect(screen.getByText("Make search useful for customers.")).toBeTruthy();
    expect(screen.getAllByText("open PRs").length).toBeGreaterThan(0);
    expect(screen.getAllByText("Search feature").length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "Run course review" }));

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Course review started"));
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

  it("shows expandable milestone test evidence and screenshots", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: /expand search feature/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /expand search feature/i }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Milestone test evidence" })).toBeTruthy());
    expect(screen.getByText("1/1 passing · 1 screenshot")).toBeTruthy();
    fireEvent.click(screen.getByText("Search", { exact: true }));
    expect(screen.getByText("desktop flow")).toBeTruthy();
    expect(screen.getByText("pytest -q")).toBeTruthy();
    expect(screen.getByRole("link", { name: /open linked pr/i }).getAttribute("href")).toBe("https://github.com/example/project/pull/1");
  });

  it("shows Number One continuity and approves a pending milestone correction", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: /expand search feature/i })).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: /expand search feature/i }));
    await waitFor(() => expect(screen.getByRole("heading", { name: "Course corrections" })).toBeTruthy());
    expect(screen.getAllByText(/codex\/gpt-5.6-sol/).length).toBeGreaterThan(0);
    expect(screen.getByText("Split search validation")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("milestone-change-approve"))).toBe(true));
    const approval = fetchMock.mock.calls.find(([request]) => String(request).includes("milestone-change-approve"));
    expect(String((approval?.[1] as RequestInit | undefined)?.body)).toContain("proposal-1");
  });

  it("opens repository registration and sends the submitted fields", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    const registrationPanel = screen.getByRole("region", { name: "Set a course" });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Register repository" }));
    fireEvent.change(within(registrationPanel).getByLabelText("GitHub repository"), { target: { value: "https://github.com/example/second" } });
    await waitFor(() => expect(within(registrationPanel).getByLabelText("Discord channel")).toBeTruthy());
    fireEvent.change(within(registrationPanel).getByLabelText("Discord channel"), { target: { value: "channel:200" } });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Inspect repository" }));
    await waitFor(() => expect(within(registrationPanel).getByText("Inspection complete")).toBeTruthy());
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Continue" }));
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Continue" }));
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Register and start planning" }));

    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("repos/register"))).toBe(true));
    const registration = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/register"));
    const body = String((registration?.[1] as RequestInit | undefined)?.body);
    expect(body).toContain("example/second");
    expect(body).not.toContain("https://github.com");
    expect(body).toContain("channel:200");
    expect(body).not.toContain("local_path");
    expect(body).toContain("planning_doc_choice");
    expect(body).toContain("operation_mode");
    await waitFor(() => expect(screen.getByText("Number One planning message sent.")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "New greenfield repo" })).toBeNull();
  });

  it("accepts the owner/repository shorthand during inspection", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    const registrationPanel = screen.getByRole("region", { name: "Set a course" });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Register repository" }));
    await waitFor(() => expect((within(registrationPanel).getByLabelText("Discord channel") as HTMLSelectElement).value).toBe("channel:111111111111111111"));
    fireEvent.change(within(registrationPanel).getByLabelText("GitHub repository"), { target: { value: "example/second" } });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Inspect repository" }));
    await waitFor(() => expect(within(registrationPanel).getByText("Inspection complete")).toBeTruthy());
  });

  it("selects a verified local clone and carries its path through inspection and registration", async () => {
    const fetchMock = vi.mocked(globalThis.fetch);
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    const registrationPanel = screen.getByRole("region", { name: "Set a course" });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Register repository" }));
    await waitFor(() => expect((within(registrationPanel).getByLabelText("Discord channel") as HTMLSelectElement).value).toBe("channel:111111111111111111"));
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Local clone" }));
    fireEvent.change(within(registrationPanel).getByLabelText("Local repository"), { target: { value: "example/local" } });
    fireEvent.change(within(registrationPanel).getByLabelText("Discord channel"), { target: { value: "channel:200" } });
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Inspect repository" }));
    await waitFor(() => expect(within(registrationPanel).getByText("Inspection complete")).toBeTruthy());

    const inspectionCall = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/inspect"));
    expect(String((inspectionCall?.[1] as RequestInit | undefined)?.body)).toContain('"local_path":"/workspace/local"');
    for (let index = 0; index < 2; index += 1) fireEvent.click(within(registrationPanel).getByRole("button", { name: "Continue" }));
    fireEvent.click(within(registrationPanel).getByRole("button", { name: "Register and start planning" }));
    await waitFor(() => expect(fetchMock.mock.calls.some(([request]) => String(request).includes("repos/register"))).toBe(true));
    const registrationCall = fetchMock.mock.calls.find(([request]) => String(request).includes("repos/register"));
    expect(String((registrationCall?.[1] as RequestInit | undefined)?.body)).toContain('"local_path":"/workspace/local"');
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

  it("changes intelligence without silently changing selected models", async () => {
    render(<App />);
    await waitFor(() => expect(screen.getAllByText("example/project").length).toBeGreaterThan(0));

    fireEvent.click(screen.getByText("Repository controls"));
    const strategistModel = screen.getAllByDisplayValue("codex/gpt-5.6-sol")[0] as HTMLInputElement;
    fireEvent.change(screen.getAllByLabelText("Intelligence level")[0], { target: { value: "deep" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Apply intelligence" })[0]);

    expect(strategistModel.value).toBe("codex/gpt-5.6-sol");
    expect(screen.getAllByDisplayValue("xhigh").length).toBeGreaterThan(0);
  });

  it("shows UI acceptance as a first-class repository fact for web work", async () => {
    render(<App />);

    await waitFor(() => expect(screen.getByText("UI acceptance")).toBeTruthy());
    expect(screen.getAllByText("required").length).toBeGreaterThan(0);
  });

  it("does not call a terminal course complete when required evidence is missing", async () => {
    mockRepo = {
      ...repo,
      state: "merged",
      workboard_status: {
        ...repo.workboard_status,
        status: "completed",
        terminal: true,
        milestones: [{
          ...repo.workboard_status.milestones[0],
          evidence: { status: "not_run" },
        }],
      },
    };
    render(<App />);

    await waitFor(() => expect(screen.getByText("Evidence gate pending")).toBeTruthy());
    expect(screen.getAllByText("Evidence pending").length).toBeGreaterThan(0);
    expect(screen.getByText("Implementation is terminal, but required milestone evidence has not been recorded.")).toBeTruthy();
    expect(screen.queryByText("Implementation verified")).toBeNull();
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
