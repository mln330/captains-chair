import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

const repo = {
  full_name: "example/project",
  local_path: "/workspace/project",
  exists: true,
  dirty: false,
  operation_mode: "supervised",
  completion_policy: "owner_approval",
  state: "ready",
  notification_route: "notifications",
  surfaces: ["web_ui"],
  tokens: { total_tokens: 1250 },
  worker_models: { coder: "codex/gpt-5.6-terra" },
  github_status: { status: "available", open_prs: 0, checks: { passed: 1, pending: 0, failed: 0 }, prs: [] },
  workboard_status: {
    status: "ready",
    current_stage: "planning",
    review_cycles: 0,
    historical_blockers: 0,
    current_blockers: 0,
    stage_history: [],
    workflow_runs: [],
    milestones: [{
      course_key: "feature-search",
      work_package_key: "discovery",
      title: "Discovery",
      objective: "Inspect the repository.",
      status: "complete",
      policy: { required: true, minimum_pass_rate: 100, require_command: true, require_screenshot: true, minimum_screenshots: 1 },
      evidence: {
        status: "passed",
        reason: "test evidence passed",
        head_sha: "abcdef1",
        current_head_sha: "abcdef1",
        pass_rate: 100,
        tests_total: 3,
        tests_passed: 3,
        tests_failed: 0,
        tests_skipped: 0,
        commands: ["pytest -q"],
        screenshots: [{ kind: "screenshot", title: "Desktop screenshot", url: "https://example.test/evidence/desktop.png" }],
        artifacts: [{ kind: "screenshot", title: "Desktop screenshot", url: "https://example.test/evidence/desktop.png" }],
        model: "codex/gpt-5.6-luna",
        provider: "codex",
      },
      pr_url: "https://github.com/example/project/pull/1",
    }],
  },
  warnings: [],
  events: [],
};

const course = {
  key: "feature-search",
  title: "Search improvements",
  kind: "feature",
  status: "readiness_review",
  goal: "Make repository search faster and easier to use for existing users.",
  readiness: [],
  work_packages: [{ key: "discovery", title: "Discovery", objective: "Inspect the repository.", status: "planned", test_evidence_policy: { required: true, minimum_pass_rate: 100, require_command: true, require_screenshot: true, minimum_screenshots: 1 } }],
  checkpoints: [],
};

async function mockApi(page: Page) {
  await page.route("**/make-it-so/api/portfolio/status", (route) => route.fulfill({ json: { repos: [repo] } }));
  await page.route("**/make-it-so/api/courses/list", (route) => route.fulfill({ json: { courses: [{ repository: repo.full_name, course, readiness: { ready: true, unresolved: [] } }] } }));
  await page.route("**/make-it-so/api/models/config", (route) => route.fulfill({ json: { global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"] } }));
  await page.route("**/make-it-so/api/schedule/status", (route) => route.fulfill({ json: { status: "inspected", jobs: [{ name: "make-it-so-reconcile", every: "5m", enabled: true, health: "healthy" }, { name: "make-it-so-course-review", every: "2h", enabled: false, health: "paused" }] } }));
  await page.route("**/make-it-so/api/models/validate", (route) => route.fulfill({ json: { can_save: true, status: "unverified", warnings: [] } }));
  await page.route("**/make-it-so/api/repos/register", (route) => route.fulfill({ json: { status: "registered", follow_up_required: true, follow_up_message: "Repository registered. Number 1 will follow up in chat before work begins." } }));
  await page.route("**/make-it-so/api/course/models", (route) => route.fulfill({ json: { status: "updated" } }));
  await page.route("**/make-it-so/api/course/planning-session", (route) => route.fulfill({
    json: {
      interaction: "host_agent_conversation",
      mutation_requires_course_approval: true,
      next_questions: ["Which search users are in scope?"],
      prompt: "Inspect the repository, ask only unresolved questions, and wait for explicit course approval.",
    },
  }));
  await page.route("**/make-it-so/api/course/milestone-evidence", (route) => route.fulfill({ json: { milestones: repo.workboard_status.milestones } }));
}

test("repository registration stays with the mission overview and asks only for repo and route", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");

  const overview = page.getByRole("region", { name: "Current courses" });
  const registration = overview.getByRole("region", { name: "Add a repository" });
  await expect(registration.getByRole("heading", { name: "Add a repository" })).toBeVisible();
  await registration.getByRole("button", { name: "Register repository" }).click();
  await registration.getByLabel("GitHub repository").fill("https://github.com/example/second");
  await registration.getByLabel("Discord route").fill("project-room");
  const requestPromise = page.waitForRequest((request) => request.url().endsWith("/repos/register") && request.method() === "POST");
  await registration.getByRole("button", { name: "Register and inspect" }).click();
  const body = (await requestPromise).postDataJSON();
  expect(body).toEqual({ full_name: "example/second", notification_route: "project-room" });
  await expect(registration.getByRole("status")).toContainText("Number 1 will follow up in chat");
});

test("dashboard renders the course map and planning brief", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Make It So" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "example/project" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Project at a glance" })).toBeVisible();
  await expect(page.getByText("Project goals", { exact: true })).toBeVisible();
  await expect(page.getByLabel("High-level goals for example/project").getByText("Ready to engage", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /expand search improvements/i }).click();
  await page.getByRole("button", { name: "Open planning brief" }).click();
  await expect(page.getByRole("heading", { name: "Plan and charter review" })).toBeVisible();
  await expect(page.getByText("Which search users are in scope?")).toBeVisible();
  await expect(page.getByRole("region", { name: "Execution evidence for example/project" })).toBeVisible();
  const accessibility = await new AxeBuilder({ page }).analyze();
  expect(accessibility.violations).toEqual([]);
  const screenshot = await page.screenshot({ fullPage: true, path: testInfo.outputPath("dashboard-planning-brief.png") });
  expect(screenshot.byteLength).toBeGreaterThan(10_000);

  await page.getByText("Course and package model routes", { exact: true }).click();
  await page.getByLabel("Override layer").selectOption("stage");
  await page.getByLabel("Stage name").fill("implementation");
  await page.getByLabel("Model").last().fill("codex/stage-canary");
  const requestPromise = page.waitForRequest((request) => request.url().endsWith("/course/models") && request.method() === "POST");
  await page.getByRole("button", { name: "Save model routes" }).click();
  expect((await requestPromise).postDataJSON()).toMatchObject({ layer: "stage", stage_name: "implementation", stage_scope: "course" });
});

test("dashboard planning controls are keyboard reachable", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");

  const expand = page.getByRole("button", { name: /expand search improvements/i });
  await expand.focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Open planning brief" })).toBeVisible();

  const planning = page.getByRole("button", { name: "Open planning brief" });
  await planning.focus();
  await expect(planning).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("heading", { name: "Plan and charter review" })).toBeVisible();
});

test("dashboard has no horizontal overflow and exposes schedule controls", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Managed schedules" })).toBeVisible();
  await expect(page.getByText("make-it-so-course-review")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});

test("dashboard expands milestone evidence and screenshot proof", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");

  await page.getByRole("button", { name: /expand search improvements/i }).click();
  await expect(page.getByRole("heading", { name: "Milestone test evidence" })).toBeVisible();
  await expect(page.getByText("1/1 passing · 1 screenshot", { exact: true })).toBeVisible();
  await page.getByText("Discovery", { exact: true }).click();
  await expect(page.getByText("Desktop screenshot", { exact: true })).toBeVisible();
  await expect(page.getByText("pytest -q", { exact: true })).toBeVisible();
  await expect(page.getByRole("link", { name: /open linked pr/i })).toHaveAttribute("href", "https://github.com/example/project/pull/1");
});
