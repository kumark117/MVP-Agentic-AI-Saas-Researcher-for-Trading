import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";

import { login, registerAndVerify } from "./helpers";

const TAGS = ["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"];

async function check(page: Page, path: string): Promise<void> {
  await page.goto(path);
  await page.waitForLoadState("networkidle");
  const r = await new AxeBuilder({ page }).withTags(TAGS).analyze();
  expect(r.violations, `${path}: ${r.violations.map((v) => v.id).join(", ")}`).toEqual([]);
}

test("main screens have no detectable WCAG 2.2 AA violations", async ({ page }) => {
  for (const path of ["/login", "/register", "/about"]) await check(page, path);
  const email = await registerAndVerify(page, "a11y");
  await login(page, email);
  for (const path of ["/research", "/account", "/security"]) await check(page, path);
});

test("the research form works with the keyboard only", async ({ page }) => {
  const email = await registerAndVerify(page, "kbd");
  await login(page, email);
  const metal = page.getByLabel("Metal");
  const venue = page.getByLabel("Venue");
  await expect(metal.locator("option")).not.toHaveCount(1); // catalogue loaded
  await metal.focus();
  await page.keyboard.press("ArrowDown");
  await expect(venue).toBeEnabled(); // the dependent field unlocks after the choice
  await page.keyboard.press("Tab");
  await expect(venue).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByRole("radio").first()).toBeEnabled();
  await page.keyboard.press("Tab");
  await page.keyboard.press("Space"); // select the first instrument radio
  const horizon = page.getByLabel("Horizon");
  for (let i = 0; i < 8; i++) {
    if (await horizon.evaluate((el) => el === document.activeElement)) break;
    await page.keyboard.press("Tab");
  }
  await expect(horizon).toBeFocused();
  // Focus is visible on the focused control.
  const outline = await horizon.evaluate((el) => getComputedStyle(el).outlineStyle);
  expect(outline).not.toBe("none");
});
