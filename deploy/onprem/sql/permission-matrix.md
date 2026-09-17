# dbace yetki matrisi (ÜRETİLEN — elle düzenlemeyin)

`python backend/scripts/permission_inventory.py --measure --write` ile üretilir. Nesneler dbace'in hedef
veritabanında GERÇEKTEN çalıştırdığı SQL'den (AST) çıkarılır. Ölçüm PAKETİN rol/login SQL'iyle kurulan
`dbace_monitor` ile yapılır (PostgreSQL: pg_monitor + tablolarda SELECT, TEMP/CREATE YOK; SQL Server:
VIEW SERVER STATE + VIEW DATABASE STATE). **Gereken yetki** de ölçümden: nesne en az yetkiliden paketin
rolüne sırayla okunur (PostgreSQL: yetkisiz → yalnızca SELECT → yalnızca pg_monitor → paket; SQL Server:
yetkisiz → yalnızca VIEW SERVER STATE → paket) ve paket rolüyle AYNI sonucu (satır sayısı, maskelenen satır,
NULL alan; ölçüm sırasında değişen nesnelerde hata/boş/var/maskeli) veren İLK rolün yetkisi yazılır.
Rol kurulumu: `postgresql-monitor-role.sql`, `sqlserver-monitor-login.sql`.

| Motor | Nesne | Kullanan modüller | Gereken yetki (ölçülen) | PG 15 | PG 16 | PG 17 / SQL Server |
|---|---|---|---|---|---|---|
| postgresql | `hypopg_create_index` | services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `hypopg_drop_index` | services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `hypopg_get_indexdef` | services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_attribute` | services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_blocking_pids` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_class` | collectors/postgresql, services/index_advice_outcome, services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_current_wal_lsn` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_database` | collectors/postgresql, services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_database_size` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_extension` | collectors/postgresql, services/index_advisor, services/pgss, services/prerequisites | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_get_indexdef` | collectors/postgresql, services/index_advice_outcome | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_index` | collectors/postgresql, services/index_advice_outcome | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_indexes` | services/index_advice_outcome, services/index_advisor | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_is_in_recovery` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_last_wal_receive_lsn` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_last_wal_replay_lsn` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_locks` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_namespace` | collectors/postgresql, services/index_advice_outcome, services/index_advisor, services/pgss | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_prepared_statements` | services/generic_plan | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_relation_size` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_replication_slots` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_settings` | services/config_comparison, services/parameter_audit | pg_monitor | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_activity` | collectors/postgresql, services/prerequisites | pg_monitor | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_archiver` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_bgwriter` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_checkpointer` | collectors/postgresql | ek yetki yok (PUBLIC) | sürümde yok | sürümde yok | ✅ |
| postgresql | `pg_stat_database` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_io` | collectors/postgresql | ek yetki yok (PUBLIC) | sürümde yok | ✅ | ✅ |
| postgresql | `pg_stat_progress_basebackup` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_replication` | collectors/postgresql | pg_monitor | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_statements` | collectors/postgresql, services/pgss, services/plan_source | pg_monitor | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_user_indexes` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_user_tables` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stat_wal_receiver` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_statio_user_tables` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_stats` | services/index_advisor | tabloda SELECT | ✅ | ✅ | ✅ |
| postgresql | `pg_table_size` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| postgresql | `pg_wal_lsn_diff` | collectors/postgresql | ek yetki yok (PUBLIC) | ✅ | ✅ | ✅ |
| sqlserver | `msdb.dbo.backupmediafamily` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `msdb.dbo.backupset` | collectors/sqlserver_mongodb, services/prerequisites | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.all_columns` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.all_objects` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.availability_replicas` | services/alwayson_health | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.configurations` | collectors/sqlserver_mongodb, services/config_comparison | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.database_files` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.database_query_store_options` | services/prerequisites | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.databases` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.dm_db_index_usage_stats` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_db_missing_index_details` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_db_missing_index_group_stats` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_db_missing_index_groups` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_db_partition_stats` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_exec_connections` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_exec_query_stats` | services/prerequisites | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_exec_requests` | collectors/sqlserver_mongodb, services/prerequisites | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_exec_sessions` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_exec_sql_text` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_hadr_availability_group_states` | services/alwayson_health | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_hadr_availability_replica_cluster_states` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_hadr_availability_replica_states` | collectors/sqlserver_mongodb, services/alwayson_health | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_hadr_database_replica_states` | services/alwayson_health | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_os_performance_counters` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_os_sys_info` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_os_waiting_tasks` | collectors/sqlserver_mongodb, services/prerequisites | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_tran_active_transactions` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_tran_locks` | collectors/sqlserver_mongodb | VIEW DATABASE STATE (izlenen veritabanında) | — | — | ✅ |
| sqlserver | `sys.dm_tran_session_transactions` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_xe_session_targets` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.dm_xe_sessions` | collectors/sqlserver_mongodb | VIEW SERVER STATE | — | — | ✅ |
| sqlserver | `sys.indexes` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |
| sqlserver | `sys.objects` | collectors/sqlserver_mongodb | ek yetki yok (public) | — | — | ✅ |

Hedef veritabanına yazan ifade (DDL/DML): YOK
