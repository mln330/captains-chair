import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { extname, join } from "node:path";

const port = Number(process.env.MAKE_IT_SO_FIXTURE_PORT ?? 4197);
const uiRoot = join(import.meta.dirname, "..", "dist", "ui");
const stage = (name, total, models, loops = 0) => ({ stage: name, total, done: total, active: 0, blocked: 0, loops, models });
const step = (id, name, summary, model, loop = false) => ({
  id, stage: name, status: "done", summary, model, loop,
  pr_url: "https://github.com/mln330/image-manager/pull/1",
});
const repo = {
  full_name: "mln330/image-manager",
  local_path: "/home/mln330/.openclaw/workspace/image-manager",
  exists: true,
  dirty: false,
  operation_mode: "autonomous",
  completion_policy: "auto_merge",
  allow_autonomous_merge: true,
  state: "merged",
  schedule_enabled: true,
  notification_route: "notifications",
  surfaces: ["web_ui"],
  tokens: { total_tokens: 763469, accounted_tokens: 763469 },
  worker_models: {
    coder: "codex/gpt-5.6-terra", reviewer: "codex/gpt-5.6-terra",
    tester: "codex/gpt-5.6-luna", final_reviewer: "codex/gpt-5.6-sol",
  },
  github_status: { status: "available", open_prs: 0, checks: { passed: 7, pending: 0, failed: 0 }, prs: [] },
  usage_detail: {
    dimensions: [
      { stage: "implementation", model: "codex/gpt-5.6-terra", tokens: 28104 },
      { stage: "review", model: "codex/gpt-5.6-terra", tokens: 92166 },
      { stage: "repair", model: "codex/gpt-5.6-terra", tokens: 228507 },
      { stage: "test", model: "codex/gpt-5.6-luna", tokens: 55302 },
      { stage: "final_review", model: "codex/gpt-5.6-sol", tokens: 283904 },
      { stage: "post_merge", model: "codex/gpt-5.6-terra", tokens: 75486 },
    ],
    model_totals: [
      { model: "codex/gpt-5.6-terra", accounted_tokens: 424263 },
      { model: "codex/gpt-5.6-sol", accounted_tokens: 283904 },
      { model: "codex/gpt-5.6-luna", accounted_tokens: 55302 },
    ],
  },
  workboard_status: {
    status: "completed", terminal: true, current_stage: "post_merge",
    pr_count: 1, review_cycles: 3, reviews_passed: 3, review_status: "passed",
    test_status: "passed", blockers: 0, current_blockers: 0, historical_blockers: 3,
    total_loop_count: 6,
    stage_history: [
      stage("implementation", 1, ["codex/gpt-5.6-terra"]),
      stage("review", 3, ["codex/gpt-5.6-terra"]),
      stage("repair", 3, ["codex/gpt-5.6-terra"], 3),
      stage("test", 3, ["codex/gpt-5.6-luna"]),
      stage("final_review", 3, ["codex/gpt-5.6-sol"], 2),
      stage("merge", 1, ["deterministic gate"]),
      stage("post_merge", 1, ["codex/gpt-5.6-terra"]),
    ],
    workflow_runs: [
      { workflow: "build", index: 1, title: "Build the production image workspace", kind: "build", status: "superseded", cards: 3, done: 3, loops: 0, timeline: [step("build", "implementation", "Built upload, library, and metadata workflows", "codex/gpt-5.6-terra")] },
      { workflow: "review", index: 2, title: "Independent PR review and usability QA", kind: "review", status: "superseded", cards: 9, done: 9, loops: 4, timeline: [step("review", "review", "Found keyboard and responsive navigation issues", "codex/gpt-5.6-terra"), step("repair", "repair", "Reworked focus order and mobile layout", "codex/gpt-5.6-terra", true), step("test", "test", "Unit, integration, and browser checks passed", "codex/gpt-5.6-luna")] },
      { workflow: "completion", index: 3, title: "Final review, merge, and post-merge proof", kind: "completion", status: "completed", current: true, cards: 5, done: 5, loops: 2, timeline: [step("final", "final_review", "Exit criteria verified after two focused repair passes", "codex/gpt-5.6-sol", true), step("merge", "merge", "PR #1 merged through deterministic gate", "deterministic gate"), step("verify", "post_merge", "Main branch checks and production build passed", "codex/gpt-5.6-terra")] },
    ],
  },
  events: [],
  warnings: [],
};
const course = { key: "image-workspace", title: "Image Manager production course", kind: "greenfield", status: "completed", goal: "Ship a production-ready image management application.", readiness: [], work_packages: [{ key: "application", title: "Application", objective: "Deliver the application", status: "completed" }], checkpoints: [] };

function json(response, payload) {
  response.writeHead(200, { "content-type": "application/json" });
  response.end(JSON.stringify(payload));
}

createServer(async (request, response) => {
  const path = new URL(request.url ?? "/", `http://127.0.0.1:${port}`).pathname;
  if (path === "/make-it-so/api/portfolio/status") return json(response, { repos: [repo] });
  if (path === "/make-it-so/api/courses/list") return json(response, { courses: [{ repository: repo.full_name, course, readiness: { ready: true, unresolved: [] } }] });
  if (path === "/make-it-so/api/models/config") return json(response, { global_profiles: {}, runtime_profiles: {}, runtimes: ["openclaw"], usage: { block_on_unknown: true } });
  if (path === "/make-it-so/api/schedule/status") return json(response, { status: "inspected", jobs: [{ name: "make-it-so-reconcile", every: "5m", enabled: true, health: "healthy" }, { name: "make-it-so-course-review", every: "2h", enabled: true, health: "healthy" }] });
  if (path.startsWith("/make-it-so/api/")) return json(response, { status: "ok" });
  const relative = path === "/" ? "index.html" : path.replace(/^\//, "");
  try {
    const body = await readFile(join(uiRoot, relative));
    const types = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css" };
    response.writeHead(200, { "content-type": types[extname(relative)] ?? "application/octet-stream" });
    response.end(body);
  } catch {
    response.writeHead(404);
    response.end("Not found");
  }
}).listen(port, "127.0.0.1", () => console.log(`Dashboard fixture listening on ${port}`));
