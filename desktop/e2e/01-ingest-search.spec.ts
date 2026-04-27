import { test, expect } from "@playwright/test";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { E2E_API_BASE } from "./constants";

const repoRoot = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const fixtureNote = join(repoRoot, "chatgpt_mcp_memory", "tests", "fixtures", "note.md");

test("ingest fixture via API then search from UI", async ({ page, request }) => {
  const st = await request.get(`${E2E_API_BASE}/status`);
  expect(st.ok()).toBeTruthy();

  const ing = await request.post(`${E2E_API_BASE}/ingest`, {
    data: { path: fixtureNote },
    headers: { "content-type": "application/json" },
  });
  expect(ing.ok(), await ing.text()).toBeTruthy();

  await expect.poll(async () => {
    const r = await request.get(`${E2E_API_BASE}/sources`);
    if (!r.ok()) return 0;
    const j = (await r.json()) as { sources?: unknown[] };
    return j.sources?.length ?? 0;
  }, { timeout: 120_000, intervals: [500, 1000, 2000] }).toBeGreaterThanOrEqual(1);

  await page.goto("/");
  await expect(page.locator(".bootstrap-overlay")).toBeHidden({ timeout: 60_000 });
  await page.getByRole("button", { name: "Settings" }).click();
  await page.getByRole("button", { name: "Library & search" }).click();

  const searchInput = page.getByPlaceholder("Search your memory…");
  await searchInput.fill("Good Capital owns this repo");
  await page.getByRole("button", { name: "Search", exact: true }).click();

  await expect(page.getByText("Good Capital", { exact: false }).first()).toBeVisible({ timeout: 120_000 });
});
