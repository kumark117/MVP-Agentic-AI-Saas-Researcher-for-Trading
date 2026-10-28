import { defineConfig, devices } from "@playwright/test";

// Run through scripts/e2e.ps1, which starts the SYNTHETIC stack on alloyiq_e2e.
export default defineConfig({
  testDir: "./e2e",
  timeout: 180_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  workers: 1, // auth rate limits are per IP; journeys run one after another
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
    trace: "retain-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
    { name: "mobile", use: { ...devices["Pixel 7"] }, testMatch: /a11y\.spec\.ts/ },
  ],
});
