# Sorular / Varsayımlar

Karar veremediğim veya kapsam belirsizliği olan noktalar burada; her biri için
makul bir varsayımla devam ettim.

## Faz 2 — Grup seviyeli alert kalıcılığı

`AlertRule`/`AlertEvent` modelleri `instance_id`'ye bağlı (nullable ama bir
`Instance` satırına referans veriyor); `DatabaseGroup`'un tek bir temsilci
`Instance`'ı yok. Bu yüzden `replication_lag_bytes`, `etcd_quorum_lost`,
`split_brain`, `node_down` flag'lerini `GET /api/groups/{id}/health`
yanıtında hesaplayıp döndürdüm ve `alert_engine.GROUP_RULE_SPECS` içine
metrik kataloğu olarak ekledim, ama bunları kalıcı `AlertEvent` satırlarına
yazan bir evaluate/ensure fonksiyonu yazmadım.

**Varsayım:** Bunu kalıcı hale getirmek `AlertRule`/`AlertEvent`'e
`group_id` eklemeyi gerektirir — bu Faz 1'de tanımlanmayan bir şema
değişikliği olur. Faz 2'nin isteği "flag'leri genişlet" idi; ben bunu
"katalog + canlı health yanıtında hesapla" olarak yorumladım. Grup
seviyeli alarmların e-posta/UI'da kalıcı olarak görünmesi isteniyorsa
ayrı bir faz olarak ele alınmalı.

## Faz 2 — Node servis seçimi

`Node` modelinde `services` alanı yok (kullanıcı şemasında belirtilmedi).
`_node_services()` şu mantığı kullanıyor: `node.options.services` override
edilmişse onu kullan; yoksa `group.topology == "patroni"` ise tam Patroni
yığınını (`etcd, patroni, postgresql, keepalived, haproxy`), değilse
(standalone/alwayson) sadece `postgresql` (genel TCP port testi) prob'la.

**Varsayım:** SQL Server Always On düğümleri için Faz 2'nin ürettiği
`postgresql` servis adı yalnızca "veritabanı portu açık mı" testidir,
motor adı değil — Faz 4'teki `alwayson_health.py` asıl AG sağlığını
DMV'lerle ayrı bir endpoint'te verecek.
