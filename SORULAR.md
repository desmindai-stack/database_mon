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

## Faz 3 — Node'da veritabanı kimlik bilgisi yok

Kullanıcının verdiği `Node` şeması (id, group_id, name, host, port, site,
role_hint, agent_url, agent_token, options) veritabanına bağlanmak için
username/password içermiyor — `Instance` modelinin aksine. Ama parametre
denetimi (`pg_settings` okumak) gerçek bir SQL bağlantısı gerektiriyor.

**Varsayım:** `node.options` JSON'ına üç opsiyonel anahtar ekledim:
`db_username`, `db_password`, `db_database` (yoksa `db_database` varsayılan
`"postgres"`). `db_username` tanımlı değilse endpoint 400 ile açık bir hata
mesajı döner (`Node '{name}' için node.options.db_username tanımlı değil`).

**Bilinen sınır:** Bu değerler `Instance.password` gibi
`credentials.encrypt_secret` ile şifrelenmiyor — `options` JSON'ı zaten
şifrelenmeden saklanıyor (`Node.agent_token` de aynı durumda). Üretimde
gerçek şifreler burada plaintext saklanmamalı; bu, Node'a da `Instance`
tarzı şifreli bir credential deposu eklemeyi gerektiren ayrı bir iş —
kapsam dışı bıraktım ama not ediyorum.
