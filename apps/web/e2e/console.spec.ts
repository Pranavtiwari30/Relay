import { expect, test, type Page } from "@playwright/test";

// Accounts created by scripts/e2e-stack.sh in a disposable database.
const OPERATOR = { email: "operator@e2e.test", password: "e2e-operator-password-1" };
const VIEWER = { email: "viewer@e2e.test", password: "e2e-viewer-password-1" };
const ADMIN = { email: "admin@e2e.test", password: "e2e-admin-password-1" };

async function signIn(page: Page, account: { email: string; password: string }) {
  await page.goto("/");
  await page.getByLabel("Email").fill(account.email);
  await page.getByLabel("Password").fill(account.password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Overview" })).toBeVisible();
}

async function setDestinationMode(page: Page, mode: RegExp) {
  await page.goto("/settings?tab=demo");
  await page.getByRole("radio", { name: mode }).check();
  await expect(page.getByRole("radio", { name: mode })).toBeChecked();
  // Wait for the server to confirm before sending events that depend on it.
  await expect(page.getByText(/^Current behaviour:/)).toBeVisible();
}

/** Send one simulated event and open its detail page. */
async function sendAndOpen(page: Page, scenario = "Valid order") {
  await page.goto("/settings?tab=demo");
  await page.getByLabel("Scenario").selectOption({ label: scenario });
  await page.getByRole("button", { name: "Send", exact: true }).click();
  await page.getByRole("link", { name: "Open" }).first().click();
  await expect(page.getByRole("heading", { name: /^Event/ })).toBeVisible();
}

test("sign-in rejects bad credentials with a clear message", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("Email").fill(OPERATOR.email);
  await page.getByLabel("Password").fill("definitely-not-the-password");
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("alert")).toContainText("don’t match an account");
  await expect(page.getByRole("heading", { name: "Sign in to Relay" })).toBeVisible();
});

test("an event flows from the simulated source to the destination and is shown as delivered", async ({
  page,
}) => {
  await signIn(page, OPERATOR);
  await expect(page.getByRole("note")).toContainText("Demo workspace");
  await setDestinationMode(page, /^Healthy/);
  await sendAndOpen(page);

  await expect(page.locator("h1").getByText("Delivered")).toBeVisible();
  await expect(page.getByText("Attempt 1 succeeded — HTTP 201")).toBeVisible();
  // Both payloads are shown, and the card number from the source is redacted.
  const received = page.getByLabel("Received from source");
  await expect(received).toContainText('"card_number": "[REDACTED]"');
  await expect(received).not.toContainText("4111");
  await expect(page.getByLabel("Sent to destination")).toContainText('"order_id"');
  // A delivered event cannot be replayed.
  await expect(page.getByRole("button", { name: "Replay…" })).toBeDisabled();

  await page.getByRole("link", { name: "Overview" }).click();
  await expect(page.getByText("Success rate")).toBeVisible();
  await expect(page.locator(".tile", { hasText: "Delivered" }).locator(".tile-value")).not.toHaveText("0");
});

test("a failing destination dead-letters the event; an operator investigates and replays it", async ({
  page,
}) => {
  await signIn(page, OPERATOR);
  await setDestinationMode(page, /^Error 500/);
  await sendAndOpen(page);
  const eventUrl = page.url();

  // Retries are exhausted (4 attempts with backoff), then the event is dead-lettered.
  await expect(page.locator("h1").getByText("Dead-lettered")).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText("Attempt 4 failed — HTTP 503")).toBeVisible();
  await page.goto("/failures");
  await expect(page.getByRole("cell", { name: "destination responded with HTTP 503" }).first()).toBeVisible();

  // Investigate: no model is configured, so the rule-based diagnosis runs.
  await page.goto(eventUrl);
  await page.getByRole("button", { name: "Investigate" }).click();
  await expect(page.getByRole("heading", { name: "Investigation" })).toBeVisible();
  await expect(page.getByText("Rule-based diagnosis.")).toBeVisible();
  await expect(page.getByText("Destination unavailable")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Likely cause (hypothesis)" })).toBeVisible();
  await expect(page.locator(".chip", { hasText: "attempt:" }).first()).toBeVisible();

  // Approving a recommendation records a decision; it does not replay anything.
  await page.getByRole("button", { name: "Approve" }).last().click();
  await expect(page.getByText(/Approval is a record only/)).toBeVisible();
  await page.goto(eventUrl);
  await expect(page.locator("h1").getByText("Dead-lettered")).toBeVisible();

  // Fix the destination, then replay explicitly with a reason.
  await setDestinationMode(page, /^Healthy/);
  await page.goto(eventUrl);
  await page.getByRole("button", { name: "Replay…" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("recorded in the audit log");
  await expect(dialog.getByRole("button", { name: "Replay event" })).toBeDisabled();
  await dialog.getByLabel("Reason").fill("Destination recovered");
  await dialog.getByRole("button", { name: "Replay event" }).click();

  await expect(page.locator("h1").getByText("Delivered")).toBeVisible({ timeout: 30_000 });
  // History is preserved: the four failed attempts are still there beside the new one.
  await expect(page.getByText("Attempt 4 failed — HTTP 503")).toBeVisible();
  await expect(page.getByText("Attempt 1 (replay cycle 2) succeeded — HTTP 201")).toBeVisible();
  await expect(page.getByRole("heading", { name: "Replays" })).toBeVisible();

  await page.goto("/audit?action=event.replay");
  await expect(
    page.getByRole("row", { name: /operator@e2e\.test.*event\.replay.*Destination recovered/ }),
  ).toBeVisible();
});

test("a malformed event is rejected with actionable errors and never delivered", async ({ page }) => {
  await signIn(page, OPERATOR);
  await sendAndOpen(page, "Malformed");
  await expect(page.locator("h1").getByText("Validation failed")).toBeVisible();
  await expect(page.getByRole("heading", { name: /Validation issues/ })).toBeVisible();
  await expect(page.getByText("order_no", { exact: true })).toBeVisible();
  await expect(page.getByText("required field is missing")).toBeVisible();
  await expect(page.getByText("No delivery attempts")).toBeVisible();
});

test("a viewer can read but cannot replay — in the UI or by calling the API directly", async ({
  page,
  browser,
}) => {
  // An operator (separate browser session) produces a replayable failed event.
  const operatorContext = await browser.newContext();
  const operatorPage = await operatorContext.newPage();
  await signIn(operatorPage, OPERATOR);
  await sendAndOpen(operatorPage, "Malformed");
  const eventPath = new URL(operatorPage.url()).pathname;
  await expect(operatorPage.getByRole("button", { name: "Replay…" })).toBeEnabled();
  await operatorContext.close();

  await signIn(page, VIEWER);
  await page.goto(eventPath);
  await expect(page.getByRole("heading", { name: /^Event/ })).toBeVisible();
  await expect(page.locator("h1").getByText("Validation failed")).toBeVisible();
  await expect(page.getByRole("button", { name: "Replay…" })).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Investigate" })).toHaveCount(0);
  await expect(page.getByText("Your role can view this event but not replay it.")).toBeVisible();

  // The UI hiding a button is not the control. The API must refuse too.
  const eventId = page.url().split("/").pop();
  const session = await (await page.request.get("/api/v1/auth/me")).json();
  const response = await page.request.post(`/api/v1/events/${eventId}/replay`, {
    data: { reason: "viewer trying anyway" },
    headers: {
      "X-CSRF-Token": session.csrf_token,
      "Idempotency-Key": "viewer-attempt-0001",
      Origin: new URL(page.url()).origin,
    },
  });
  expect(response.status()).toBe(403);
  expect((await response.json()).error.code).toBe("permission_denied");

  await page.goto("/settings?tab=members");
  await expect(page.getByText("Only administrators can add members or change roles.")).toBeVisible();
  await page.goto("/integrations");
  await expect(page.getByRole("link", { name: "New integration" })).toHaveCount(0);
});

test("an administrator validates, tests and saves a new configuration version", async ({ page }) => {
  await signIn(page, ADMIN);
  await page.goto("/integrations");
  await page.getByRole("link", { name: "Storefront orders to fulfilment" }).click();
  await page.getByRole("tab", { name: "Configuration" }).click();

  await page.getByRole("tab", { name: "Test with a sample" }).click();
  await page.getByRole("button", { name: "Run test" }).click();
  await expect(page.getByText("This payload would be delivered.")).toBeVisible();
  await expect(page.getByLabel("Payload the destination would receive")).toContainText('"sku": "KB-100"');

  // Break a mapping: validation explains what is wrong and where.
  await page.getByRole("tab", { name: "Form" }).click();
  await page.getByLabel("Mapping 1 source path").fill("order_no..oops");
  await page.getByRole("button", { name: "Validate" }).click();
  await expect(page.getByText("This configuration cannot be activated yet.")).toBeVisible();
  await expect(page.getByText("mappings[0].source")).toBeVisible();

  // Fix it and save: a new immutable version is created.
  await page.getByLabel("Mapping 1 source path").fill("order_no");
  await page.getByLabel("Maximum delivery attempts").fill("3");
  await page.getByLabel("Change note").fill("Fewer attempts for the demo");
  await page.getByRole("button", { name: "Save as new version" }).click();
  await expect(page.getByText(/Saved as version 2/)).toBeVisible();
  await page.getByRole("tab", { name: "Version history" }).click();
  await expect(page.getByRole("row", { name: /v2 current.*Fewer attempts for the demo/ })).toBeVisible();
  await expect(page.getByRole("row", { name: /v1.*Seeded demo configuration/ })).toBeVisible();
});

test("keyboard users get a skip link and visible focus", async ({ page }) => {
  await signIn(page, VIEWER);
  await page.keyboard.press("Tab");
  const skip = page.getByRole("link", { name: "Skip to content" });
  await expect(skip).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page).toHaveURL(/#main$/);
});
