# explainer-gif: API başvurusu

Betikler `.agents/skills/explainer-gif/scripts/` altında. Python'dan kullanmak için klasörü `sys.path`'e ekle:

```python
sys.path.insert(0, str(ROOT / ".agents/skills/explainer-gif/scripts"))
from gif_archetypes import render_scope_sweep_gif
```

Bütün `render_*` fonksiyonları yazdıkları `.gif`'in `Path`'ini döndürür.

## Ortak girdiler

- `t`: zaman ekseni (birim serbest; `xlabel`'da yaz). Her iz `t` ile aynı uzunluktadır, yoksa `ValueError`.
- `panels`: `[{"title": str, "ylabel": str, "ylim": (alt, üst) | None, "traces": [{"y": dizi, "label": str, "color": rol}]}]`.
  `ylim` verilmezse izlerden %12 payla hesaplanır. Bir panelde birden çok etiketli iz varsa lejant çizilir.
- `stages`: `[{"t": başlangıç, "text": str, "role": rol}]`. O andaki metin, `t`'si geçilmiş son aşamadır; ilk
  aşamadan önce metin yoktur. Metin şeklin üstünde, başlığın altında durur.
- `events`: `[{"t": an, "panel": dizin, "xy": (x, y), "offset": (dx, dy), "text": str, "role": rol, "rad": eğrilik}]`.
  `xy` eğrinin üstündeki ölçülen nokta, `offset` balonun o noktaya göre kayması (punto). Balon tarama `t`'ye
  gelince açılır, döngü sonuna kadar kalır; olay anı bir vurgu karesidir (uzun bekler).
- `event_windows`: `[(t0, t1)]` ağır çekim aralıkları; verilmezse normalize eğimin yüksek olduğu bölgeler kullanılır.
- `highlight_times`: tam bir kareye düşürülüp bekletilecek ek anlar. `poster_time`: kare 0'daki imleç anı
  (verilmezse son an).
- Roller: `blue`, `amber`, `emerald`, `rose`, `violet`, `slate`; koyu temada ayrıca `cyan` (varsayılan).

## `render_scope_sweep_gif` / `render_phosphor_scope_gif`

```python
render_scope_sweep_gif(
    out_path, t, panels, *, title="", xlabel="", stages=(), events=(), event_windows=None,
    n_sweep_frames=52, poster_time=None, highlight_times=(), min_text_dwell_ms=1750, fps=18,
    figsize=(7.55, 4.35), dpi=150,
)
```

Kare sayısı `n_sweep_frames + 1` (kare 0 poster). `render_phosphor_scope_gif` aynı imzayı taşır (varsayılan
`n_sweep_frames=50`, `min_text_dwell_ms=1800`); kareler koyu zemin + ışıma ile birleştirilir ve `fastoctree` paletle
kodlanır; açık temadan birkaç kat yavaştır.

Bu depoda bir LTspice koşumundan örnek. Balonun anı mutlak bir zaman olmalı: `WHEN` biçimli `.meas` kullan
(`TRIG/TARG` bir süre verir, an değil):

```python
import numpy as np
from ltspice_llm.report import format_value, read_trace
from study_helpers import report_path            # examples/ altındaki betikten

T_ON = 5e-3                                      # girdi: PULSE'ın açık kalma süresi (netliste de bu yazılır)
# netlistte: .meas tran t63 WHEN V(out)=0.632*5 RISE=1
t, v_out = read_trace(result, "V(out)")
_, i_c = read_trace(result, "I(C1)")
t_ms = t * 1e3
t63 = result.measurements["t63"]                 # .meas'ten; elle sayı yazma
t_off_ms = T_ON * 1e3
render_scope_sweep_gif(
    report_path("rc_dolma.gif"), t_ms,
    [{"title": "Çıkış gerilimi", "ylabel": "V", "traces": [{"y": v_out, "label": "V(out)", "color": "blue"}]},
     {"title": "Kondansatör akımı", "ylabel": "mA", "traces": [{"y": i_c * 1e3, "label": "I(C1)", "color": "amber"}]}],
    xlabel="Zaman (ms)",
    stages=[{"t": 0.0, "text": "Kaynak açık: çıkış dolar", "role": "blue"},
            {"t": t_off_ms, "text": "Kaynak kapalı: çıkış boşalır", "role": "rose"}],
    events=[{"t": t63 * 1e3, "panel": 0, "xy": (t63 * 1e3, float(np.interp(t63, t, v_out))),
             "offset": (24, -22), "text": f"%63,2: {format_value(t63, 'ms', 1e-3, 2)}", "role": "blue"}],
)
```

`.meas` adı ve satırı kendi devrene göre değişir; yöntem aynıdır: sayı `.meas`'ten, iz `.raw`'dan.

## Tarama (`.step`)

`read_trace` yalnızca ilk adımı okur; `result.measurements[ad]` steplenmiş koşumda adımların ortalamasıdır. Adım
başına iz ve sayı için:

```python
from ltspice_llm.parser import ALL_TRACES, open_raw, raw_axis_values, raw_steps, raw_wave

raw = open_raw(result.raw_path, traces=ALL_TRACES)   # .raw yanındaki .log ile birlikte okunur
grid = np.linspace(t_start, t_stop, 1000)            # her adımın ekseni ayrıdır (değişken adım): ortak ızgaraya taşı
cases = []
for k in raw_steps(raw):
    y = np.interp(grid, raw_axis_values(raw, k), raw_wave(raw, "V(out)", k))
    value = result.stepped_measurements["t63"][k]["t63"]   # adım k'nın kendi ölçümü
    cases.append({"label": f"R = {format_value(R_VALUES[k], 'kΩ', 1e3, 1)}", "y": y,
                  "note": format_value(value, "ms", 1e-3, 2)})
```

`.ac` taramasında izler karmaşıktır: genliği (`np.abs`, dB için `20*np.log10`) çiz, frekansı `np.log10(f)` ekseninde
ver (arketiplerde log eksen yok).

## `render_parameter_morph_gif`

```python
render_parameter_morph_gif(
    out_path, x_common, cases, *, title="", xlabel="", ylabel="", ylim=None, interp_steps=12,
    min_text_dwell_ms=1800, fps=18, figsize=(7.55, 4.35), dpi=150,
)
```

`cases = [{"label": "R = 1 kΩ", "y": dizi, "note": "ölçülen değer", "color": rol}, ...]`, en az iki durum, her `y`
`x_common` ile aynı uzunlukta. Tüm durumlar soluk kesikli çizgiyle arkada durur; etkin eğri kosinüs yumuşatmasıyla
bir sonrakine geçer. Kare sayısı `(len(cases) - 1) * interp_steps + 1`. Burada kare 0 poster değildir, ilk durumdur.
Rozet etkin durumun etiketini gösterir: geçişin ilk yarısında (karışım payı `s < 0,5`) eski durum, sonra yeni durum.

Ara kareler iki simüle edilmiş eğrinin doğrusal karışımıdır, simülasyon değildir: kayan bir rezonans tepesi
karışımda kayan tek tepe değil, iki tümsek olarak görünür. Bu yüzden `.step` değerlerini sık seç ve `interp_steps`'i
küçük tut; rozetteki sayı yalnızca durumun kendi `.meas` değeridir.

## `capture_html_scene_to_gif` (isteğe bağlı, Playwright)

```python
capture_html_scene_to_gif(
    html_content, sample_times_ms, out_path, *, poster_index=0, highlight_indices=(), state_label_fn=None,
    min_dwell_ms=1750, width=1132, height=652, canvas_selector="canvas", fps=18,
)
```

Sayfa `window.renderAtTime(t_ms)` tanımlar ve kendi kendine yeter: kütüphaneleri (Three.js vb.) satır içi göm. Sahne ağ
kapalı çalışır: dış adresler ve WebSocket yüklenmez, göreli adresler çözülmez. `performance.now`, `Date.now` ve `requestAnimationFrame` sanal saate bağlanır; `Math.random` ve
`setTimeout` bağlanmaz. Poster önce yakalanıp zaman geri sarıldığı için sahne yalnızca `t`'nin fonksiyonu olmalı;
öyleyse aynı girdi aynı GIF'i verir. Kare 0, `sample_times_ms[poster_index]` anının posteridir. `highlight_indices` ve
`state_label_fn(i, t)`'nin `i`'si `sample_times_ms` dizinidir.

## Kendi kare döngün (`gif_encoder`)

Arketipe uymayan sahnede (devre şeması, mekanik benzetme) şu kalıbı izle:

```python
from gif_encoder import (adaptive_event_samples, build_pacing_schedule, encode_master_palette_gif,
                         enforce_minimum_dwell, integrate_particle_phases, sample_polyline_points)

sample_t = adaptive_event_samples(t[0], t[-1], 48, event_windows=[(t_edge - d, t_edge + d)], snap_points=[t_edge])
anchors = [int(np.argmin(abs(sample_t - t_edge))) + 1]       # +1: kare 0 poster
phases = integrate_particle_phases(np.interp(sample_t, t, i_trace))   # (kare, tanecik) konumları

frames, labels = [draw(t_poster, full=True)], [stage_text(t_poster)]  # draw: Figure'ı güncelleyip RGB döndürür
for k, tk in enumerate(sample_t):
    xs, ys = sample_polyline_points(wire_vertices, phases[k])
    frames.append(draw(tk, particles=(xs, ys)))
    labels.append(stage_text(tk))                             # zaman penceresinden, anlık eşikten değil

durations = build_pacing_schedule(len(frames), fps=18, highlight_indices=anchors)
durations = enforce_minimum_dwell(durations, labels, min_dwell_ms=1750, anchor_indices=anchors)
encode_master_palette_gif(frames, durations, "out.gif", state_labels=labels)
```

- `adaptive_event_samples`: `snap_points`'teki her an ayrı bir iç örneğe düşer, ilk ve son örnek uçlarda kalır. İç
  örnekten (`n_frames - 2`) çok an varsa `ValueError`. Arketiplerde bu anlar `events`, `highlight_times` ve
  `poster_time`'dır; sığmıyorsa `n_sweep_frames`'i artır.
- `required_reading_ms(metin, min_dwell_ms)`: `max(min_dwell_ms, min(450 + 230 × kelime, 2700))`, 10 ms'ye yukarı
  yuvarlanmış; boş metin 0.
- `enforce_minimum_dwell` hiçbir kareyi kısaltmaz; kare 0'ı (poster) `skip_frame_zero=True` iken denetlemez (tek kareli
  dizide ayrı poster yoktur, o kare denetlenir).
  Kare 0 poster değilse hem buna hem `encode_master_palette_gif`'e `skip_frame_zero=False` ver.
- `encode_master_palette_gif(..., method="mediancut" | "fastoctree")`: açık zeminde `mediancut`, ışımalı koyu
  zeminde `fastoctree`. `state_labels` verilirse metin kalış süreleri GIF yorumuna yazılır, `validate_gif` okur.
  Pillow birbirinin aynısı ardışık kareleri tek kareye indirip sürelerini toplayabilir.
- Koyu tema katmanları: `hdr_compositor.compose_hdr_frame(base, emit, hud_rgba)`, zemin için `build_studio_vignette`
  ve `blend_static_base`, sabit desen için `make_bayer_dither_field`.

## `validate_gif.py`

`audit_gif(path, max_size_mb=3.5)` sözlük döndürür (`passed`, `errors`, `warnings`, `frame_count`, `duration_s`,
`dwell_runs`, `min_dwell_ms`...). Hata: tek kareli dosya, 1200 ms'den kısa metin, boş poster, hareketsiz kareler.
Uyarı: sonsuz döngü yok, 20 ms'den kısa kare, 900 ms'den kısa poster ya da kapanış, 1500 ms'den kısa metin, tek
karelik parlama, 3,5 MB üstü. `render_storyboard(path, png)` posteri, en uzun bekleyen kareleri ve son kareyi 3 × 2
film şeridine koyar.
