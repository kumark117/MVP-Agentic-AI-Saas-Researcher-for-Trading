import { expect, test } from "@playwright/test";

import { fillResearch, login, registerAndVerify } from "./helpers";

// All data here is SYNTHETIC (fixture providers + fake model). No real money is spent.

test("register, verify, research, watch live, report, reconnect", async ({ page, browser }) => {
  const email = await registerAndVerify(page, "journey");
  await login(page, email);
  await expect(page.getByText("₹500.00 (simulated)").first()).toBeVisible();
  await fillResearch(page);
  await page.getByRole("button", { name: "Start research" }).click();
  await expect(page).toHaveURL(/\/runs\//);
  // Reconnect mid-run: a reload resumes from persisted state and the stream.
  await page.reload();
  await expect(page.getByText(/Signal: (BUY|SELL|HOLD|NO_SIGNAL)/)).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByText(/uncalibrated, not a probability/)).toBeVisible();
  await expect(page.getByRole("heading", { name: "Usage and cost" })).toBeVisible();
  // A second browser sees the same run and state.
  const other = await browser.newContext();
  const p2 = await other.newPage();
  await login(p2, email);
  await p2.goto(page.url());
  await expect(p2.getByText(/Signal: /)).toBeVisible();
  await other.close();
});

test("cancel a run", async ({ page }) => {
  const email = await registerAndVerify(page, "cancel");
  await login(page, email);
  await fillResearch(page);
  await page.getByRole("button", { name: "Start research" }).click();
  await expect(page).toHaveURL(/\/runs\//);
  const cancel = page.getByRole("button", { name: "Cancel research" });
  if (await cancel.isVisible().catch(() => false)) {
    await expect(
      page.getByText("Provider work already started may still incur real platform cost."),
    ).toBeVisible();
    await cancel.click();
  }
  // Either the cancel won, or the synthetic run had already finished: one terminal state.
  await expect(page.getByText(/^(cancelled|succeeded|incomplete)$/)).toBeVisible({
    timeout: 60_000,
  });
});

test("double-clicked top-up gives exactly one credit", async ({ page }) => {
  const email = await registerAndVerify(page, "topup");
  await login(page, email);
  await page.goto("/account");
  await page.getByLabel("Amount (INR, simulated)").first().fill("100.00");
  await page.getByRole("button", { name: "Open demo checkout" }).dblclick();
  await expect(page.getByRole("heading", { name: "Demo checkout" })).toBeVisible();
  await expect(page.getByText(/never asks for card, bank or UPI details/)).toBeVisible();
  await page.getByRole("button", { name: "Simulate success" }).dblclick();
  await expect(page.getByText("Demo credit added (simulated).")).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "Back to account" }).click();
  await expect(page.getByText("₹600.00 (simulated)").first()).toBeVisible();
  await expect(page.getByRole("row").filter({ hasText: "topup" })).toHaveCount(1);
});

test("revoking a session from a second browser signs the first one out", async ({
  page,
  browser,
}) => {
  const email = await registerAndVerify(page, "revoke");
  await login(page, email);
  const second = await browser.newContext();
  const p2 = await second.newPage();
  await login(p2, email);
  await p2.goto("/security");
  await p2.getByRole("button", { name: "Revoke" }).first().click();
  await page.goto("/account");
  await expect(page).toHaveURL(/\/login/, { timeout: 20_000 });
  await second.close();
});
