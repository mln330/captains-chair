// @vitest-environment node
import { mkdtempSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { findControlUiAsset, installRocketIcon } from "../scripts/install-sidebar-rocket.mjs";

describe("OpenClaw sidebar rocket installer", () => {
  it("adds the rocket before the puzzle fallback and is idempotent", () => {
    const root = mkdtempSync(join(tmpdir(), "make-it-so-icon-"));
    const assets = join(root, "dist", "control-ui", "assets");
    mkdirSync(assets, { recursive: true });
    const asset = join(assets, "index-test.js");
    writeFileSync(asset, "const M={circle:c`circle`,puzzle:c`puzzle`};", "utf8");

    expect(findControlUiAsset(root)).toBe(asset);
    expect(installRocketIcon(asset).status).toBe("installed");
    expect(installRocketIcon(asset).status).toBe("already-installed");
    const installed = readFileSync(asset, "utf8");
    expect(installed).toContain("rocket:c`");
    expect(installed.indexOf("rocket:c`")).toBeLessThan(installed.indexOf("puzzle:c`"));
    expect(installed.match(/rocket:c`/g)).toHaveLength(1);
  });

  it("refuses to modify an unknown dashboard bundle", () => {
    const root = mkdtempSync(join(tmpdir(), "make-it-so-icon-"));
    const asset = join(root, "index-test.js");
    writeFileSync(asset, "const icons = {};", "utf8");
    expect(() => installRocketIcon(asset)).toThrow("icon registry marker was not found");
  });
});
