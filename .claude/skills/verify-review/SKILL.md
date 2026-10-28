---
name: verify-review
description: Antigravity'nin (ya da başka bir yapay zekânın) verdiği inceleme, performans önerisi, donanım veya simülasyon iddialarını uygulamadan önce gerçek LTspice ölçümüyle doğrula, yalnızca tutanları uygula ve dersleri kaydet. "antigravity şunları verdi", "bu öneriler doğru mu", "incelemeyi uygula", "lesson learned olarak kaydet" gibi isteklerde kullan.
---

# İnceleme iddialarını ölçerek doğrula

1. **İddiaları ayıkla.** Her iddiayı numaralı listeye yaz; yanına "hangi ölçüm bunu doğrular ya da çürütür" notunu ekle.
   Hız iddiası için süre, devre iddiası için düğüm gerilimi/akımı ya da `.meas` değeri.
   - Önerilen özellik zaten var mı, sayısı nereden geliyor? Gemini'nin ".save ile 39,2 → 1,4 MB, 28x"i bizim
     belgemizden, eski ASCII tabanından kopyalanmıştı; `save_signals` ve `report._decimate` zaten vardı (2026-10-06).
     Bulgu `docs/` "Açık konular" listelerimizde mi? Üçüncü incelemede 6 bulgunun 3'ü oradan sayılarıyla alınmıştı
     (`grep` ile sayıyı ara); bunlar yeni değil, zaten bilinen işlerdir. Bilinen iş ölçülmüş iş demek değil: açık
     konu notu bir tahmin olabilir. Dördüncü incelemede "`.log` yoksa hata ASCII diyor" notunu ölçünce altından
     sessiz bir hata çıktı (adımların tek adımda birleşmesi). Notu da iddia gibi ölç.
   - Önerilen çözümü değil sorunun kökünü ölç. "RAM şişiyor, LTTB ekle" önerisinde şişme gerçekti ama nedeni
     spicelib'in tembel okumasıydı; LTTB hem çözmüyor hem uç değerleri kaybediyordu.
2. **Kaynak netlisti bul.** Antigravity `.scratch/` betiklerini çalıştırdıktan sonra siler. Kopyaları
   `~/.gemini/antigravity/brain/<konuşma-id>/.system_generated/logs/transcript_full.jsonl` içindeki
   `write_to_file` → `CodeContent` adımlarındadır; en yeni klasör son konuşmadır.
3. **Ölçüm betiği yaz.** Önce/sonra değeri aynı betikte ölç, koşumu tekrarla ve sonucun değişmediğini kontrol et.
   LTspice'ı yalnızca `run_netlist` / `run_sweep` / `run_batch` ile başlat (`cpu_guard` açık kalsın).
   - Davranışı gerçek programla üret, yerine koyduğunla değil. Python'un erişim ihlali WerFault'suz 0,09 sn'de
     bitti; LTspice 3,7 sn bekledi. Önce iddianın tetikleyicisini LTspice'ta ara (ör. kendini çağıran `.subckt`).
   - Ortam mirası sorunu gizleyebilir. Claude Code alt süreçlere hata kipi 3'ü geçiriyor ve bu kipte WerFault hiç
     çalışmıyor. Ölçüm alt sürecini `CREATE_DEFAULT_ERROR_MODE` ile başlat.
   - "Eski davranış" kolu tek değişkenle kurulur: değişikliği monkeypatch'le geri al (ör.
     `adapter_mod._assign_to_job = lambda p: False`), ayrı bir kod kopyası tutma.
   - Süreler arka plan yüküyle 2-3 kat oynar (açık tarayıcı ve editör). Betiği birkaç kez koş, belgeye aralığı
     yaz; bellek, dosya boyutu, sayım ve çıkış kodu yükten bağımsızdır.
4. **Logdan doğrula.** Seçeneğin uygulandığını gör (`Maximum thread count: N`, `method = Gear`, `syntax error` yok).
   Salınım varsa `.options method=gear` ile tekrar koş. Zaman aşımında logu oku: livelock olabilir.
5. **Raporla, sonra uygula.** Tablo: iddia | ölçüm | sonuç (tuttu / tutmadı / zararlı). Yalnızca tutanları uygula;
   hız kazancını son kod üzerinde yeniden ölçmeden yazma. Simülasyon içeren her doğrulama ayrıca
   `SimulationReport` ile HTML rapor üretir (GEMINI.md "Simülasyon raporu"): soru, yöntem, dalga formu, tek
   değişkenli bileşen etkileri, yeni etkiler, güvenilirlik. Reddedilen öneri de ölçümüyle rapora girer.
   - Uyguladıktan sonra `test-reviewer`'ın bulgularını mutantla doğrula. Önce mutantın sağ kaldığını gör, sonra
     testi yaz ve öldüğünü gör. `scripts/mutants.py` çıktısında hangi testin öldürdüğüne bak: yanlış nedenle ölen
     mutant (sahte süreçte olmayan metot, uç noktayı düşüren seyreltme) boşluğu gizler (2026-10-06: 6 bulgu, 6 mutant).
6. **Dersleri kaydet.**
   - Ölçümler ve gerekçe: ilgili `docs/LESSONS_LEARNED_*.md` (PERFORMANCE, LTSPICE_REFERENCE).
   - Sayıları üreten betik: `examples/` altına kalıcı örnek olarak (belgedeki sayılarla senkron kalmalı).
   - Ajan kuralı: `GEMINI.md` (CLAUDE.md onu içe aktarır; iki ajan da aynı kuralı okur).
   - Davranış değişikliği: `CHANGELOG.md` "Yayımlanmamış" bölümü.
