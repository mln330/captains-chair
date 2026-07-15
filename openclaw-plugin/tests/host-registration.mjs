import { execFileSync } from "node:child_process";
import { existsSync, rmSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const cli = join(root, "node_modules", "openclaw", "openclaw.mjs");
const profile = `captains-chair-host-${process.pid}`;
const profileDir = join(homedir(), `.openclaw-${profile}`);
const workspaceDir = join(homedir(), `.openclaw`, `workspace-${profile}`);

function run(args) {
  return execFileSync(process.execPath, [cli, "--profile", profile, ...args], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  });
}

try {
  run(["plugins", "install", root, "--link"]);
  run(["plugins", "disable", "captains-chair"]);
  const disabled = JSON.parse(run(["plugins", "inspect", "captains-chair", "--json"]));
  if (disabled.plugin?.status !== "disabled") throw new Error("plugin did not disable cleanly");
  run(["plugins", "enable", "captains-chair"]);
  const inspection = JSON.parse(run(["plugins", "inspect", "captains-chair", "--json", "--runtime"]));
  const plugin = inspection.plugin;
  if (plugin?.status !== "loaded") throw new Error(`plugin status was ${plugin?.status ?? "missing"}`);
  if (plugin.diagnostics?.length) throw new Error("plugin diagnostics were reported");
  for (const tool of [
    "captains_chair_course_status",
    "captains_chair_resolve_checkpoint",
    "captains_chair_answer_readiness",
    "captains_chair_start_planning",
    "captains_chair_ready_work",
  ]) {
    if (!plugin.toolNames.includes(tool)) throw new Error(`missing registered tool: ${tool}`);
  }
  if (!plugin.hookNames.includes("captains-chair-workboard-reconciliation")) {
    throw new Error("missing Workboard reconciliation hook");
  }
  if (!plugin.services.includes("captains-chair")) throw new Error("missing sidecar service");
  const doctor = run(["plugins", "doctor"]);
  if (!doctor.includes("No plugin issues detected.")) throw new Error(`plugin doctor failed: ${doctor}`);
  process.stdout.write("OpenClaw host registration passed\n");
} finally {
  for (const path of [profileDir, workspaceDir]) {
    if (existsSync(path)) rmSync(path, { recursive: true, force: true });
  }
}
