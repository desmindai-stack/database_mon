-- Faz 18 İŞ 1: bulgunun işaret ettiği KESİN hedef.
-- Rapor bir sorgudan bahsedip "DPA'da EXPLAIN'e bakın" dediğinde, bağlantı yalnızca
-- ?tab=queries olduğu için DPA kendi varsayılan penceresinde açılıyor ve sorgu orada
-- bulunamıyordu. link_hint artık sorgu anahtarını ve raporun kullandığı pencereyi taşıyor.

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS link_hint VARCHAR(512);
