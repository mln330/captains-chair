import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";

const root = resolve(import.meta.dirname, "..");
const cli = join(root, "node_modules", "openclaw", "openclaw.mjs");
const profile = `make-it-so-host-${process.pid}`;
const profileDir = join(homedir(), `.openclaw-${profile}`);
const workspaceDir = join(homedir(), `.openclaw`, `workspace-${profile}`);

function run(args) {
  return execFileSync(process.execPath, [cli, "--profile", profile, ...args], {
    cwd: root,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
    timeout: 60_000,
  });
}

try {
  mkdirSync(profileDir, { recursive: true });
  writeFileSync(join(profileDir, "openclaw.json"), JSON.stringify({
    plugins: {
      load: { paths: [root] },
      entries: { "make-it-so": { enabled: true } },
    },
  }));
  const inspection = JSON.parse(run(["plugins", "inspect", "make-it-so", "--json", "--runtime"]));
  const plugin = inspection.plugin;
  if (plugin?.status !== "loaded") throw new Error(`plugin status was ${plugin?.status ?? "missing"}`);
  if (plugin.diagnostics?.length) throw new Error("plugin diagnostics were reported");
  for (const tool of [
    "make_it_so_course_status",
    "make_it_so_resolve_checkpoint",
    "make_it_so_answer_readiness",
    "make_it_so_start_planning",
    "make_it_so_ready_work",
    "make_it_so_approve_course",
  ]) {
    if (!plugin.toolNames.includes(tool)) throw new Error(`missing registered tool: ${tool}`);
  }
  if (!plugin.hookNames.includes("make-it-so-workboard-reconciliation")) {
    throw new Error("missing Workboard reconciliation hook");
  }
  if (!plugin.services.includes("make-it-so")) throw new Error("missing sidecar service");
  if (!plugin.commands.includes("make-it-so")) throw new Error("missing native /make-it-so command");
  const doctor = run(["plugins", "doctor"]);
  if (!doctor.includes("No plugin issues detected.")) throw new Error(`plugin doctor failed: ${doctor}`);
  process.stdout.write("OpenClaw host registration passed\n");
} finally {
  for (const path of [profileDir, workspaceDir]) {
    if (existsSync(path)) rmSync(path, { recursive: true, force: true });
  }
}
