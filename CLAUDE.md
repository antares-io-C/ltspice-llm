# CLAUDE.md

Ajan kurallarının tek kaynağı GEMINI.md'dir (Antigravity ile ortak). Yeni bir ajan kuralı ya da ders oraya
yazılır; bu dosyada yalnızca Claude Code'a özgü olanlar durur.

@GEMINI.md

## Claude Code'a özgü

- Kullanıcıyla Türkçe yazış.
- GEMINI.md'deki "Terminal komutları" bölümü Antigravity'nin komut onayı içindir, Claude Code için geçerli
  değildir. Geçici betikleri oturum scratchpad'ine ya da `.scratch/`'a yaz.
- **`.asc` dosyasını Edit/Write araçlarıyla değiştirme.** Bu araçlar cp1252 dosyayı UTF-8 sanar: ilgisiz tek bir
  satır değişse bile dosyadaki her `µ` (0xB5) U+FFFD olur ve `.1µ` 0,1 F okunur (ölçüldü, 2026-10-01). Read aracı
  da `µ`'yü `�` gösterir. Değiştirmek için `AscSchematic` / `write_asc_file`, değerleri okumak için
  `adapter.inspect_schematic()` kullan. `.claude/settings.json` Edit/Write'ı her yerdeki `.asc` için engeller;
  Claude Code bu kuralı Bash'teki `cp`/`mv`/`rm` yollarına da uygular.
- Bash aracında komut metnindeki her `\\` tek `\`'e iner (tek tırnak ve `<<'EOF'` içinde de); tek `\` korunur.
  Çift ters eğik çizgi içeren kodu Write aracıyla dosyaya yaz ya da PowerShell aracını kullan. Git Bash'te
  `taskkill //F //PID <n>`.
- Çıktı boruya ya da dosyaya giderken Python Windows'un ANSI kod sayfasını kullanır. Bu sayfa makineye göre değişir:
  ilk makinede cp1252 (Türkçe harfleri basamaz), şimdiki makinede cp1254 (Ω, →, ✓ gibi işaretleri basamaz). İkisinde
  de bu karakterleri basan Python çöker. `.claude/settings.json` bu yüzden `PYTHONIOENCODING=utf-8` verir.
  `PYTHONUTF8=1` kullanma: `open()` varsayılanını da UTF-8 yapar.
- `tests/conftest.py` her pytest oturumuna ayrı `cpu_guard` kilit dizini verir, yani pytest'in LTspice koşumları
  başka süreçlerle iş parçacığı bütçesi paylaşmaz. PreToolUse hook'u bu yüzden batch LTspice çalışırken pytest'i
  (ve pytest'in LTspice'ı çalışırken örnek/`.scratch` betiklerini) engeller. pytest'i komutun bölümlerinde
  (`&& ; |`) çalışan programdan tanır: `pytest`, `python -m pytest`, `--full` ile `check.py`/`flows.py`/`isolation.py`.
  `pip install pytest` ya da tırnak içinde geçen `pytest` engellenmez. LTspice'sız koşum için `LTSPICE_EXE` var
  olmayan bir dosyayı göstermeli (`LTSPICE_EXE=C:/yok/x.exe`, `export` ya da `$env:`). Tam paket LTspice ile, örnekler
  dahil ~8 dk (i7-7500U; örnekler ~6 dk, `-m "not examples"` atlar);
  LTspice'sız alt küme `LTSPICE_EXE=C:/yok/x.exe python -m pytest tests -q` ~10 sn (LTspice testleri atlanır).
- Hook'lar `.claude/hooks/`'ta. Stop hook'u her tur sonunda `.asc` bütünlüğünü (U+FFFD, UTF-8 `µ`, BOM) denetler,
  Python dosyaları değiştiyse `python scripts/check.py --steps lint,tests` koşar ve kırıksa turu bir kez geri
  çevirir. `guard_tests.py` (PreToolUse, Edit/Write) test silen, skip/xfail ekleyen ya da assert azaltan
  düzenlemede, `tests/usecases/*.json`'da da senaryo/eylem silen ya da denetimi gevşeten düzenlemede
  kullanıcıya sorar. SessionStart hook'u LTspice.exe değişince ve oturum başında kalmış batch LTspice
  varsa uyarır. Kişisel olarak kapatmak için `.claude/settings.local.json`'a `"disableAllHooks": true`.
- Test ekledikten ya da değiştirdikten sonra `test-reviewer` subagent'ıyla bağımsız inceleme yap
  (`.claude/agents/test-reviewer.md`); bulguları doğrulamadan uygulama.
- `git clean` ve `git reset --hard` izin kurallarıyla engellidir: geri alınamazlar.
- Bir yapay zekâ incelemesini ölçerek doğrulama ve dersleri kaydetme akışı: `/verify-review`.
- Simülasyon raporunu (GEMINI.md "Simülasyon raporu") yerel yolun yanında Artifact olarak da yayımla.
  Betiğin yazdığı iskeletsiz `reports/<ad>.parca.html` dosyasını yayımla; aynı dosya yoluyla yeniden yayımlamak
  aynı bağlantıyı günceller. Görsel kontrol için Edge başsız kipte ekran görüntüsü alır:
  `msedge --headless=new --window-size=1280,12000 --screenshot=<png> file:///<rapor>`.
