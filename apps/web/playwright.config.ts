import { defineConfig, devices } from "@playwright/test";

const webPort = Number(process.env.RELAY_E2E_WEB_PORT ?? 5273);

// The stack script creates a throwaway database, seeds it, and starts the API,
// the worker and the UI on spare ports. Tests share that one stack and run
// serially, because they change shared demo state (the destination's behaviour).
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  timeout: 90_000,
  expect: { timeout: 20_000 },
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  use: {
    baseURL: `http://127.0.0.1:${webPort}`,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: "../../scripts/e2e-stack.sh",
    url: `http://127.0.0.1:${webPort}`,
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
