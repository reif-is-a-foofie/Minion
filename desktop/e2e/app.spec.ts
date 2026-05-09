import { test, expect } from "@playwright/test";

const API_PORT = process.env.E2E_API_PORT ?? "9876";

test.describe("Minion desktop UI (browser + sidecar)", () => {
  test("main shell and drop zone visible", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Minion" })).toBeVisible();
    await expect(page.getByText("Drop files or folders")).toBeVisible();
    await expect(page.getByRole("button", { name: "Settings" })).toBeVisible();
    await expect(page.getByText("Activity")).toBeVisible();
  });

  test("settings hub navigates library, identity, claude, support", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    const hub = page.getByRole("dialog", { name: "Minion preferences" });
    await expect(hub).toBeVisible();

    const nav = page.getByRole("complementary", { name: "Sections" });
    await nav.getByRole("button", { name: "Library & search" }).click();
    await expect(hub.getByText("Indexed sources")).toBeVisible();

    await nav.getByRole("button", { name: "Identity", exact: true }).click();
    await expect(hub.getByRole("button", { name: "Proposed" })).toBeVisible();
    await expect(hub.getByRole("button", { name: "Active" })).toBeVisible();

    await nav.getByRole("button", { name: "Claude (MCP)" }).click();
    await expect(hub.getByRole("button", { name: "Add to Claude" })).toBeVisible();

    await nav.getByRole("button", { name: "Support" }).click();
    await expect(hub.getByText("About", { exact: true }).first()).toBeVisible({ timeout: 25_000 });

    await hub.getByRole("button", { name: "Close" }).click();
    await expect(hub).not.toBeVisible();
  });

  test("library search POST completes against sidecar", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("button", { name: "Settings" }).click();
    const hub = page.getByRole("dialog", { name: "Minion preferences" });
    await page.getByRole("complementary", { name: "Sections" }).getByRole("button", { name: "Library & search" }).click();

    const resp = page.waitForResponse(
      (r) => r.url().includes("/search") && r.request().method() === "POST" && r.ok(),
      { timeout: 30_000 },
    );
    await hub.getByPlaceholder("Search your memory…").fill("cursor automated qa");
    await hub.getByRole("button", { name: "Search", exact: true }).click();
    await resp;
  });

  test("sidecar capabilities JSON", async ({ request }) => {
    const r = await request.get(`http://127.0.0.1:${API_PORT}/capabilities`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.service).toBe("minion-api");
  });
});
