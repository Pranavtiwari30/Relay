import { expect, test, type Page } from "@playwright/test";

// Captures the screenshots used in the docs. Opt-in, so a normal e2e run never
// rewrites committed images:  RELAY_CAPTURE_SCREENSHOTS=1 npx playwright test screenshots
//
// Every screen shown is real application state produced through the real
// pipeline in the disposable e2e database. Nothing is mocked for the docs.
test.skip(!process.env.RELAY_CAPTURE_SCREENSHOTS, "set RELAY_CAPTURE_SCREENSHOTS=1 to capture");

const OUT = "../../docs/design/screenshots";
const ADMIN = { email: "admin@e2e.test", password: "e2e-admin-password-1" };

async function signIn(page: Page) {
  await page.goto("/");
  await page.getByLabel("Email").fill(ADMIN.email);
  await page.getByLabel("Password").fill(ADMIN.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

async function mode(page: Page, name: RegExp) {
  await page.goto("/settings?tab=demo");
  await page.getByRole("radio", { name }).check();
  await expect(page.getByText(/^Current behaviour:/)).toBeVisible();
}

async function send(page: Page, scenario: string, count = 1) {
  await page.goto("/settings?tab=demo");
  await page.getByLabel("Scenario").selectOption({ label: scenario });
  await page.getByLabel("How many").fill(String(count));
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await expect(page.getByRole("link", { name: "Open" }).first()).toBeVisible();
}

test("capture documentation screenshots", async ({ page }) => {
  test.setTimeout(180_000);
  await page.setViewportSize({ width: 1360, height: 900 });
  await signIn(page);

  await mode(page, /^Healthy/);
  await send(page, "Valid order", 6);
  await send(page, "Malformed");
  await send(page, "Filtered");
  await mode(page, /^Flaky/);
  await send(page, "Valid order", 2);
  await mode(page, /^Error 500/);
  await send(page, "Valid order");
  await page.getByRole("link", { name: "Open" }).first().click();
  const failedUrl = page.url();
  await expect(page.locator("h1").getByText("Dead-lettered")).toBeVisible({ timeout: 60_000 });
  await page.screenshot({ path: `${OUT}/event-dead-lettered.png`, fullPage: true });

  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByText("Rule-based diagnosis.")).toBeVisible();
  await page.screenshot({ path: `${OUT}/investigation.png`, fullPage: true });

  await mode(page, /^Healthy/);
  await page.screenshot({ path: `${OUT}/demo-tools.png`, fullPage: true });
  await page.goto(failedUrl);
  await page.getByRole("button", { name: "Replay…" }).click();
  await page.getByRole("dialog").getByLabel("Reason").fill("Destination recovered");
  await page.screenshot({ path: `${OUT}/replay-dialog.png` });
  await page.getByRole("dialog").getByRole("button", { name: "Replay event" }).click();
  await expect(page.locator("h1").getByText("Delivered")).toBeVisible({ timeout: 30_000 });
  await page.screenshot({ path: `${OUT}/event-replayed.png`, fullPage: true });

  await page.goto("/");
  await expect(page.getByText("Success rate")).toBeVisible();
  await page.screenshot({ path: `${OUT}/overview.png`, fullPage: true });
  await page.goto("/events");
  await expect(page.getByRole("table")).toBeVisible();
  await page.screenshot({ path: `${OUT}/events.png`, fullPage: true });
  await page.goto("/integrations");
  await page.getByRole("link", { name: "Storefront orders to fulfilment" }).click();
  await page.getByRole("tab", { name: "Configuration" }).click();
  await expect(page.getByRole("heading", { name: "Mappings" })).toBeVisible();
  await page.screenshot({ path: `${OUT}/integration-editor.png`, fullPage: true });
  await page.goto("/audit");
  await expect(page.getByRole("table")).toBeVisible();
  await page.screenshot({ path: `${OUT}/audit-log.png`, fullPage: true });

  // Dark theme and a narrow viewport, for the visual check only.
  await page.emulateMedia({ colorScheme: "dark" });
  await page.goto("/");
  await expect(page.getByText("Success rate")).toBeVisible();
  await page.screenshot({ path: "test-results/overview-dark.png", fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({ path: "test-results/overview-mobile-dark.png", fullPage: true });
});

test("visual check: trend chart layout with several buckets (mocked response, not saved to docs)", async ({
  page,
}) => {
  await signIn(page);
  const real = await (await page.request.get("/api/v1/dashboard/summary?window=24h")).json();
  const now = Date.now();
  const points = Array.from({ length: 24 }, (_, i) => {
    const received =
      [3, 0, 1, 8, 14, 22, 31, 18, 9, 4, 2, 0, 6, 12, 27, 40, 33, 21, 15, 7, 5, 3, 11, 19][i] ?? 0;
    const failureShare = [
      0.1, 0, 0, 0.25, 0.07, 0.05, 0.2, 0, 0.1, 0, 0.5, 0, 0, 0.1, 0.04, 0.3, 0.06, 0, 0.13, 0, 0.2, 0, 0.1,
      0.05,
    ];
    const failed = Math.round(received * (failureShare[i] ?? 0));
    const delivered = Math.max(0, received - failed - (i % 5 === 0 ? 1 : 0));
    return { at: new Date(now - (23 - i) * 3600_000).toISOString(), received, delivered, failed };
  });
  await page.route("**/api/v1/dashboard/summary*", (route) =>
    route.fulfill({ json: { ...real, series: { bucket: "hour", points } } }),
  );
  await page.setViewportSize({ width: 1360, height: 900 });
  await page.goto("/");
  await expect(page.getByRole("img", { name: /Events received per hour/ })).toBeVisible();
  await page.locator(".chart rect").nth(15).hover();
  await page
    .locator(".card", { hasText: "Events per hour" })
    .screenshot({ path: "test-results/chart-light.png" });
  await page.emulateMedia({ colorScheme: "dark" });
  await page
    .locator(".card", { hasText: "Events per hour" })
    .screenshot({ path: "test-results/chart-dark.png" });
});
