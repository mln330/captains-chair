import { copyFileSync, existsSync, readFileSync, readdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const ROCKET_ICON = `rocket:c\`
    <svg viewBox="0 0 24 24">
      <path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5" />
      <path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09" />
      <path d="M9 12a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.4 22.4 0 0 1-4 2z" />
      <path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 .05 5 .05" />
    </svg>
  \``;

const PUZZLE_MARKER = "puzzle:c`";

export function installRocketIcon(assetPath) {
  const source = readFileSync(assetPath, "utf8");
  if (source.includes("rocket:c`")) return { assetPath, status: "already-installed" };
  const markerIndex = source.indexOf(PUZZLE_MARKER);
  if (markerIndex === -1) {
    throw new Error(`OpenClaw icon registry marker was not found in ${assetPath}`);
  }
  const backupPath = `${assetPath}.pre-make-it-so-rocket`;
  if (!existsSync(backupPath)) copyFileSync(assetPath, backupPath);
  const updated = `${source.slice(0, markerIndex)}${ROCKET_ICON},${source.slice(markerIndex)}`;
  writeFileSync(assetPath, updated, "utf8");
  return { assetPath, backupPath, status: "installed" };
}

export function findControlUiAsset(openClawRoot) {
  const assetsDir = join(openClawRoot, "dist", "control-ui", "assets");
  const candidates = readdirSync(assetsDir)
    .filter((name) => /^index-[^.]+\.js$/.test(name))
    .sort();
  if (candidates.length !== 1) {
    throw new Error(`Expected one active OpenClaw Control UI asset in ${assetsDir}; found ${candidates.length}`);
  }
  return join(assetsDir, candidates[0]);
}

function defaultOpenClawRoot() {
  const scriptDir = dirname(fileURLToPath(import.meta.url));
  return process.env.OPENCLAW_ROOT || join(scriptDir, "..", "node_modules", "openclaw");
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const openClawRoot = process.argv[2] || defaultOpenClawRoot();
  const result = installRocketIcon(findControlUiAsset(openClawRoot));
  process.stdout.write(`${JSON.stringify(result)}\n`);
}
