-- Faz 18 İŞ 4: bulgunun sayısal özeti.
-- Bulgu metinleri tek uzun paragraftı; "ne kadar / neye göre" bilgisi cümlenin içinde
-- kayboluyordu. facts artık etiketli ve vurgulanabilir satırlar taşıyor, detail ise yalnızca
-- "ne oldu" cümlesi olarak kısa kalıyor.

ALTER TABLE report_findings ADD COLUMN IF NOT EXISTS facts JSONB;
