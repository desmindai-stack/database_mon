import { useEffect, useState } from "react";
import { api, BlockingNode, BlockingTree } from "../api";
import AdviceCard from "./AdviceCard";
import { EmptyState, PageError, PageSkeleton } from "./PageState";

/**
 * Blocking hiyerarşisi (Faz 26 İŞ 3).
 *
 * "Kaç oturum bloklandı" bir sayıdır; "kim kimi blokluyor" bir cevaptır. Bankada en sık
 * sorulan soru ikincisi.
 *
 * Ekranın taşıdığı tek asıl mesaj: müdahale edilecek oturum ZİNCİRİN BAŞINDAKİDİR. Zincirin
 * ortasındaki oturumu sonlandırmak sorunu çözmez, yalnızca bekleyeni değiştirir — bu yüzden
 * kök engelleyici en güçlü vurguyu alıyor ve alt düğümler görsel olarak ona bağlanıyor.
 */

function seconds(value: number | null | undefined): string {
  if (value == null) return "—";
  if (value < 60) return `${value.toFixed(0)} sn`;
  if (value < 3600) return `${Math.floor(value / 60)} dk ${Math.round(value % 60)} sn`;
  return `${Math.floor(value / 3600)} sa ${Math.floor((value % 3600) / 60)} dk`;
}

function NodeRow({ node, depth = 0 }: { node: BlockingNode; depth?: number }) {
  return (
    <li className="blocking-node" style={{ marginLeft: depth * 20 }}>
      <div
        className={
          "blocking-node-body" +
          (node.is_root_blocker ? " root" : "") +
          (node.is_idle_in_transaction ? " idle" : "")
        }
      >
        <div className="blocking-node-head">
          {node.is_root_blocker && (
            <span className="blocking-badge root" title="Zincirin başındaki oturum — müdahale edilecek tek yer">
              kök engelleyici
            </span>
          )}
          {node.is_idle_in_transaction && (
            <span
              className="blocking-badge idle"
              title="Hiçbir sorgu çalıştırmıyor ama açık transaction'ıyla kilit tutuyor"
            >
              sorgu çalıştırmıyor
            </span>
          )}
          <strong>pid {node.pid}</strong>
          {node.username && <span className="muted-note">{node.username}</span>}
          {node.application && <span className="muted-note">{node.application}</span>}
          {node.state && <span className="blocking-state">{node.state}</span>}
        </div>

        <div className="blocking-node-meta">
          {node.blocked_total > 0 && (
            <span className="blocking-impact">{node.blocked_total} oturumu bekletiyor</span>
          )}
          {node.transaction_seconds != null && (
            <span title="Transaction ne kadar süredir açık — sessiz blokların ölçüsü">
              transaction {seconds(node.transaction_seconds)}
            </span>
          )}
          {node.query_seconds != null && <span>sorgu {seconds(node.query_seconds)}</span>}
          {node.wait_seconds != null && node.depth > 0 && (
            <span>bekliyor {seconds(node.wait_seconds)}</span>
          )}
          {node.held_locks > 0 && <span>{node.held_locks} kilit tutuyor</span>}
          {node.lock_type && (
            <span title="Beklenen kilidin türü ve nesnesi">
              {node.lock_type}
              {node.lock_mode ? ` / ${node.lock_mode}` : ""}
              {node.lock_object ? ` → ${node.lock_object}` : ""}
            </span>
          )}
        </div>

        {node.query && <pre className="query-text">{node.query}</pre>}
      </div>

      {node.children.length > 0 && (
        <ul className="blocking-children">
          {node.children.map((child) => (
            <NodeRow key={child.pid} node={child} depth={depth + 1} />
          ))}
        </ul>
      )}
    </li>
  );
}

export default function BlockingTreePanel({ instanceId }: { instanceId: number }) {
  const [tree, setTree] = useState<BlockingTree | null>(null);
  const [loading, setLoading] = useState(true);
  // HAM hata saklanıyor: PageError 500 ile ağ kopmasını ApiError.status üzerinden ayırıyor.
  const [error, setError] = useState<unknown>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .getBlockingTree(instanceId)
      .then((data) => {
        if (!cancelled) setTree(data);
      })
      .catch((err) => {
        if (!cancelled) setError(err);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [instanceId, reloadKey]);

  // Bloklama ANLIK bir durumdur ve saniyeler içinde değişir; kullanıcının elle yenilemesi
  // gerekmesin diye periyodik tazeleniyor. 10 saniye: canlı hissettirecek kadar sık, hedef
  // sunucuya yük bindirmeyecek kadar seyrek.
  useEffect(() => {
    const timer = setInterval(() => setReloadKey((k) => k + 1), 10000);
    return () => clearInterval(timer);
  }, []);

  if (loading && !tree) return <PageSkeleton rows={4} />;
  if (error) return <PageError error={error} onRetry={() => setReloadKey((k) => k + 1)} />;
  if (!tree) return null;

  if (tree.unavailable_reason) {
    return <EmptyState title="Bloklama görüntüsü alınamadı" detail={tree.unavailable_reason} />;
  }

  const nothingToShow =
    tree.roots.length === 0 &&
    tree.idle_in_transaction.length === 0 &&
    tree.long_transactions.length === 0;

  return (
    <div className="blocking-panel">
      <div className="stats-grid compact">
        <div
          className="card stat-card"
          style={{ borderLeftColor: tree.blocked_sessions > 0 ? "var(--danger)" : "var(--accent)" }}
        >
          <div className="stat-meta">
            <h3>Bloklanan oturum</h3>
            <span>{tree.root_blockers} kök engelleyici</span>
          </div>
          <div className="value">{tree.blocked_sessions}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-meta">
            <h3>Zincir derinliği</h3>
            <span>{tree.max_depth > 1 ? "Çok kademeli blok" : "Tek kademe"}</span>
          </div>
          <div className="value">{tree.max_depth}</div>
        </div>
        <div className="card stat-card">
          <div className="stat-meta">
            <h3>Açık transaction (sorgu yok)</h3>
            <span>Sessiz bloklama riski</span>
          </div>
          <div className="value">{tree.idle_in_transaction.length}</div>
        </div>
      </div>

      {tree.advice && <AdviceCard advice={tree.advice} defaultOpen={false} />}

      {nothingToShow ? (
        <EmptyState
          title="Bloklanan oturum yok"
          detail="Şu anda hiçbir oturum başka bir oturumu beklemiyor ve uzun süre açık kalmış transaction da yok."
        />
      ) : (
        <>
          {tree.roots.length > 0 && (
            <div className="card">
              <h3 className="chart-title">Bloklama zinciri</h3>
              <p className="muted-note">
                Müdahale edilecek oturum <strong>zincirin başındakidir</strong>. Ortadaki bir
                oturumu sonlandırmak sorunu çözmez — o da bekliyor.
              </p>
              <ul className="blocking-tree">
                {tree.roots.map((root) => (
                  <NodeRow key={root.pid} node={root} />
                ))}
              </ul>
            </div>
          )}

          {tree.idle_in_transaction.length > 0 && (
            <div className="card">
              <h3 className="chart-title">Sorgu çalıştırmadan kilit tutan oturumlar</h3>
              <p className="muted-note">
                Bu oturumlar CPU harcamıyor ve yavaş sorgu listesinde görünmüyor, ama açık
                transaction'larıyla kilit tutuyorlar. Şu anda kimseyi bekletmiyor olsalar bile
                zaman bombasıdır.
              </p>
              <ul className="blocking-tree">
                {tree.idle_in_transaction.map((node) => (
                  <NodeRow key={node.pid} node={node} />
                ))}
              </ul>
            </div>
          )}

          {tree.long_transactions.length > 0 && (
            <div className="card">
              <h3 className="chart-title">Uzun süredir açık transaction'lar</h3>
              <ul className="blocking-tree">
                {tree.long_transactions.map((node) => (
                  <NodeRow key={node.pid} node={node} />
                ))}
              </ul>
            </div>
          )}
        </>
      )}
    </div>
  );
}
