import { test, expect } from "@playwright/test";

test("main shell loads without bootstrap lock", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Minion" })).toBeVisible();
  await expect(page.locator(".bootstrap-overlay")).toBeHidden({ timeout: 60_000 });
  await expect(page.getByText("Drop files or folders")).toBeVisible();
});

test("settings status shows live sidecar counts", async ({ page }) => {
  await page.goto("/");
  await expect(page.locator(".bootstrap-overlay")).toBeHidden({ timeout: 60_000 });
  await page.getByRole("button", { name: "Settings" }).click();
  const dialog = page.getByRole("dialog", { name: "Minion preferences" });
  await expect(dialog).toBeVisible();
  await expect(page.getByRole("heading", { name: "Status" })).toBeVisible();
  await expect(dialog.getByText("Sources", { exact: true })).toBeVisible();
  await expect(dialog.getByText("Chunks", { exact: true })).toBeVisible();
  const metricValues = dialog.locator(".status-summary-grid .status-card-v");
  await expect(metricValues).toHaveCount(4);
  await expect(metricValues.nth(0)).toHaveText(/^[0-9]+$/, { timeout: 60_000 });
  await expect(metricValues.nth(1)).toHaveText(/^[0-9]+$/);
});
