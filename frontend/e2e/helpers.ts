import { expect, type Page } from "@playwright/test";

const MAILPIT = process.env.MAILPIT_URL ?? "http://localhost:8025";
export const PASSWORD = "correct horse battery staple";

export function newEmail(tag: string): string {
  const suffix = Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  return `e2e-${tag}-${suffix}@example.com`;
}

/** Read the newest verification/reset link for an address from Mailpit's API. */
export async function latestLink(email: string, path: string): Promise<string> {
  const pattern = new RegExp(`https?://\\S+${path}#token=[\\w-]+`);
  for (let i = 0; i < 40; i++) {
    const query = encodeURIComponent(`to:"${email}"`);
    const r = await fetch(`${MAILPIT}/api/v1/search?query=${query}`);
    const list = (await r.json()) as { messages?: { ID: string }[] };
    for (const m of list.messages ?? []) {
      const msg = (await (await fetch(`${MAILPIT}/api/v1/message/${m.ID}`)).json()) as {
        Text: string;
      };
      const match = pattern.exec(msg.Text);
      if (match) return match[0];
    }
    await new Promise((res) => setTimeout(res, 500));
  }
  throw new Error(`no ${path} email for ${email}`);
}

export async function registerAndVerify(page: Page, tag: string): Promise<string> {
  const email = newEmail(tag);
  await page.goto("/register");
  await page.getByLabel("Name").fill("E2E Tester");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  // The API rate-limits registration per IP (5 a minute); back off like a person would.
  for (let attempt = 0; attempt < 8; attempt++) {
    await page.getByRole("button", { name: "Create account" }).click();
    const done = page.getByText("Check your email");
    const limited = page.getByText("Too many requests");
    await expect(done.or(limited)).toBeVisible();
    if (await done.isVisible()) break;
    await page.waitForTimeout(15_000);
  }
  await expect(page.getByText("Check your email")).toBeVisible();
  const link = new URL(await latestLink(email, "/verify-email"));
  await page.goto(link.pathname + link.hash);
  await expect(page.getByText("Your email is verified")).toBeVisible();
  return email;
}

export async function login(page: Page, email: string): Promise<void> {
  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(PASSWORD);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByRole("heading", { name: "Research", exact: true })).toBeVisible();
}

export async function fillResearch(page: Page): Promise<void> {
  await page.getByLabel("Metal").selectOption("gold");
  await page.getByLabel("Venue").selectOption("COMEX");
  await page.getByRole("radio").first().check();
  await page.getByLabel("Horizon").selectOption("swing");
  await page.getByRole("button", { name: "Get estimate" }).click();
  await expect(page.getByText("Maximum charge")).toBeVisible();
}
