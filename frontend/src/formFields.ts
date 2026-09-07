/**
 * Kurulum formlarında hangi alanın hangi engine/topolojide görüneceğinin TEK kaynağı.
 *
 * Neden tek kaynak: bu kural daha önce her formda ayrı ayrı yazılmıştı ve ayrışmıştı —
 * standalone seçiliyken cluster alanları duruyordu, SQL Server instance'ında Patroni/etcd
 * servis listesi çıkıyordu. Kural burada bir TABLO; formlar `showField()` çağırıyor.
 *
 * İlke: kural sağlanmıyorsa alan **DOM'da hiç bulunmaz** — gizlenmez, devre dışı bırakılmaz.
 * Gizli bir alan hâlâ form durumunda yer tutar, sekme sırasında görünür ve kaydedilirken
 * ilgisiz değer gönderir.
 */
import type { DbEngine } from "./api";

/** Formun topolojisi. `patroni` / `alwayson` ayrımı alan gösterimini etkilemiyor. */
export type FormTopology = "standalone" | "cluster";

export type FieldKey =
  // --- Cluster kimliği ---
  | "cluster_name"
  | "role"
  | "services"
  | "access_name"
  | "vip_address"
  | "listener_port"
  | "add_node"
  // --- PostgreSQL cluster servisleri ---
  | "patroni_port"
  | "etcd_port"
  | "haproxy_stats_port"
  | "haproxy_stats_path"
  | "keepalived_vip"
  // --- PostgreSQL bağlantısı ---
  | "ssl_mode"
  | "uses_pooler"
  // --- SQL Server ---
  | "instance_name"
  | "auth_type"
  // --- MongoDB ---
  | "replica_set"
  | "auth_source";

type Rule = {
  /** Verilmezse her engine'de geçerli. */
  engines?: DbEngine[];
  /** Verilmezse her topolojide geçerli. */
  topologies?: FormTopology[];
  /** Alanın neden bu kapsamda olduğu — kural değiştirilirken okunsun diye. */
  why: string;
};

const CLUSTER: FormTopology[] = ["cluster"];

export const FIELD_RULES: Record<FieldKey, Rule> = {
  cluster_name: {
    topologies: CLUSTER,
    why: "Standalone bir veritabanının cluster adı yoktur; alan doldurulursa yanlış gruplama yapılır.",
  },
  role: {
    topologies: CLUSTER,
    why: "primary/replica rolü yalnızca cluster üyeliğinde anlamlı.",
  },
  services: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "Liste Patroni/etcd/HAProxy/keepalived servisleri — PostgreSQL cluster kavramları. SQL Server ve MongoDB'de karşılığı yok.",
  },
  access_name: {
    topologies: CLUSTER,
    why: "Listener adı (SQL Server) / VIP adresi (Patroni) tek düğümlü kurulumda yoktur.",
  },
  vip_address: { topologies: CLUSTER, why: "Sanal IP yalnızca cluster erişiminde kullanılır." },
  listener_port: { topologies: CLUSTER, why: "Listener/VIP portu tek düğümlü kurulumda yoktur." },
  add_node: {
    topologies: CLUSTER,
    why: "Standalone bir grup ikinci düğüm almaz (backend wizard_add_nodes 400 döner).",
  },

  patroni_port: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "Patroni REST API yalnızca PostgreSQL cluster'ında var.",
  },
  etcd_port: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "etcd, Patroni'nin dağıtık yapılandırma deposu; standalone'da ve diğer engine'lerde yok.",
  },
  haproxy_stats_port: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "HAProxy, Patroni kurulumunda önde duran yük dengeleyici.",
  },
  haproxy_stats_path: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "HAProxy stats yolu — port ile birlikte anlamlı.",
  },
  keepalived_vip: {
    engines: ["postgresql"],
    topologies: CLUSTER,
    why: "keepalived VIP sahipliğini yönetir; tek düğümde devretme kavramı yok.",
  },

  ssl_mode: { engines: ["postgresql"], why: "asyncpg sslmode parametresi; diğer sürücülerde farklı ele alınıyor." },
  uses_pooler: {
    engines: ["postgresql"],
    why: "PgBouncer/Supabase pooler prepared statement uyumsuzluğu PostgreSQL'e özgü.",
  },

  instance_name: {
    engines: ["sqlserver"],
    why: "Adlandırılmış SQL Server örneği (MSSQLSERVER varsayılan); başka engine'de karşılığı yok.",
  },
  auth_type: { engines: ["sqlserver"], why: "SQL / Windows Integrated seçimi SQL Server'a özgü." },

  replica_set: { engines: ["mongodb"], why: "MongoDB replica set adı." },
  auth_source: { engines: ["mongodb"], why: "MongoDB authSource veritabanı." },
};

export type FieldContext = { engine: DbEngine; topology: FormTopology };

/** Alan bu bağlamda render edilmeli mi? */
export function showField(key: FieldKey, ctx: FieldContext): boolean {
  const rule = FIELD_RULES[key];
  if (rule.engines && !rule.engines.includes(ctx.engine)) return false;
  if (rule.topologies && !rule.topologies.includes(ctx.topology)) return false;
  return true;
}

/** Bir bağlamda görünen tüm alanlar — testler ve hata ayıklama için. */
export function visibleFields(ctx: FieldContext): FieldKey[] {
  return (Object.keys(FIELD_RULES) as FieldKey[]).filter((key) => showField(key, ctx));
}

/**
 * Bir grubun/instance'ın topolojisi. Backend `GroupTopology` üç değer taşıyor
 * (`standalone` | `patroni` | `alwayson`); alan gösterimi için ikisi de "cluster".
 */
export function topologyOf(value: string | null | undefined): FormTopology {
  return value && value !== "standalone" ? "cluster" : "standalone";
}
