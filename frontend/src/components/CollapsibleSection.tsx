import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { CSSProperties, ReactNode } from "react";

/**
 * Katlanabilir bölüm ve sayfa özet şeridi (Faz 29 İŞ 3).
 *
 * ## Çözdüğü sorun
 *
 * Sayfalar sonsuza doğru akıyordu: DPA'da 15, raporlarda 13 kart alt alta. Kritik bir bulgu
 * onuncu kartın içinde kalıyor ve kullanıcı onu hiç görmüyordu. Uzun açıklama blokları
 * (ör. "Ön koşullar — şu uzantı, şu ayar, şu yetki...") ekranın yarısını kaplıyordu.
 *
 * ## Üç kural
 *
 * 1. **Sorunlu bölüm açık, sorunsuz bölüm kapalı gelir.** "Her şeyi kapat" da yanlış
 *    olurdu: kullanıcı kritik bulguyu görmek için tıklamak zorunda kalırdı. Varsayılan
 *    duruma karar veren şey bölümün DURUMU.
 * 2. **Kullanıcının kararı varsayılanı yener ve hatırlanır.** Bir bölümü kapattıysa
 *    bir dahaki gelişinde kapalı gelir — ama bölüm SORUNLU hale geldiyse açılır, çünkü
 *    "dün kapattım" kararı dünkü duruma verilmişti.
 * 3. **Özet şeridi her zaman en üstte.** Sayfada kaç kritik, kaç uyarı olduğu tek bakışta
 *    görünür ve tıklayınca ilgili bölüme gidilir (kapalıysa açılarak).
 *
 * ## Durum neden localStorage'da
 *
 * Kullanıcı başına ve tarayıcı başına bir tercih; sunucuya taşımak, her sayfa açılışında
 * fazladan bir istek ve bir tablo demekti. Kaybolması da zararsız: en kötü ihtimalle
 * varsayılan davranışa dönülür.
 */

export type SectionStatus = "critical" | "warning" | "info" | "ok" | "unknown";

/** Bölümün varsayılan olarak açık gelip gelmeyeceği — DURUMA göre. */
export function defaultOpenFor(status: SectionStatus): boolean {
  return status === "critical" || status === "warning" || status === "unknown";
}

interface RegisteredSection {
  id: string;
  title: string;
  status: SectionStatus;
  critical: number;
  warning: number;
}

interface SectionsContextValue {
  register: (section: RegisteredSection) => void;
  unregister: (id: string) => void;
  isOpen: (id: string, status: SectionStatus, fallbackOpen?: boolean) => boolean;
  toggle: (id: string) => void;
  open: (id: string) => void;
  sections: RegisteredSection[];
  pageKey: string;
}

const SectionsContext = createContext<SectionsContextValue | null>(null);

function storageKey(pageKey: string): string {
  return `dbace.sections.${pageKey}`;
}

function readStored(pageKey: string): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(storageKey(pageKey));
    return raw ? (JSON.parse(raw) as Record<string, boolean>) : {};
  } catch {
    // Gizli sekme, kapalı depolama, bozuk JSON — hiçbiri sayfayı düşürmemeli.
    return {};
  }
}

export function SectionsProvider({ pageKey, children }: { pageKey: string; children: ReactNode }) {
  const [overrides, setOverrides] = useState<Record<string, boolean>>(() => readStored(pageKey));
  const [sections, setSections] = useState<RegisteredSection[]>([]);

  useEffect(() => {
    setOverrides(readStored(pageKey));
  }, [pageKey]);

  const persist = useCallback(
    (next: Record<string, boolean>) => {
      setOverrides(next);
      try {
        localStorage.setItem(storageKey(pageKey), JSON.stringify(next));
      } catch {
        // Depolama yoksa tercih bu oturumda yaşar; işlev kaybı yok.
      }
    },
    [pageKey],
  );

  const register = useCallback((section: RegisteredSection) => {
    setSections((prev) => {
      const rest = prev.filter((s) => s.id !== section.id);
      const next = [...rest, section];
      // Kayıt sırası = sayfadaki sıra değil (efektler alt bileşenden başlar), o yüzden
      // ciddiyete göre sıralanıyor: özet şeridinde kritik olan başta görünmeli.
      return next;
    });
  }, []);

  const unregister = useCallback((id: string) => {
    setSections((prev) => prev.filter((s) => s.id !== id));
  }, []);

  const isOpen = useCallback(
    (id: string, status: SectionStatus, fallbackOpen?: boolean) => {
      const stored = overrides[id];
      // Ayar sayfalarinda durumdan turetmek yanlis olurdu: kullanici oraya zaten bir ayari
      // degistirmeye geliyor, kontrolu bir tiklamanin arkasina saklamak isi zorlastirirdi.
      // O yuzden cagiran acik bir varsayilan verebiliyor; kullanicinin kendi karari yine yener.
      if (stored === undefined) return fallbackOpen ?? defaultOpenFor(status);
      // KULLANICININ KAPATMA KARARI SORUNLU BÖLÜMDE GEÇERSİZ: "dün kapattım" kararı dünkü
      // duruma verilmişti; bölüm bugün kritikse gizlemek, gizlenmesi en yanlış şeyi gizlemek
      // olurdu. Açma kararı ise her durumda geçerli.
      if (stored === false && status === "critical") return true;
      return stored;
    },
    [overrides],
  );

  const toggle = useCallback(
    (id: string) => {
      const current = sections.find((s) => s.id === id);
      const currentlyOpen = isOpen(id, current?.status ?? "ok");
      persist({ ...overrides, [id]: !currentlyOpen });
    },
    [isOpen, overrides, persist, sections],
  );

  const open = useCallback(
    (id: string) => {
      persist({ ...overrides, [id]: true });
    },
    [overrides, persist],
  );

  const value = useMemo<SectionsContextValue>(
    () => ({ register, unregister, isOpen, toggle, open, sections, pageKey }),
    [register, unregister, isOpen, toggle, open, sections, pageKey],
  );

  return <SectionsContext.Provider value={value}>{children}</SectionsContext.Provider>;
}

function useSections(): SectionsContextValue | null {
  return useContext(SectionsContext);
}

const STATUS_ORDER: Record<SectionStatus, number> = {
  critical: 0,
  warning: 1,
  unknown: 2,
  info: 3,
  ok: 4,
};

/**
 * Sayfa özet şeridi: kaç kritik, kaç uyarı — ve tıklayınca ilgili bölüme gider.
 *
 * `SectionsProvider` içinde değilse hiçbir şey çizmiyor: bileşeni yanlışlıkla provider
 * dışına koymak sayfayı düşürmemeli.
 */
export function PageSummaryBar() {
  const ctx = useSections();
  if (!ctx || ctx.sections.length === 0) return null;

  const critical = ctx.sections.reduce((sum, s) => sum + s.critical, 0);
  const warning = ctx.sections.reduce((sum, s) => sum + s.warning, 0);
  const problems = ctx.sections
    .filter((s) => s.critical > 0 || s.warning > 0 || s.status === "critical" || s.status === "warning")
    .sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status] || b.critical - a.critical);

  function goTo(id: string) {
    ctx!.open(id);
    // Açılma bir sonraki render'da olacağı için kaydırma bir kare geciktiriliyor; aksi
    // halde kapalı yüksekliğe kaydırılır ve bölüm ekranın dışında kalır.
    requestAnimationFrame(() => {
      document.getElementById(`section-${id}`)?.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  return (
    <div className={`page-summary-bar${critical > 0 ? " has-critical" : ""}`}>
      <div className="page-summary-counts">
        <span className={`summary-count${critical > 0 ? " critical" : ""}`}>
          <strong>{critical}</strong> kritik
        </span>
        <span className={`summary-count${warning > 0 ? " warning" : ""}`}>
          <strong>{warning}</strong> uyarı
        </span>
      </div>
      {problems.length === 0 ? (
        <span className="muted-note">Bu sayfada dikkat gerektiren bir bölüm yok.</span>
      ) : (
        <div className="page-summary-links">
          {problems.map((s) => (
            <button
              key={s.id}
              type="button"
              className={`summary-jump ${s.status}`}
              onClick={() => goTo(s.id)}
            >
              {s.title}
              {s.critical > 0 && <span className="summary-jump-count">{s.critical}</span>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Katlanabilir bölüm.
 *
 * `SectionsProvider` dışında da çalışır (kendi state'iyle): bir sayfayı kademeli olarak
 * taşımak mümkün olsun diye. O durumda özet şeridine katkı vermez.
 */
export default function CollapsibleSection({
  id,
  title,
  status = "ok",
  critical = 0,
  warning = 0,
  subtitle,
  actions,
  defaultOpen,
  style,
  children,
}: {
  id: string;
  title: string;
  status?: SectionStatus;
  critical?: number;
  warning?: number;
  subtitle?: ReactNode;
  actions?: ReactNode;
  /** Durumdan turetilen varsayilani ezer (ayar bolumleri gibi "durumu olmayan" yerler icin). */
  defaultOpen?: boolean;
  style?: CSSProperties;
  children: ReactNode;
}) {
  const ctx = useSections();
  const [localOpen, setLocalOpen] = useState(() => defaultOpen ?? defaultOpenFor(status));

  const { register, unregister } = ctx ?? {};
  useEffect(() => {
    if (!register || !unregister) return;
    register({ id, title, status, critical, warning });
    return () => unregister(id);
  }, [register, unregister, id, title, status, critical, warning]);

  const open = ctx ? ctx.isOpen(id, status, defaultOpen) : localOpen;
  const toggle = () => (ctx ? ctx.toggle(id) : setLocalOpen((v) => !v));

  return (
    <div className={`card collapsible-section ${status}`} id={`section-${id}`} style={style}>
      <div className="collapsible-head">
        <div className="collapsible-title">
          <h3 className="chart-title">{title}</h3>
          {critical > 0 && <span className="insight-severity critical">{critical}</span>}
          {warning > 0 && <span className="insight-severity warning">{warning}</span>}
          {subtitle && <span className="muted-note">{subtitle}</span>}
        </div>
        <div className="collapsible-actions">
          {actions}
          <button
            type="button"
            className="collapse-toggle"
            onClick={toggle}
            aria-expanded={open}
            aria-controls={`section-body-${id}`}
            title={open ? "Bölümü kapat" : "Bölümü aç"}
          >
            {open ? "▾" : "▸"}
          </button>
        </div>
      </div>
      {/* `hidden` yerine koşullu render: kapalı bölümün içeriği hiç çizilmiyor, yani ağır
          tablolar ve grafikler sayfayı yavaşlatmıyor. Katlamanın asıl kazancı bu. */}
      {open && (
        <div className="collapsible-body" id={`section-body-${id}`}>
          {children}
        </div>
      )}
    </div>
  );
}
