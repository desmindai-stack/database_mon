import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { ApiHelper, authenticated, expect, tab, test } from "./support/fixtures";

/**
 * Rozet sayısı == görünen kalem sayısı, GERÇEK veriyle (Faz 31 Commit 7).
 *
 * Belirti: Tuning rozeti "2" diyordu, sayfada 1 kalem vardı; "17 yavaş sorgu" deniyordu, listede
 * karşılığı yoktu. Veri sahte değil: `backend/scripts/e2e_collect.py` canlı PostgreSQL'de iş yükü
 * çalıştırıp gerçek toplayıcıyı e2e veritabanına karşı koşuyor (kısıtlı pg_monitor rolü).
 *
 * DBACE_TEST_PG_DSN yoksa atlanıyor (`python scripts/live_pg.py up`).
 */
test.use(authenticated);

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const BACKEND = join(FRONTEND, "..", "backend");
const DB_PATH = join(BACKEND, "data", "dbace_e2e.db");
const PG = (process.env.DBACE_TEST_PG_DSN ?? "").split(",")[0]?.trim() ?? "";
const PYTHON =
  process.env.PYTHON ??
  (existsSync(join(BACKEND, ".venv", "Scripts", "python.exe"))
    ? join(BACKEND, ".venv", "Scripts", "python.exe")
    : existsSync(join(BACKEND, ".venv", "bin", "python"))
      ? join(BACKEND, ".venv", "bin", "python")
      : "python");

test.describe("@live rozet = görünen kalem (gerçek veri)", () => {
  test.skip(!PG, "DBACE_TEST_PG_DSN yok — python scripts/live_pg.py up");

  test("Tuning rozeti bulgu satırlarıyla, yavaş sorgu bulgusu listeyle aynı; kalem ayrıntısı dolu", async ({ page, request }) => {
    test.setTimeout(120_000);
    const url = new URL(PG);
    const api = new ApiHelper(request);
    const instance = await api.createInstance({
      host: url.hostname,
      port: Number(url.port),
      database: url.pathname.replace(/^\//, ""),
      username: "dbace_it_monitor",
      password: "dbace_it_pw",
    });
    execFileSync(PYTHON, ["scripts/e2e_collect.py", String(instance.id), PG], {
      cwd: BACKEND,
      env: { ...process.env, DATABASE_URL: `sqlite+aiosqlite:///${DB_PATH.replace(/\\/g, "/")}`, PYTHONIOENCODING: "utf-8" },
      stdio: "inherit",
    });

    await page.goto(`/instances/${instance.id}?tab=tuning`);
    const issueRows = page.locator(".insight-row.critical, .insight-row.high, .insight-row.medium");
    const slowInsight = page.locator(".insight-row").filter({ hasText: /yavaş ortalama süreli sorgu/ });
    await expect(slowInsight).toHaveCount(1);
    const visibleIssues = await issueRows.count();
    await expect(tab(page, "Tuning").locator(".tab-badge")).toHaveText(String(visibleIssues));
    await expect(page.locator(".tuning-hero-note")).toContainText(`${visibleIssues} aksiyon gerektiren bulgu`);

    const title = (await slowInsight.locator(".insight-title strong").textContent()) ?? "";
    const claimed = Number(title.trim().split(" ")[0]);
    expect(claimed).toBeGreaterThan(0);

    // Bulgunun bağlantısı, sayının hesaplandığı liste görünümünü açıyor (ortalama süreye göre ilk 20).
    await slowInsight.getByRole("button", { name: /İlgili sekmeye git/ }).click();
    await expect(page).toHaveURL(/tab=queries/);
    // Liste bölümü katlanmış gelebiliyor (durum/localStorage) — başlıktaki sayı listenin kendisinden.
    const section = page.getByRole("button", { name: /^En sorunlu sorgular \(\d+\)/ });
    if ((await section.getAttribute("aria-expanded")) !== "true") await section.click();
    // Listenin kendi açıklaması hangi görünümde olduğunu söylüyor (sıralama düğmeleri ayrı, katlanabilir bölümde).
    await expect(page.getByText(/ortalama süreye göre ilk 20/)).toBeVisible();
    const rows = page.locator("tr.query-row");
    await expect(rows.first()).toBeVisible();
    const means = await rows.locator("td:nth-child(3)").allTextContents();
    expect(means.filter((m) => Number(m) >= 50).length).toBe(claimed);

    // Gizlenen kalemler nedeniyle yazılı.
    await expect(page.getByText(/sistem\/platform sorgusu/)).toBeVisible();

    // Bir kaleme tıklanınca ayrıntı dolu.
    await rows.first().click();
    const detail = page.locator("tr.query-expanded");
    await expect(detail.locator("pre")).toContainText("orders");
    await expect(detail.getByText("Sorgu bazında I/O ve CPU")).toBeVisible();

    await api.delete(`/api/instances/${instance.id}?cascade=true`);
  });
});
