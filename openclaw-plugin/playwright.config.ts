import { defineConfig, devices } from "@playwright/test";

const e2ePort = process.env.CAPTAINS_CHAIR_E2E_PORT ?? "4191";

export default defineConfig({
  testDir: "./tests-e2e",
  fullyParallel: true,
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: {
    baseURL: `http://127.0.0.1:${e2ePort}`,
    trace: "on-first-retry",
  },
  webServer: {
    command: `npm run build && python -m http.server ${e2ePort} --directory dist/ui`,
    cwd: ".",
    url: `http://127.0.0.1:${e2ePort}/`,
    reuseExistingServer: false,
    timeout: 120_000,
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 5"] } },
  ],
});
