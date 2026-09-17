// Faz 31 İŞ 1 — index önerisi sonucu.
//
// Üç şey her zaman görünür, çünkü "öneri yok" tek başına denetlenemez bir sonuçtu:
//   1. Sonucun DURUMU (öneri / eşik altında / sistem sorgusu / çözümlenemedi …),
//   2. Sorguda BULUNAN filtreler ve her birinin neden index'e dönüştürülemediği,
//   3. Ölçülemeyen şeyler (yetki, istatistik) — uydurma bir yüzde yerine gerekçe.
import type { IndexAdviceOutcome, IndexAdviceReport, IndexAdviceWatch, IndexPredicate } from "../api";
import AdviceCard from "./AdviceCard";
import CopyableAction from "./CopyableAction";
import RecommendationHeader from "./RecommendationHeader";

const KIND_LABELS: Record<string, string> = {
  eq: "eşitlik",
  in: "IN / eşitlik listesi",
  is_null: "IS NULL",
  join: "join anahtarı",
  range: "aralık",
  like_prefix: "LIKE (önekli)",
  like_unanchored: "LIKE (baştan joker)",
  like_unknown: "LIKE (kalıp bilinmiyor)",
  sort: "sıralama",
  group: "gruplama",
  other: "diğer",
};

const CLAUSE_LABELS: Record<string, string> = {
  where: "WHERE",
  join_on: "JOIN ON",
  having: "HAVING",
  order_by: "ORDER BY",
  group_by: "GROUP BY",
};

const CONTEXT_LABELS: Record<string, string> = {
  main: "",
  cte: " (CTE içinde)",
  subquery: " (alt sorguda)",
  exists: " (EXISTS içinde)",
};

const INDEX_KIND_LABELS: Record<string, string> = {
  btree: "B-tree",
  expression: "İfade index'i",
  like_prefix: "Önek araması",
  trigram: "Trigram (GIN)",
};

function FoundPredicates({ predicates }: { predicates: IndexPredicate[] }) {
  if (predicates.length === 0) {
    return (
      <p className="muted-note">
        Bulunan filtreler: sorguda WHERE/JOIN/ORDER BY/GROUP BY ile filtrelenen bir kolon bulunamadı.
      </p>
    );
  }
  return (
    <details className="found-predicates" open={predicates.some((p) => !p.usable)}>
      <summary>
        Bulunan filtreler ({predicates.length}, {predicates.filter((p) => !p.usable).length} tanesi index'e
        dönüştürülemedi)
      </summary>
      <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>Kolon</th>
            <th>Tablo</th>
            <th>Tür</th>
            <th>Yer</th>
            <th>Durum</th>
          </tr>
        </thead>
        <tbody>
          {predicates.map((p, i) => (
            <tr key={`${p.column}-${p.clause}-${i}`}>
              <td>
                <code>{p.expression ?? p.column}</code>
              </td>
              <td>{p.table ?? (p.candidates?.length ? `belirsiz: ${p.candidates.join(", ")}` : "çözülemedi")}</td>
              <td>{KIND_LABELS[p.kind] ?? p.kind}</td>
              <td>
                {CLAUSE_LABELS[p.clause] ?? p.clause}
                {CONTEXT_LABELS[p.context] ?? ""}
              </td>
              <td>{p.usable ? "Kullanılabilir" : <span className="muted-note">{p.unusable_reason}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      </div>
    </details>
  );
}

function Reasons({ report }: { report: IndexAdviceReport }) {
  if (report.no_advice_reasons.length === 0) {
    return null;
  }
  return (
    <div className="tuning-checklist">
      {report.no_advice_reasons.map((r) => (
        <div key={r.code + r.message} className={`checklist-row ${r.code === "insufficient_samples" ? "info" : "warn"}`}>
          <span className="checklist-status">{r.code === "insufficient_samples" ? "İzlemede" : "Öneri yok"}</span>
          <div>
            <strong>{r.message}</strong>
            <p>{r.what_to_do}</p>
          </div>
        </div>
      ))}
    </div>
  );
}

function AdviceItem({ advice: a }: { advice: IndexAdviceReport["advice"][number] }) {
  return (
    <div className={`advice-card${a.verified === false ? " unverified" : ""}`}>
      <div className="advice-header">
        <span className="advice-table">
          {a.schema_name}.{a.table_name} · {INDEX_KIND_LABELS[a.index_kind ?? "btree"] ?? a.index_kind}
        </span>
        {a.verified === false && <span className="tag warn">Doğrulanmadı</span>}
        <span className="advice-pill">
          {a.estimated_improvement_pct != null ? (
            <>
              Tahmini iyileştirme: <strong>%{a.estimated_improvement_pct}</strong> (hypopg ile ölçüldü)
            </>
          ) : (
            <>Fayda ölçülemedi</>
          )}
        </span>
      </div>
      {a.verification_note && <p className="advice-empty">{a.verification_note}</p>}
      {a.advice ? (
        <AdviceCard advice={a.advice} defaultOpen={false} />
      ) : (
        <>
          <RecommendationHeader title={`${a.schema_name}.${a.table_name} için index ekleyin`} />
          <p className="recommendation-reason">{a.reason}</p>
          <CopyableAction command={a.index_ddl} />
        </>
      )}
      {a.before_cost != null && a.after_cost != null && (
        <div className="advice-costs">
          <span>
            Plan maliyeti (hypopg tahmini, index kurulmadan): {a.before_cost.toFixed(1)} → {a.after_cost.toFixed(1)}
          </span>
        </div>
      )}
      {(a.measurement_notes ?? []).length > 0 && (
        <ul className="measurement-notes">
          {(a.measurement_notes ?? []).map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
    </div>
  );
}

// Faz 31 Commit 4: hangi tabloda hangi yetki NEDEN gerekiyor — hepsi tek yerde, tek komut bloğuyla.
function RequiredGrants({ grants }: { grants: NonNullable<IndexAdviceReport["required_grants"]> }) {
  return (
    <section className="advice-grants">
      <h4>Gereken yetkiler</h4>
      <ul>
        {grants.tables.map((t) => (
          <li key={t.table}>
            <code>{t.table}</code> — {t.privilege}
            <ul>
              {t.reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
      <CopyableAction command={grants.command} />
      <p className="muted-note">{grants.note}</p>
    </section>
  );
}

export function IndexAdviceResult({ report }: { report: IndexAdviceReport }) {
  const verified = report.advice.filter((a) => a.verified !== false);
  const unverified = report.advice.filter((a) => a.verified === false);
  return (
    <div className="advice-results">
      <p className="muted-note">
        Kaynak: dbace — bu sorgunun metninden üretildi. Fayda, hypopg kuruluysa ve izleme kullanıcısı tabloyu
        okuyabiliyorsa ölçülür; ölçülemediyse kartta nedeni yazar.
      </p>
      {report.threshold && (
        <p className="advice-threshold">
          Şu anda <strong>{report.threshold.calls_now}/{report.threshold.threshold}</strong> çağrı.{" "}
          {report.threshold.watching
            ? "Sorgu izlemeye alındı; eşik dolunca öneri otomatik üretilecek ve burada görünecek."
            : report.threshold.watch_enabled
              ? ""
              : "Otomatik izleme kapalı (Yönetim → Ayarlar → Analiz)."}
        </p>
      )}
      <Reasons report={report} />
      {verified.map((a) => (
        <AdviceItem key={a.index_ddl} advice={a} />
      ))}
      {unverified.length > 0 && (
        <section className="advice-unverified">
          <h4>Doğrulanmamış öneriler</h4>
          <p className="muted-note">
            Bu öneriler sorgu yapısından üretildi ama sunucuda DOĞRULANAMADI. Her kartta hangi denetimin neden
            yapılamadığı yazıyor; uygulamadan önce bir test ortamında deneyin.
          </p>
          {unverified.map((a) => (
            <AdviceItem key={a.index_ddl} advice={a} />
          ))}
        </section>
      )}
      {(report.outcomes ?? []).length > 0 && (
        <p className="muted-note">
          {(report.outcomes ?? []).every((o) => o.status === "not_measurable")
            ? `Etki ölçümü: ${(report.outcomes ?? [])[0].note ?? "ölçülemedi"}`
            : "Etki ölçümü: öneri anındaki plan kaydedildi. Index kurulunca dbace aynı sorgunun planını yeniden alır; sonuç \"Kurulan index'lerin ölçülen etkisi\" bölümünde görünür."}
        </p>
      )}
      {report.required_grants && <RequiredGrants grants={report.required_grants} />}
      {report.status !== "system" && <FoundPredicates predicates={report.predicates ?? []} />}
    </div>
  );
}

const OUTCOME_STATUS_LABELS: Record<string, string> = {
  waiting_for_index: "Index bekleniyor",
  measured: "Ölçüldü",
  not_measurable: "Ölçülemedi",
};

// Faz 31 Commit 5 — asıl fayda yolu: index kurulduktan sonra AYNI sorgunun önce/sonra planı.
// Yüzde yalnızca ölçüldüyse ve kaynağıyla birlikte gösteriliyor.
export function AdviceOutcomeList({
  outcomes,
  error,
}: {
  outcomes: IndexAdviceOutcome[] | null;
  error: string | null;
}) {
  if (error) {
    return <p className="advice-empty">Index etkisi ölçümleri yüklenemedi: {error}</p>;
  }
  if (!outcomes || outcomes.length === 0) {
    return null;
  }
  const measured = outcomes.filter((o) => o.status === "measured").length;
  return (
    <details className="advice-watch-list" open={measured > 0}>
      <summary>
        Kurulan index'lerin ölçülen etkisi: {outcomes.length} öneri
        {measured > 0 ? ` — ${measured} tanesi ölçüldü` : ""}
      </summary>
      <p className="muted-note">{outcomes[0].source_label}</p>
      {outcomes.map((o) => (
        <div key={o.id} className={`advice-watch ${o.status}`}>
          <div className="advice-header">
            <span className="advice-pill">{OUTCOME_STATUS_LABELS[o.status] ?? o.status}</span>
            <span className="advice-table">{o.table_name}</span>
          </div>
          <code className="query-text">{o.index_ddl}</code>
          {o.status === "measured" && o.before_cost != null && o.after_cost != null && (
            <p>
              Plan maliyeti: <strong>{o.before_cost.toFixed(1)} → {o.after_cost.toFixed(1)}</strong>
              {o.measured_cost_reduction_pct != null && ` (%${o.measured_cost_reduction_pct} azalma)`} ·{" "}
              {o.after_uses_new_index
                ? `plan yeni index'i (${o.after_index_name}) kullanıyor`
                : `plan yeni index'i (${o.after_index_name}) KULLANMIYOR`}
            </p>
          )}
          {o.status === "waiting_for_index" && o.before_cost != null && (
            <p className="muted-note">
              Önce: plan maliyeti {o.before_cost.toFixed(1)}. Index hedefte kurulunca sonraki plan otomatik alınır.
            </p>
          )}
          {o.note && <p className="advice-empty">{o.note}</p>}
        </div>
      ))}
    </details>
  );
}

const WATCH_STATUS_LABELS: Record<string, string> = {
  waiting: "Eşik bekleniyor",
  ready: "Öneri hazır",
  failed: "Son deneme başarısız",
};

export function AdviceWatchList({
  watches,
  error,
}: {
  watches: IndexAdviceWatch[] | null;
  error: string | null;
}) {
  if (error) {
    return <p className="advice-empty">İzlenen sorgular yüklenemedi: {error}</p>;
  }
  if (!watches || watches.length === 0) {
    return null;
  }
  const ready = watches.filter((w) => w.status === "ready").length;
  return (
    <details className="advice-watch-list" open={ready > 0}>
      <summary>
        İzlenen index önerileri: {watches.length} sorgu
        {ready > 0 ? ` — ${ready} tanesi için öneri hazır` : ""}
      </summary>
      {watches.map((w) => (
        <div key={w.id} className={`advice-watch ${w.status}`}>
          <div className="advice-header">
            <span className="advice-pill">{WATCH_STATUS_LABELS[w.status] ?? w.status}</span>
            <span>
              {w.calls_seen}/{w.threshold} çağrı
            </span>
          </div>
          <code className="query-text">{w.query.length > 240 ? `${w.query.slice(0, 240)}…` : w.query}</code>
          {w.last_error && <p className="advice-empty">{w.last_error}</p>}
          {w.status === "ready" && w.report && <IndexAdviceResult report={w.report} />}
        </div>
      ))}
    </details>
  );
}
