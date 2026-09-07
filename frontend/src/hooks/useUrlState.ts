import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";

/**
 * Sekme ve filtre durumunu URL'de tutar (Faz 19 İŞ 1).
 *
 * Öncesinde sekmeler `useState` içindeydi ve URL'e hiç yazılmıyordu (AdminPage, AlertsPage),
 * ya da yazılıyor ama `replace: true` ile yazılıyordu (InstanceDetailPage, GroupDetailPage).
 * İkisinin de sonucu aynı: tarayıcının GERİ düğmesi sekme değişimini geri almıyor, kullanıcı
 * geri bastığında beklediği sekme yerine bir önceki SAYFAYA fırlıyordu.
 *
 * Burada sekme değişimi geçmişe bir kayıt olarak İTİLİYOR (push), böylece geri/ileri
 * düğmeleri sekmeler arasında gezinir. Bağlantı paylaşıldığında da aynı sekme açılır.
 */
export function useUrlTab<T extends string>(
  key: string,
  allowed: readonly T[],
  fallback: T,
): [T, (next: T) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const raw = searchParams.get(key);
  const tab = useMemo(
    () => (allowed.includes(raw as T) ? (raw as T) : fallback),
    // `allowed` her render'da yeni dizi olabilir; içeriği sabit olduğu için raw yeterli.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [raw, fallback],
  );

  const setTab = useCallback(
    (next: T) => {
      const params = new URLSearchParams(searchParams);
      if (next === fallback) params.delete(key);
      else params.set(key, next);
      // Sekme değişimi bir gezinme adımıdır — replace DEĞİL, push.
      setSearchParams(params);
    },
    [key, fallback, searchParams, setSearchParams],
  );

  return [tab, setTab];
}

/**
 * Tek bir metin filtresini URL'de tutar. Boş değer parametreyi siler ki adres temiz kalsın.
 * Filtreler sekmelerden farklı: her tuş vuruşunu geçmişe yazmak geri düğmesini kullanılamaz
 * hâle getirir, bu yüzden `replace` kullanılıyor.
 */
export function useUrlFilter(
  key: string,
  fallback = "",
  options: { history?: "replace" | "push" } = {},
): [string, (next: string) => void] {
  const [searchParams, setSearchParams] = useSearchParams();
  const value = searchParams.get(key) ?? fallback;
  // Varsayılan `replace`: bir arama kutusunda her tuş vuruşunu geçmişe yazmak geri düğmesini
  // kullanılamaz hâle getirir.
  //
  // Ama TIKLAMAYLA seçilen bir filtre (dashboard durum kartı gibi) ayrı bir durum: kullanıcı
  // bunu bilinçli bir gezinme adımı olarak yapıyor ve geri basınca filtrenin kalkmasını
  // bekliyor. `replace` orada geri düğmesini uygulamadan ÇIKARIYORDU — tarayıcı testi
  // (Faz 24) yakaladı.
  const replace = (options.history ?? "replace") === "replace";

  const setValue = useCallback(
    (next: string) => {
      const params = new URLSearchParams(searchParams);
      if (!next || next === fallback) params.delete(key);
      else params.set(key, next);
      setSearchParams(params, { replace });
    },
    [key, fallback, replace, searchParams, setSearchParams],
  );

  return [value, setValue];
}
