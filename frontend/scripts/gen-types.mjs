/**
 * Backend'in OpenAPI şemasından TypeScript tipleri üretir (Faz 21 İŞ 2).
 *
 * Neden var: `src/api.ts` içindeki tipler ELLE yazılmıştı ve API'yi tam yansıtmıyordu. Bu
 * doğrudan canlı çökmelere yol açtı — en somutu `ReportFinding.facts`: tip "her zaman var"
 * diyordu, şemada alan hiç tanımlı olmadığı için API onu HİÇ döndürmüyordu, arayüz
 * `.length` okuyunca patlıyordu. Derleyici yakalayamadı çünkü tip yalan söylüyordu.
 *
 * Üretilen dosya elle düzenlenmez; kaynağı backend'in kendisidir.
 *
 * Kullanım:
 *   npm run gen:types        → backend'i ÇALIŞTIRMADAN üretir (python gerekir)
 *   npm run gen:types:live   → çalışan bir backend'in /openapi.json ucundan üretir
 */

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const FRONTEND = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(FRONTEND, "..");
const SPEC = join(FRONTEND, "openapi.json");
const OUT = join(FRONTEND, "src", "api-types.ts");

const BANNER = `/**
 * OTOMATİK ÜRETİLDİ — ELLE DÜZENLEMEYİN.
 *
 * Kaynak: backend'in OpenAPI şeması (FastAPI \`app.openapi()\`).
 * Yeniden üretmek için:  npm run gen:types
 *
 * Bu dosya, elle yazılmış tiplerin API'den sessizce ayrışması sonucu yaşanan canlı
 * çökmelere karşı var. Bir alan burada yoksa API onu döndürmüyordur; opsiyonel
 * görünüyorsa gerçekten opsiyoneldir. Elle "düzeltmek" tam olarak kaçınılmak istenen
 * duruma geri döner.
 *
 * Değiştirmek isterseniz backend'deki Pydantic şemasını değiştirin, sonra bu komutu
 * çalıştırın. CI, üretilen çıktı commit'lenmiş hâlinden farklıysa kırmızı olur.
 */
`;

const live = process.argv.includes("--live");

if (live) {
  const url = process.env.DBACE_API_URL ?? "http://localhost:8000";
  console.log(`OpenAPI şeması indiriliyor: ${url}/openapi.json`);
  const response = await fetch(`${url}/openapi.json`);
  if (!response.ok) {
    console.error(`Şema alınamadı (HTTP ${response.status}). Backend çalışıyor mu?`);
    process.exit(1);
  }
  const spec = await response.json();
  // Anahtarları sıralı yaz: çıktı sıraya göre oynamasın, yoksa CI'daki sürüklenme kontrolü
  // kod değişmeden de kırmızı olurdu (backend'deki dump_openapi.py da aynısını yapıyor).
  writeFileSync(SPEC, `${JSON.stringify(spec, sortKeys(spec), 2)}\n`, "utf8");
} else {
  const python = process.env.PYTHON ?? defaultPython();
  console.log(`OpenAPI şeması üretiliyor: ${python} scripts/dump_openapi.py`);
  execFileSync(python, ["scripts/dump_openapi.py", SPEC], {
    cwd: join(ROOT, "backend"),
    stdio: "inherit",
  });
}

// CLI'ın JS giriş noktasını mevcut Node ile çalıştırıyoruz. İki alternatif de sorunlu:
// `npx` + `shell: true` DEP0190 uyarısı veriyor, `.bin/*.cmd` ise Windows'ta shell olmadan
// spawn edilemiyor (Node 20+ EINVAL). Bu yol her platformda aynı şekilde çalışıyor.
const cli = join(FRONTEND, "node_modules", "openapi-typescript", "bin", "cli.js");
if (!existsSync(cli)) {
  console.error("openapi-typescript kurulu değil. Önce `npm ci` çalıştırın.");
  process.exit(1);
}
execFileSync(process.execPath, [cli, SPEC, "-o", OUT], { cwd: FRONTEND, stdio: "inherit" });

// openapi-typescript kendi kısa başlığını yazıyor; onun üstüne bu projeye özgü açıklamayı
// koyuyoruz ki dosyayı açan kişi NEDEN üretildiğini de görsün.
const generated = readFileSync(OUT, "utf8");
writeFileSync(OUT, BANNER + generated, "utf8");
console.log(`${OUT} yazıldı.`);

/** JSON.stringify replacer: nesne anahtarlarını sıralar (deterministik çıktı için). */
function sortKeys() {
  return (_key, value) =>
    value && typeof value === "object" && !Array.isArray(value)
      ? Object.fromEntries(Object.entries(value).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0)))
      : value;
}

function defaultPython() {
  const venv = join(ROOT, "backend", ".venv", process.platform === "win32" ? "Scripts/python.exe" : "bin/python");
  return existsSync(venv) ? venv : "python";
}
