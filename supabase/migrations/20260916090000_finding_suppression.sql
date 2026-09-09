-- Faz 28 İŞ 2: bağımlılık bastırma.
--
-- Bir düğüm düştüğünde rapor 40 bulgu üretiyordu: biri gerçek ("düğüme erişilemiyor"),
-- 39'u onun sonucu. Hepsi teknik olarak doğru, hepsi pratik olarak gürültü — tek bir şey
-- düzeltilince hepsi birden kapanacak.
--
-- Bastırılan bulgu SİLİNMİYOR. Silmek, bastırma kuralı yanlışsa gerçek bir sorunu
-- görünmez yapardı; işaretlemek ise en kötü ihtimalle bir tıklama maliyeti. Bulgu raporda
-- kalıyor, sayaçlara girmiyor ve "kök sebep nedeniyle N kontrol yapılamadı" satırının
-- altından açılabiliyor.

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS is_root_cause BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS suppressed BOOLEAN NOT NULL DEFAULT FALSE;
-- Bastıran kök sebebin fingerprint'i; arayüz bulguyu kök sebebinin altına yerleştiriyor.
ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS suppressed_by VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_report_findings_suppressed
    ON report_findings (suppressed);

-- Rapor başına bastırma özeti: {"roots": [{root_fingerprint, root_label, suppressed_count,
-- reason, ...}], "suppressed_total": N}. Bulgulardan türetilebilirdi ama rapor donmuş bir
-- belge; her okuyanın yeniden hesaplaması gereksiz ve zamanla ayrışmaya açık olurdu.
ALTER TABLE health_reports ADD COLUMN IF NOT EXISTS suppression JSONB;
