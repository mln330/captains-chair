import { execFileSync } from "node:child_process";

const npmCli = process.env.npm_execpath;
if (!npmCli) throw new Error("npm_execpath is unavailable");

const output = execFileSync(
  process.execPath,
  [npmCli, "pack", "--dry-run", "--json"],
  { cwd: process.cwd(), encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] },
);
const jsonStart = output.lastIndexOf('[\n  {\n    "id"');
if (jsonStart < 0) throw new Error(`npm pack did not emit a JSON report: ${output.slice(-500)}`);
const pack = JSON.parse(output.slice(jsonStart))[0];
const paths = new Set(pack.files.map((entry) => entry.path));
for (const required of [
  "dist/index.js",
  "dist/ui/assets/index.js",
  "dist/ui/assets/index.css",
  "openclaw.plugin.json",
]) {
  if (!paths.has(required)) throw new Error(`published package is missing ${required}`);
}
if ([...paths].some((path) => path.startsWith("tests") || path.startsWith("ui/src"))) {
  throw new Error("published package contains development-only test or UI source files");
}

process.stdout.write("OpenClaw package contents passed\n");
