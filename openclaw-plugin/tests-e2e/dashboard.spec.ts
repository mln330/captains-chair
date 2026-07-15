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
  work_packages: [{ key: "discovery", title: "Discovery", objective: "Inspect the repository.", status: "planned" }],
  checkpoints: [],
};

async function mockApi(page: Page) {
  await page.route("**/captains-chair/api/portfolio/status", (route) => route.fulfill({ json: { repos: [repo] } }));
  await page.route("**/captains-chair/api/courses/list", (route) => route.fulfill({ json: { courses: [{ repository: repo.full_name, course, readiness: { ready: true, unresolved: [] } }] } }));
  await page.route("**/captains-chair/api/models/config", (route) => route.fulfill({ json: { global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"] } }));
  await page.route("**/captains-chair/api/schedule/status", (route) => route.fulfill({ json: { status: "inspected", jobs: [{ name: "captains-chair-reconcile", every: "5m", enabled: true, health: "healthy" }, { name: "captains-chair-course-review", every: "2h", enabled: false, health: "paused" }] } }));
  await page.route("**/captains-chair/api/models/validate", (route) => route.fulfill({ json: { can_save: true, status: "unverified", warnings: [] } }));
  await page.route("**/captains-chair/api/course/models", (route) => route.fulfill({ json: { status: "updated" } }));
  await page.route("**/captains-chair/api/course/planning-session", (route) => route.fulfill({
    json: {
      interaction: "host_agent_conversation",
      mutation_requires_course_approval: true,
      next_questions: ["Which search users are in scope?"],
      prompt: "Inspect the repository, ask only unresolved questions, and wait for explicit course approval.",
    },
  }));
}

test("dashboard renders the course map and planning brief", async ({ page }, testInfo) => {
  await mockApi(page);
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "Captain's Chair" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "example/project" })).toBeVisible();
  await expect(page.getByText("Search improvements")).toBeVisible();
  await page.getByRole("button", { name: /expand search improvements/i }).click();
  await page.getByRole("button", { name: "Open planning brief" }).click();
  await expect(page.getByRole("heading", { name: "Plan and charter review" })).toBeVisible();
  await expect(page.getByText("Which search users are in scope?")).toBeVisible();
  await expect(page.getByRole("region", { name: "SDLC progress" })).toHaveAttribute("tabindex", "0");
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
  await expect(page.getByText("captains-chair-course-review")).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth)).toBe(true);
});
