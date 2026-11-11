import { defineConfig } from '@playwright/test';
import { origin } from './fixture-settings.mjs';

export default defineConfig({
  testDir: './specs',
  testMatch: '**/*.spec.mjs',
  fullyParallel: false,
  forbidOnly: true,
  workers: 1,
  retries: 0,
  timeout: 25_000,
  outputDir: process.env.AFTERCARE_E2E_RESULTS_DIR ?? './test-results',
  use: {
    baseURL: origin,
    browserName: 'chromium',
    headless: true,
    serviceWorkers: 'block',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'node fixture-server.mjs',
    url: `${origin}/__fixture_ready`,
    reuseExistingServer: false,
    timeout: 15_000,
  },
});
