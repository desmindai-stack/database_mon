// Faz 31 İŞ 1 — index önerisi sonucu.
//
// Üç şey her zaman görünür, çünkü "öneri yok" tek başına denetlenemez bir sonuçtu:
//   1. Sonucun DURUMU (öneri / eşik altında / sistem sorgusu / çözümlenemedi …),
//   2. Sorguda BULUNAN filtreler ve her birinin neden index'e dönüştürülemediği,
//   3. Ölçülemeyen şeyler (yetki, istatistik) — uydurma bir yüzde yerine gerekçe.
import type { IndexAdviceReport, IndexAdviceWatch, IndexPredicate } from "../api";
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

export function IndexAdviceResult({ report }: { report: IndexAdviceReport }) {
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
      {report.advice.map((a) => (
        <div className="advice-card" key={a.index_ddl}>
          <div className="advice-header">
            <span className="advice-table">
              {a.schema_name}.{a.table_name} · {INDEX_KIND_LABELS[a.index_kind ?? "btree"] ?? a.index_kind}
            </span>
            <span className="advice-pill">
              {a.estimated_improvement_pct != null ? (
                <>
                  Tahmini iyileştirme: <strong>%{a.estimated_improvement_pct}</strong>
                  {a.has_hypopg_estimate ? " (hypopg ile ölçüldü)" : " (istatistik tahmini)"}
                </>
              ) : (
                <>Fayda ölçülemedi</>
              )}
            </span>
          </div>
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
                Plan maliyeti: {a.before_cost.toFixed(1)} → {a.after_cost.toFixed(1)}
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
      ))}
      {report.status !== "system" && <FoundPredicates predicates={report.predicates ?? []} />}
    </div>
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
