"""
Ajan skill'lerinin (``.agents/skills/``) testleri.

Skill betikleri ``ltspice_llm``'e bağlı değildir ve paket olarak kurulmaz; burada ``sys.path``'e dokunmadan, takma adlı
bir paket olarak yüklenir (göreli içe aktarmalar çalışır, başka testlerin içe aktarmaları etkilenmez). Belgelenen
komut satırı kullanımı ayrıca ayrı süreçte sınanır.

Beklentilerin kaynakları:
- Skill biçimi: Agent Skills belirtimi (https://agentskills.io/specification, 2026-11-03'te okundu): ``name`` 1-64
  karakter, küçük harf/rakam/tire, tireyle başlayıp bitmez, ``--`` içermez, klasör adıyla aynıdır; ``description``
  1-1024 karakter; ``compatibility`` en çok 500 karakter; SKILL.md'nin 500 satırdan kısa tutulması önerilir.
- Claude Code skill'leri yalnızca ``.claude/skills/``'ten okur; ``.claude/skills/<ad>/SKILL.md`` kanonik dosyanın
  önbilgisini birebir taşıyan bir yönlendirmedir (CLAUDE.md "Claude Code'a özgü").
- Okuma süresi yardımcıları: fonksiyonların kendi sözleşmesi ("en az ``min_dwell_ms``", "süre kısaltılmaz"); koşum
  dizileri test içinde ``itertools.groupby`` ile bağımsız hesaplanır.
- GIF kare süreleri: GIF89a belirtimi, Graphic Control Extension "Delay Time" alanı yüzde bir saniye birimindedir;
  10 ms'nin katı olan süre okununca aynen dönmelidir.
- Türkçe büyük harf: Türk alfabesinde i'nin büyüğü İ, ı'nın büyüğü I.
- GIF'in sunumda oynaması için python-pptx dosyayı değiştirmeden gömmelidir: gömülü bayt dizisi kaynakla aynı.
- Örnekleme, bekleme ve denetim eşikleri: ``gif_encoder``/``validate_gif``/``validate_deck`` belgeleri, SKILL.md kuralları ve
  ``references/archetypes.md``; elle hesaplar testin yanında.
- Sahnenin ağ erişimi: Playwright 1.64.1 + Edge ile ölçüldü (2026-11-07). Yerel ağ koruması kapatılınca dokuz kanal (script,
  css, img, iframe, fetch, XHR, EventSource, sendBeacon, WebSocket) yerel sunucuya ulaştı, ``page.route`` ile kesince
  WebSocket yine ulaştı, ``offline=True`` ile hiçbiri ulaşmadı ve sahne yüklendi.
"""

import importlib
import importlib.util
import io
import itertools
import json
import math
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest
import yaml
from hypothesis import example, given
from hypothesis import strategies as st
from PIL import Image, ImageSequence
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.presentation import Presentation as PresentationObject
from pptx.util import Inches

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".agents" / "skills"
SKILL_NAMES = sorted(p.name for p in SKILLS.iterdir() if (p / "SKILL.md").is_file())
EXAMPLE_SPEC = SKILLS / "narrative-pptx" / "assets" / "example_spec.json"


def _load_scripts(skill: str):
    """``.agents/skills/<skill>/scripts`` klasörünü ``skill_<ad>`` adlı paket olarak yükler."""
    alias = "skill_" + skill.replace("-", "_")
    if alias not in sys.modules:
        scripts = SKILLS / skill / "scripts"
        spec = importlib.util.spec_from_file_location(
            alias, scripts / "__init__.py", submodule_search_locations=[str(scripts)]
        )
        package = importlib.util.module_from_spec(spec)
        sys.modules[alias] = package
        spec.loader.exec_module(package)
    return alias


def _module(skill: str, name: str):
    return importlib.import_module(f"{_load_scripts(skill)}.{name}")


def _frontmatter(path: Path):
    """``(önbilgi metni, alanlar, gövde)``. Katlanmış (``>-``) değerler tek satıra birleştirilir."""
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), path
    end = text.index("\n---\n", 4)
    block = text[4:end]
    fields, key = {}, None
    for line in block.splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] = (fields[key] + " " + line.strip()).strip()
        else:
            key, _, value = line.partition(":")
            key, value = key.strip(), value.strip()
            fields[key] = "" if value in (">", ">-", "|", "|-") else value
    return block, fields, text[end + len("\n---\n"):]


# ------------------------------------------------------------------- skill biçimi ve Claude Code yönlendirmeleri
def test_skill_set_is_listed():
    assert SKILL_NAMES == ["explainer-gif", "narrative-pptx", "verify-review"]


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_frontmatter_follows_agent_skills_spec(name):
    path = SKILLS / name / "SKILL.md"
    _, fields, _ = _frontmatter(path)
    assert fields["name"] == name
    assert 1 <= len(name) <= 64 and re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", name)
    assert 1 <= len(fields["description"]) <= 1024
    assert 1 <= len(fields["compatibility"]) <= 500
    assert len(path.read_text(encoding="utf-8").splitlines()) < 500


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_frontmatter_is_valid_yaml(name):
    """Belirtim önbilgiyi YAML sayar. ``_frontmatter`` satırları böler, ``description: a: b``'yi de kabul ederdi."""
    block, fields, _ = _frontmatter(SKILLS / name / "SKILL.md")
    assert yaml.safe_load(block) == fields


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_claude_code_stub_mirrors_canonical_skill(name):
    stub = ROOT / ".claude" / "skills" / name / "SKILL.md"
    assert stub.is_file(), f"Claude Code .agents/skills'i okumaz; {stub} yönlendirmesini oluştur (CLAUDE.md)."
    canonical_block, _, _ = _frontmatter(SKILLS / name / "SKILL.md")
    stub_block, _, stub_body = _frontmatter(stub)
    assert stub_block == canonical_block
    assert stub_body.count(f"`.agents/skills/{name}/SKILL.md`") == 1


def test_every_pointer_stub_has_a_canonical_skill():
    pointers = [p for p in (ROOT / ".claude" / "skills").glob("*/SKILL.md")
                if "`.agents/skills/" in p.read_text(encoding="utf-8")]
    assert sorted(p.parent.name for p in pointers) == SKILL_NAMES


@pytest.mark.parametrize("name", SKILL_NAMES)
def test_skill_docs_point_at_files_and_functions_that_exist(name):
    """Belgedeki bağlantılar, betik yolları ve ``modül.ad`` atıfları var olan şeyleri göstermeli (eski belge yakalanır)."""
    skill = SKILLS / name
    modules = sorted(p.stem for p in (skill / "scripts").glob("*.py") if p.stem != "__init__")
    docs = [skill / "SKILL.md", *sorted((skill / "references").glob("*.md"))]
    attr_refs = 0
    for doc in docs:
        text = doc.read_text(encoding="utf-8")
        for target in re.findall(r"\]\(([^)#\s]+)\)", text):
            if not target.startswith("http"):
                assert (doc.parent / target).is_file(), (doc.name, target)
        for skill_name, script in re.findall(r"\.agents/skills/([a-z-]+)/scripts/([a-z_]+\.py)", text):
            assert (SKILLS / skill_name / "scripts" / script).is_file(), (doc.name, script)
        for module, attr in re.findall(rf"\b({'|'.join(modules)})\.([A-Za-z_]\w*)", text) if modules else ():
            if attr != "py":
                assert hasattr(_module(name, module), attr), (doc.name, f"{module}.{attr}")
                attr_refs += 1
    assert attr_refs >= (3 if modules else 0)   # betiksiz skill'de (verify-review) modül atfı yok


# ------------------------------------------------------------------- explainer-gif: okuma süresi yardımcıları
labels = st.one_of(st.sampled_from(["", "a", "iki kelime", "dört kelimelik bir durum", "on kelimelik " * 5]),
                   st.text(alphabet=" \ta", max_size=12))   # kelime sayımı boşluk türüne ve sayısına bağlı olmamalı


def _runs(labels_, start):
    """Ardışık aynı etiketlerin (metin, ilk, son) kare dizinleri; boş etiketler atlanır (bağımsız oracle)."""
    out, k = [], start
    for text, group in itertools.groupby(labels_[start:], key=lambda s: s.strip()):
        n = len(list(group))
        if text:
            out.append((text, k, k + n - 1))
        k += n
    return out


def _reading_ms(text, min_dwell):
    """Belgelenen formül (explainer-gif SKILL.md kural 6, archetypes.md): 10 ms'ye yukarı yuvarlanmış
    ``max(min_dwell, min(450 + 230 × kelime, 2700))``; boş metin 0."""
    words = len(text.split())
    return 0 if not words else math.ceil(max(min_dwell, min(450 + 230 * words, 2700)) / 10) * 10


@pytest.mark.parametrize("text, min_dwell, expected", [
    ("a", 0, 680),                          # 450 + 230
    ("dört kelimelik bir durum", 0, 1370),  # 450 + 4 × 230
    ("on kelimelik " * 5, 0, 2700),         # 450 + 10 × 230 = 2750 -> üst sınır
    ("on kelimelik " * 5, 3000, 3000),      # alt sınır üst sınırdan büyük
    ("a", 1754, 1760),                      # yukarı yuvarlama
    ("", 5000, 0),
    ("a  b", 0, 910),                       # iki boşluk iki kelimeyi üç yapmaz: 450 + 2 × 230
])
def test_required_reading_ms_matches_documented_formula(text, min_dwell, expected):
    assert _module("explainer-gif", "gif_encoder").required_reading_ms(text, min_dwell_ms=min_dwell) == expected


@given(text=labels.filter(str.strip), min_dwell=st.integers(0, 6000))
@example(text="a", min_dwell=3000)   # kelime payının üst sınırı (2700 ms) alt sınırı eziyordu
@example(text="a", min_dwell=1754)   # en yakın 10 ms'ye yuvarlama alt sınırın altına iniyordu
def test_required_reading_ms_never_below_the_floor(text, min_dwell):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    got = gif_encoder.required_reading_ms(text, min_dwell_ms=min_dwell)
    assert got >= min_dwell
    assert got == _reading_ms(text, min_dwell)


@given(
    frames=st.lists(st.tuples(st.integers(0, 3000), labels), min_size=1, max_size=30),
    min_dwell=st.integers(0, 4000),
    anchors=st.sets(st.integers(0, 29), max_size=6),
    skip_frame_zero=st.booleans(),
)
@example(  # 180 ms eksik: vurgu karesine 130, komşulara kalan 50 bölünürken round(2.5) = 2 -> 20 + 20 (toplam 170)
    frames=[(20, ""), (20, "a"), (1490, "a"), (20, "a"), (20, "")], min_dwell=1710, anchors={2},
    skip_frame_zero=True,
)
@example(  # 14 ms eksik en yakın 10'a yuvarlanınca 10 ms ekleniyordu (1746 < 1750)
    frames=[(1736, "a")], min_dwell=1750, anchors=set(), skip_frame_zero=False,
)
@example(  # vurgusuz uzun koşumda 900 ms eksik yalnız ilk üç kareye gider (beş kareye yayılmaz)
    frames=[(20, "a")] * 5, min_dwell=1000, anchors=set(), skip_frame_zero=False,
)
@example(  # sonda boşluklu aynı metin tek koşumdur (strip edilmeden karşılaştırılırsa iki koşum olur)
    frames=[(0, "on kelimelik " * 5)] * 2, min_dwell=0, anchors=set(), skip_frame_zero=False,
)
@example(  # bir koşumda iki vurgu: eksik ilkine gider (rastgele örnekler bunu her tohumda üretmiyordu)
    frames=[(20, "a")] * 5, min_dwell=1000, anchors={1, 3}, skip_frame_zero=False,
)
def test_enforce_minimum_dwell_meets_floor_without_shortening(frames, min_dwell, anchors, skip_frame_zero):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    durations = [d for d, _ in frames]
    texts = [t for _, t in frames]
    out = gif_encoder.enforce_minimum_dwell(
        durations, texts, min_dwell_ms=min_dwell, anchor_indices=sorted(anchors), skip_frame_zero=skip_frame_zero
    )
    assert len(out) == len(durations)
    assert all(new >= old for new, old in zip(out, durations))
    start = 1 if skip_frame_zero and len(durations) > 1 else 0
    runs = _runs(texts, start)
    in_runs = {k for _, first, last in runs for k in range(first, last + 1)}
    assert all(out[k] == durations[k] for k in range(len(out)) if k not in in_runs)
    for text, first, last in runs:
        before, shown, needed = sum(durations[first:last + 1]), sum(out[first:last + 1]), _reading_ms(text, min_dwell)
        assert shown >= min_dwell, (text, first, last, shown)
        assert shown >= needed
        if before >= needed:                       # yeten koşuma dokunulmaz
            assert out[first:last + 1] == durations[first:last + 1]
            continue
        assert shown < needed + 30                 # yalnız eksik eklenir: en çok 3 pay, her biri 10 ms'ye yukarı
        added = [out[k] - durations[k] for k in range(first, last + 1)]
        run_anchors = sorted(a for a in anchors if first <= a <= last)
        if run_anchors:                            # çoğu ilk vurgu karesine, kalanı yalnız komşularına
            assert added[run_anchors[0] - first] == max(added)
            assert 2 * added[run_anchors[0] - first] >= sum(added)   # belge: "çoğu ona"
            assert all(a == 0 for k, a in enumerate(added, first) if abs(k - run_anchors[0]) > 1)
        else:                                      # vurgu yoksa koşumun ilk üç karesine
            assert all(a == 0 for a in added[3:])


def test_single_frame_is_checked_even_when_frame_zero_is_skipped():
    """archetypes.md: tek kareli dizide ayrı bir poster yoktur, kare 0 denetlenir."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    assert gif_encoder.enforce_minimum_dwell([100], ["a"], min_dwell_ms=1000, skip_frame_zero=True) == [1000]
    assert gif_encoder.compute_state_dwell_summary([1000], ["a"], skip_frame_zero=True) == [("a", 0, 0, 1000)]


# ------------------------------------------------------------------- explainer-gif: örnekleme, kare süresi, tanecikler
inner_times = st.floats(0.0, 10.0, exclude_min=True, exclude_max=True)


@given(snaps=st.lists(inner_times, unique=True, max_size=12), spare=st.integers(0, 40),
       windows=st.lists(st.tuples(inner_times, inner_times), max_size=2))
@example(snaps=[2.0, 2.1], spare=4, windows=[])        # n = 8: iki an aynı en yakın örneğe düşüyor, ilki kayboluyordu
@example(snaps=[0.1], spare=5, windows=[])             # en yakın örnek ilk örnek: uç sıfırlaması anı siliyordu
@example(snaps=[9.9], spare=5, windows=[])             # aynısı son örnekte
@example(snaps=[3.0, 3.2, 3.4], spare=7, windows=[])   # n = 12: üç an tek örnekte birleşiyordu
@example(snaps=[9.0, 9.1, 9.2], spare=0, windows=[])   # iç örneklerin tamamı uca yakın anlara gider
@example(snaps=[0.1, 0.2, 0.3], spare=0, windows=[])
def test_adaptive_samples_land_on_every_snap_point(snaps, spare, windows):
    """gif_encoder belgesi ve SKILL.md kural 5: her an tam bir örneğe düşer; uçlar, örnek sayısı ve sıra korunur."""
    n_frames = len(snaps) + 2 + spare
    samples = _module("explainer-gif", "gif_encoder").adaptive_event_samples(
        0.0, 10.0, n_frames, event_windows=windows, snap_points=snaps
    )
    assert len(samples) == n_frames
    assert samples[0] == 0.0 and samples[-1] == 10.0
    assert np.all(np.diff(samples) >= 0.0)
    assert set(snaps) <= set(samples.tolist())


def test_adaptive_samples_stay_sorted_when_floats_collapse():
    """Aralık birkaç ulp olunca örnekler aynı float'a çöker; ``argmin`` eşitlerin ilkini seçer ve an ileri atlayabilir.
    Karşı örnek test-reviewer aramasından (2026-11-07). Sıra ve an yine korunmalı."""
    wide, moment = 1.0000000000000007, 1.0000000000000009
    samples = _module("explainer-gif", "gif_encoder").adaptive_event_samples(
        1.0, 1.000000000000001, 8, event_windows=[(wide, wide)], density_boost=1e8, snap_points=[moment]
    )
    assert np.all(np.diff(samples) >= 0.0)
    assert moment in samples
    assert len(samples) == 8   # belge: ``n_frames`` örnek; çöken değerler birleştirilmez


def test_adaptive_samples_reject_more_moments_than_inner_frames():
    """Uçlar dışında iki iç örnek var, üç an sığmaz: belge "tam bir örneğe" diyor, sessizce düşürmek yerine hata."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    with pytest.raises(ValueError, match="snap_points"):
        gif_encoder.adaptive_event_samples(0.0, 10.0, 4, snap_points=[1.0, 2.0, 3.0])
    # Aralık dışı anlar, uçlar ve tekrarlar iç örnek istemez: tek iç örnek 4'e yeter
    samples = gif_encoder.adaptive_event_samples(0.0, 10.0, 3, snap_points=[-1.0, 0.0, 10.0, 11.0, 4.0, 4.0, 4.0])
    assert samples.tolist() == [0.0, 4.0, 10.0]


def test_adaptive_samples_slow_down_inside_event_windows():
    """density_boost kat sık: 2 birimlik pencere 2 × 3,8 = 7,6, kalan 8 birim 8 ağırlık; 100 örneğin %48,7'si pencerede."""
    samples = _module("explainer-gif", "gif_encoder").adaptive_event_samples(
        0.0, 10.0, 100, event_windows=[(4.0, 6.0)], density_boost=3.8
    )
    assert abs(int(np.sum((samples >= 4.0) & (samples <= 6.0))) - 100 * 7.6 / 15.6) <= 1.5


def test_auto_windows_find_the_step_and_ignore_flat_traces():
    """archetypes.md: ``event_windows`` verilmezse normalize eğimin yüksek olduğu bölgeler kullanılır."""
    archetypes = _module("explainer-gif", "gif_archetypes")
    windows = archetypes._auto_transient_windows(T, [STEP])
    assert len(windows) == 1
    assert 4.5 < windows[0][0] < 5.0 < windows[0][1] < 6.0
    assert archetypes._auto_transient_windows(T, [np.zeros_like(T)]) == []


def test_pacing_schedule_holds_poster_highlights_and_last_frame():
    """Belge: akışta 1000/fps, kare 0'da, vurgu karelerinde ve son karede bekleme; GIF süresi 10 ms adımlıdır (GIF89a).
    1000/18 = 55,6 ms -> 60 ms. Kare 0 vurgu sayılmaz, listeden taşan dizin yok sayılır."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    assert gif_encoder.build_pacing_schedule(5, fps=18, poster_hold_ms=1000, highlight_indices=[0, 2, 9],
                                             highlight_hold_ms=500, end_hold_ms=2000) == [1000, 60, 500, 60, 2000]
    with pytest.raises(ValueError, match="n_frames"):
        gif_encoder.build_pacing_schedule(0)


def test_particles_follow_the_current_sign_and_stop_at_zero():
    """Belge: adım akımın tepesine göre ölçeklenir, akım sıfırken durur, işaret değişince yön değişir. Tepe 2 A, en büyük
    adım 0,1: 2, 2, 0, -1 A için adımlar (kare 0'da adım yok) +0,1, 0, -0,05."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    phases = gif_encoder.integrate_particle_phases([2.0, 2.0, 0.0, -1.0], n_particles=2, max_step_per_frame=0.1)
    np.testing.assert_allclose(phases, [[0.0, 0.5], [0.1, 0.6], [0.1, 0.6], [0.05, 0.55]])
    still = gif_encoder.integrate_particle_phases([0.0, 0.0, 0.0], n_particles=4)
    assert np.all(still == still[0])
    wrapped = gif_encoder.integrate_particle_phases([1.0] * 40, n_particles=3, max_step_per_frame=0.1)
    assert np.all((wrapped >= 0.0) & (wrapped < 1.0))


def test_polyline_points_follow_arc_length_and_skip_zones():
    """Köşeleri (0,0), (2,0), (2,1) olan 3 birimlik yol: konum 1/3 -> 1 birim, 2/3 -> köşe, 5/6 -> köşeden 0,5 yukarı."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    path = [(0.0, 0.0), (2.0, 0.0), (2.0, 1.0)]
    phases = [0.0, 1 / 3, 2 / 3, 5 / 6]
    np.testing.assert_allclose(gif_encoder.sample_polyline_points(path, phases), [[0, 1, 2, 2], [0, 0, 0, 0.5]], atol=1e-12)
    kept = gif_encoder.sample_polyline_points(path, phases, skip_zones=[(0.3, 0.7)])
    np.testing.assert_allclose(kept, [[0, 2], [0, 0.5]], atol=1e-12)
    assert gif_encoder.sample_polyline_points([(1.0, 1.0), (1.0, 1.0)], [0.5]) == ([], [])


# ------------------------------------------------------------------- explainer-gif: kodlama ve doğrulama
def _frames(n, h=24, w=32):
    """Birbirinden farklı ``n`` kare: açık zeminde kayan mavi blok (Pillow aynı kareleri birleştiremesin)."""
    assert n <= w - 4
    out = []
    for k in range(n):
        frame = np.full((h, w, 3), 240, dtype=np.uint8)
        frame[4:12, k:k + 4] = (37, 99, 235)
        out.append(frame)
    return out


def test_comment_prefix_matches_between_encoder_and_validator():
    assert _module("explainer-gif", "gif_encoder").COMMENT_PREFIX == _module("explainer-gif", "validate_gif").COMMENT_PREFIX


@pytest.mark.parametrize("method", ["mediancut", "fastoctree"])
def test_encoded_gif_keeps_frames_durations_and_dwell_metadata(tmp_path, method):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    validate_gif = _module("explainer-gif", "validate_gif")
    durations = [1400, 60, 1700, 60, 1150, 1550]
    texts = ["", "a", "a", "b c", "b c", "b c"]
    path = gif_encoder.encode_master_palette_gif(_frames(6), durations, tmp_path / "x.gif", state_labels=texts,
                                                 method=method)
    with Image.open(path) as im:
        assert im.info["loop"] == 0
        assert [frame.info["duration"] for frame in ImageSequence.Iterator(im)] == durations
    report = validate_gif.audit_gif(path)
    assert report["passed"], report["errors"]
    assert [(r["text"], r["f0"], r["f1"], r["ms"]) for r in report["dwell_runs"]] == [
        ("a", 1, 2, 60 + 1700), ("b c", 3, 5, 60 + 1150 + 1550)]


def test_dwell_metadata_includes_frame_zero_when_it_is_not_a_poster(tmp_path):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    validate_gif = _module("explainer-gif", "validate_gif")
    path = gif_encoder.encode_master_palette_gif(_frames(3), [900, 900, 1800], tmp_path / "x.gif",
                                                 state_labels=["a", "a", "b"], skip_frame_zero=False)
    runs = validate_gif.audit_gif(path)["dwell_runs"]
    assert [(r["text"], r["f0"], r["f1"], r["ms"]) for r in runs] == [("a", 0, 1, 1800), ("b", 2, 2, 1800)]


def test_validator_text_thresholds(tmp_path):
    """validate_gif.py belgesi ve archetypes.md: metin 1200 ms'den kısaysa hata, 1500 ms'den kısaysa uyarı."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    validate_gif = _module("explainer-gif", "validate_gif")
    path = gif_encoder.encode_master_palette_gif(_frames(6), [1400, 1190, 1200, 1490, 1500, 1000], tmp_path / "x.gif",
                                                 state_labels=["", "e", "w1", "w2", "ok", ""])
    report = validate_gif.audit_gif(path)
    assert not report["passed"]
    assert len(report["errors"]) == 1 and report["errors"][0].count("'e'") == 1
    assert [sum(f"'{name}'" in w for w in report["warnings"]) for name in ("w1", "w2", "ok")] == [1, 1, 0]


def test_encoder_rejects_unknown_quantize_method(tmp_path):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    with pytest.raises(ValueError, match="method"):
        gif_encoder.encode_master_palette_gif(_frames(2), [100, 100], tmp_path / "x.gif", method="octree")


def test_all_frames_share_one_palette_so_static_pixels_do_not_flicker(tmp_path):
    """SKILL.md kural 7: kare başına palet arka planı titretir. Zemin 256'dan çok renkli degrade, üst şeritte kayan blok her
    karenin histogramını değiştirir; alt bölge hiç değişmez, ortak palet ve kapalı dither ile her karede aynı renkte kalır."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    h, w = 64, 96
    red = np.tile(np.linspace(0, 255, w, dtype=np.uint8), (h, 1))
    green = np.tile(np.linspace(0, 255, h, dtype=np.uint8)[:, None], (1, w))
    base = np.dstack([red, green, 255 - red])
    frames = []
    for k in range(6):
        frame = base.copy()
        frame[:16, k * 12:k * 12 + 24] = (250, 30, 30)
        frames.append(frame)
    decoded = _decode(gif_encoder.encode_master_palette_gif(frames, [100] * 6, tmp_path / "p.gif"))
    assert len(decoded) == 6
    assert all(np.array_equal(decoded[0][16:], other[16:]) for other in decoded[1:])


def _durations(path):
    with Image.open(path) as im:
        return [frame.info["duration"] for frame in ImageSequence.Iterator(im)]


def _audit(tmp_path, frames, durations, loop=0):
    """Elle yazılmış GIF'i denetler; doğrulayıcı herhangi bir GIF'i okur (validate_gif.py belgesi)."""
    images = [Image.fromarray(frame) for frame in frames]
    path = tmp_path / "x.gif"
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations,
                   **({} if loop is None else {"loop": loop}))
    return _module("explainer-gif", "validate_gif").audit_gif(path)


def test_validator_rejects_a_single_frame_file(tmp_path):
    report = _audit(tmp_path, _frames(1), [1500])
    assert not report["passed"]
    assert sum("hareketli değil" in e for e in report["errors"]) == 1


def test_validator_rejects_a_blank_poster(tmp_path):
    """SKILL.md kural 2: boş eksenle başlayan GIF PDF'te boş görünür."""
    blank = np.full((24, 32, 3), 240, dtype=np.uint8)
    report = _audit(tmp_path, [blank, *_frames(3)], [1400, 60, 60, 1500])
    assert len(report["errors"]) == 1 and "Kare 0" in report["errors"][0]


def test_validator_rejects_frames_without_visible_motion(tmp_path):
    """Denetim kare 0, orta ve son kareye bakar; Pillow aynı ardışık kareleri birleştirdiği için araya farklı kare girer."""
    a, b = _frames(2)
    report = _audit(tmp_path, [a, b, a, b, a], [1400, 60, 60, 60, 1500])
    assert len(report["errors"]) == 1 and "hareket" in report["errors"][0]


@pytest.mark.parametrize("durations, word", [
    ([500, 60, 60, 1500], "Poster Kare"),
    ([1400, 60, 60, 500], "Kapanış karesi"),
    ([1400, 10, 60, 1500], "20 ms'den kısa"),
])
def test_validator_warns_about_short_holds(tmp_path, durations, word):
    """archetypes.md: 900 ms'den kısa poster ya da kapanış ve 20 ms'den kısa kare uyarıdır, hata değil."""
    report = _audit(tmp_path, _frames(4), durations)
    assert report["passed"]
    assert len(report["warnings"]) == 1 and word in report["warnings"][0]


def test_validator_warns_about_missing_loop_and_large_files(tmp_path):
    report = _audit(tmp_path, _frames(4), [1400, 60, 60, 1500], loop=None)
    assert sum("sonsuz döngü" in w for w in report["warnings"]) == 1
    big = _module("explainer-gif", "validate_gif").audit_gif(tmp_path / "x.gif", max_size_mb=1e-6)
    assert sum("boyutu yüksek" in w for w in big["warnings"]) == 1


def test_validator_finds_a_single_frame_flash(tmp_path):
    """Belge: tek karelik parlama. Her kare 1 piksel değişir (Pillow birleştirmesin), 4. karede büyük blok yanıp söner."""
    frames = []
    for k in range(8):
        frame = np.full((48, 64, 3), 240, dtype=np.uint8)
        frame[0, k] = (0, 0, 0)
        if k == 4:
            frame[10:40, 10:50] = (225, 29, 72)
        frames.append(frame)
    report = _audit(tmp_path, frames, [1400, *[100] * 6, 1500])
    assert report["passed"]
    assert len(report["warnings"]) == 1 and "Kare 4" in report["warnings"][0]


def test_storyboard_prefers_the_longest_inner_frames():
    """Belge: en uzun bekleyen kareler. Beş 400 ms'lik vurgu arasında 2000 ms'lik kare: altı yuvanın ikisi uçlara gider."""
    durations = [1400] + [60] * 59 + [1500]
    for k in (10, 20, 30, 40, 50):
        durations[k] = 400
    durations[25] = 2000
    assert 25 in _module("explainer-gif", "validate_gif")._select_storyboard_indices(durations, 6)


@given(durations=st.lists(st.integers(20, 3000), min_size=1, max_size=80))
def test_storyboard_selection_keeps_both_ends_and_distinct_frames(durations):
    """Belge: poster, en uzun bekleyen kareler ve son kare."""
    picks = _module("explainer-gif", "validate_gif")._select_storyboard_indices(durations, 6)
    assert picks == sorted(set(picks))
    assert len(picks) == min(len(durations), 6)
    assert picks[0] == 0 and picks[-1] == len(durations) - 1


# ------------------------------------------------------------------- explainer-gif: arketipler
T = np.linspace(0.0, 10.0, 400)
STEP = np.where(T < 5.0, 1.0, 0.0)
OUT = np.where(T < 5.0, 1.0 - np.exp(-T), (1.0 - np.exp(-5.0)) * np.exp(-(T - 5.0)))
SMALL = {"figsize": (3.2, 2.0), "dpi": 60}


@pytest.mark.parametrize("fn_name", ["render_scope_sweep_gif", "render_phosphor_scope_gif"])
def test_scope_sweep_follows_stage_windows_and_keeps_text_readable(fn_name, tmp_path):
    archetypes = _module("explainer-gif", "gif_archetypes")
    validate_gif = _module("explainer-gif", "validate_gif")
    panels = [{"title": "", "ylabel": "V", "traces": [{"y": STEP, "label": "in"}, {"y": OUT, "label": "out"}]}]
    stages = [{"t": 0.0, "text": "dolma", "role": "blue"}, {"t": 5.0, "text": "boşalma", "role": "rose"}]
    events = [{"t": 1.0, "panel": 0, "xy": (1.0, 0.63), "text": "1τ"}]
    path = getattr(archetypes, fn_name)(tmp_path / "s.gif", T, panels, stages=stages, events=events,
                                        n_sweep_frames=8, min_text_dwell_ms=1800, **SMALL)
    report = validate_gif.audit_gif(path)
    assert report["passed"], report["errors"]
    assert report["frame_count"] == 8 + 1
    assert [r["text"] for r in report["dwell_runs"]] == ["dolma", "boşalma"]
    assert all(r["ms"] >= 1800 for r in report["dwell_runs"])


def test_phosphor_scope_accepts_sizes_that_are_not_whole_pixels(tmp_path):
    """matplotlib Agg tuvali ``int(inç × dpi)`` piksel açar (kırpar): 3,21 × 60 = 192,6 -> 192 piksel."""
    archetypes = _module("explainer-gif", "gif_archetypes")
    validate_gif = _module("explainer-gif", "validate_gif")
    path = archetypes.render_phosphor_scope_gif(tmp_path / "p.gif", T, [{"traces": [{"y": OUT}]}], n_sweep_frames=3,
                                                figsize=(3.21, 2.01), dpi=60)
    report = validate_gif.audit_gif(path)
    assert (report["width"], report["height"]) == (192, 120)


def test_parameter_morph_labels_each_case_from_frame_zero(tmp_path):
    archetypes = _module("explainer-gif", "gif_archetypes")
    validate_gif = _module("explainer-gif", "validate_gif")
    cases = [{"label": "k = 1", "y": np.exp(-T)}, {"label": "k = 2", "y": np.exp(-T / 2)},
             {"label": "k = 4", "y": np.exp(-T / 4), "note": "son"}]
    path = archetypes.render_parameter_morph_gif(tmp_path / "m.gif", T, cases, interp_steps=4,
                                                 min_text_dwell_ms=1800, **SMALL)
    report = validate_gif.audit_gif(path)
    assert report["passed"], report["errors"]
    assert report["frame_count"] == (3 - 1) * 4 + 1
    runs = report["dwell_runs"]
    assert [r["text"] for r in runs] == ["k = 1", "k = 2", "k = 4 · son"]
    assert runs[0]["f0"] == 0
    # Varsayılan beklemelerle (1300/1100/1500 ms + 60 ms kareler) her koşum 1800'ün altında: yalnız eksik eklenir.
    assert all(1800 <= r["ms"] < 1800 + 30 for r in runs)


def test_archetypes_reject_traces_that_do_not_match_the_time_axis(tmp_path):
    archetypes = _module("explainer-gif", "gif_archetypes")
    with pytest.raises(ValueError, match="zaman ekseniyle"):
        archetypes.render_scope_sweep_gif(tmp_path / "x.gif", T, [{"traces": [{"y": T[:-1]}]}], **SMALL)
    with pytest.raises(ValueError, match="x ekseniyle"):
        archetypes.render_parameter_morph_gif(tmp_path / "x.gif", T, [{"label": "a", "y": T}, {"label": "b", "y": T[:5]}])


SWEEPS = ["render_scope_sweep_gif", "render_phosphor_scope_gif"]


@pytest.mark.parametrize("fn_name", SWEEPS)
def test_callout_opens_exactly_on_the_event_frame(fn_name, tmp_path):
    """SKILL.md kural 2 ve 4: poster bütün balonları gösterir (burada poster olaydan önceki bir an); tarama balonu olay
    karesinde açar ve döngü sonuna kadar kalır. Olay karesi en uzun iç karedir. Karşılaştırma aynı anı yalnız vurgulayan
    GIF'le: örnek anları ve süreler aynı, fark yalnız balon. Pencere örnekleri sıklaştırır: olaydan hemen önce de kare
    olmalı, yoksa erken açılan balon görünmez (bu düzenek kör kalmasın diye aşağıda denetlenir)."""
    render = getattr(_module("explainer-gif", "gif_archetypes"), fn_name)
    windows = [(0.0, 4.0)]
    samples = _module("explainer-gif", "gif_encoder").adaptive_event_samples(0.0, 10.0, 12, event_windows=windows,
                                                                             snap_points=[0.5, 2.0])
    assert np.any((samples > 0.95) & (samples < 2.0))
    panels = [{"traces": [{"y": OUT}]}]
    common = {"n_sweep_frames": 12, "poster_time": 0.5, "event_windows": windows, **SMALL}
    event = {"t": 2.0, "panel": 0, "xy": (2.0, float(1.0 - np.exp(-2.0))), "offset": (10, -20), "text": "olay"}
    marked = render(tmp_path / "a.gif", T, panels, events=[event], **common)
    with_callout = _decode(marked)
    without = _decode(render(tmp_path / "b.gif", T, panels, highlight_times=[2.0], **common))
    assert len(with_callout) == len(without) == 12 + 1
    durations = _durations(marked)
    event_frame = 1 + int(np.argmax(durations[1:-1]))
    shown = [float(np.mean(np.abs(a - b))) > 0.5 for a, b in zip(with_callout, without)]
    assert shown == [True] + [False] * (event_frame - 1) + [True] * (len(shown) - event_frame)


@pytest.mark.parametrize("fn_name", SWEEPS)
def test_stage_text_switches_on_the_highlighted_frame_and_holds_are_kept(fn_name, tmp_path):
    """SKILL.md kural 3: metin zaman penceresine bağlıdır, t = 5'te değişir; kural 5: o an tam bir kareye düşer ve bekler;
    kural 2: tarama arketiplerinde poster 1400 ms. Kapanış beklemesi uyarı denetiminden geçer (900 ms)."""
    archetypes = _module("explainer-gif", "gif_archetypes")
    stages = [{"t": 0.0, "text": "dolma", "role": "blue"}, {"t": 5.0, "text": "boşalma", "role": "rose"}]
    path = getattr(archetypes, fn_name)(tmp_path / "s.gif", T, [{"traces": [{"y": OUT}]}], stages=stages,
                                        highlight_times=[5.0], n_sweep_frames=8, **SMALL)
    report = _module("explainer-gif", "validate_gif").audit_gif(path)
    assert report["warnings"] == []
    durations = _durations(path)
    assert durations[0] == 1400
    assert report["dwell_runs"][1]["f0"] == 1 + int(np.argmax(durations[1:-1]))


@pytest.mark.parametrize("fn_name", SWEEPS)
def test_stage_text_is_drawn_on_the_frames(fn_name, tmp_path):
    """``dwell_runs`` etiketi çizimden değil çağrıdan gelir: metnin kareye gerçekten yazıldığını iki metni karşılaştırarak gör."""
    render = getattr(_module("explainer-gif", "gif_archetypes"), fn_name)
    panels = [{"traces": [{"y": OUT}]}]
    first, second = (_decode(render(tmp_path / f"{text}.gif", T, panels, stages=[{"t": 0.0, "text": text}],
                                    n_sweep_frames=3, **SMALL)) for text in ("dolma", "boşalma"))
    assert all(float(np.mean(np.abs(a - b))) > 0.1 for a, b in zip(first, second))


def test_parameter_morph_draws_the_active_label(tmp_path):
    """archetypes.md: etkin durumun etiketi rozette durur. Etiketi farklı iki GIF'in ilk karesi farklı olmalı."""
    render = _module("explainer-gif", "gif_archetypes").render_parameter_morph_gif
    first, second = (_decode(render(tmp_path / f"{i}.gif", T, [{"label": text, "y": np.exp(-T)},
                                                               {"label": "son", "y": np.exp(-T / 2)}],
                                    interp_steps=2, **SMALL)) for i, text in enumerate(("k = 1", "uzun bir durum etiketi")))
    assert float(np.mean(np.abs(first[0] - second[0]))) > 0.1


@pytest.mark.parametrize("fn_name", SWEEPS)
def test_stage_text_changes_on_the_sweep_frames_at_its_moment(fn_name, tmp_path):
    """SKILL.md kural 3: metin zaman penceresine bağlıdır. İki aşamalı GIF tek aşamalıyla t = 5 karesine kadar aynı (poster
    son anı gösterir, orada da ikinci metin), o kareden sonra farklı. Metin posterde takılsa tarama kareleri aynı kalırdı.
    Pencere örnekleri sıklaştırır: an, eşit aralıklı örneklemede başka kareye düşer, kare dizininden hesaplanan zaman ayrışır
    (bu ön koşul aşağıda denetlenir; ortadaki bir an iki hesapta aynı kareye düşüp yanlış kodu geçirirdi)."""
    render = getattr(_module("explainer-gif", "gif_archetypes"), fn_name)
    panels = [{"traces": [{"y": OUT}]}]
    common = {"highlight_times": [2.0], "n_sweep_frames": 12, "event_windows": [(0.0, 4.0)], **SMALL}
    first = {"t": 0.0, "text": "dolma", "role": "blue"}
    second = {"t": 2.0, "text": "uzun ikinci metin", "role": "blue"}
    switched = render(tmp_path / "iki.gif", T, panels, stages=[first, second], **common)
    two, one = _decode(switched), _decode(render(tmp_path / "tek.gif", T, panels, stages=[first], **common))
    assert len(two) == len(one) == 12 + 1
    switch = 1 + int(np.argmax(_durations(switched)[1:-1]))
    assert switch - 1 != int(np.searchsorted(np.linspace(0.0, 10.0, 12), 2.0))
    differs = [float(np.mean(np.abs(a - b))) > 0.5 for a, b in zip(two, one)]
    assert differs == [True] + [False] * (switch - 1) + [True] * (len(differs) - switch)


def test_parameter_morph_keeps_each_label_on_its_own_frames(tmp_path):
    """archetypes.md: kare sayısı (durum - 1) × interp_steps + 1, kosinüs yumuşatması. Üç adımda s = 0; 0,25; 0,75 ve
    s < 0,5 iken etkin durum eskisidir: kareler a, a, b, b, b, c, c. Tam yarı yola düşen adım bilerek yok (iki adımda s
    yuvarlamayla 0,5'in altında kalır). Yalnız ortadaki etiket değişince yalnız o üç kare farklı olmalı."""
    render = _module("explainer-gif", "gif_archetypes").render_parameter_morph_gif

    def frames(i, middle):
        cases = [{"label": "a", "y": np.exp(-T)}, {"label": middle, "y": np.exp(-T / 2)}, {"label": "c", "y": np.exp(-T / 4)}]
        return _decode(render(tmp_path / f"{i}.gif", T, cases, interp_steps=3, **SMALL))

    short, long = frames(0, "b"), frames(1, "uzun bir durum etiketi")
    assert len(short) == len(long) == 2 * 3 + 1
    differs = [float(np.mean(np.abs(a - b))) > 0.5 for a, b in zip(short, long)]
    assert differs == [False, False, True, True, True, False, False]


# ------------------------------------------------------------------- narrative-pptx
def _spec():
    return json.loads(EXAMPLE_SPEC.read_text(encoding="utf-8"))


def test_example_spec_covers_every_layout_once():
    deck_builder = _module("narrative-pptx", "deck_builder")
    layouts = [s["layout"] for s in _spec()["slides"]]
    assert len(layouts) == 7
    assert sorted(layouts) == sorted(deck_builder.LAYOUT_BUILDERS)


def test_example_deck_with_chart_and_gif_builds_and_passes_audit(tmp_path):
    deck_builder = _module("narrative-pptx", "deck_builder")
    chart_artist = _module("narrative-pptx", "chart_artist")
    validate_deck = _module("narrative-pptx", "validate_deck")
    gif_encoder = _module("explainer-gif", "gif_encoder")
    chart_artist.render_annotated_panels(
        tmp_path / "chart.png",
        [{"title": "çıkış", "traces": [{"x": T, "y": OUT, "label": "out"}],
          "callouts": [{"xy": (1.0, 0.63), "text": "1τ", "offset": (20, -20)}]}],
        dpi=60,
    )
    gif = gif_encoder.encode_master_palette_gif(_frames(4), [1400, 60, 60, 1500], tmp_path / "anim.gif")
    spec = _spec()
    by_layout = {s["layout"]: s for s in spec["slides"]}
    by_layout["mental_model"]["image"] = "chart.png"
    by_layout["evidence_bento"]["image"] = "anim.gif"

    out = deck_builder.build_deck(spec, tmp_path / "deck.pptx", base_dir=tmp_path)
    report = validate_deck.audit_pptx(out)
    assert report["passed"], report["errors"]
    assert report["warnings"] == []

    prs = Presentation(str(out))
    assert len(prs.slides) == len(spec["slides"])
    all_text = []
    for slide, slide_spec in zip(prs.slides, spec["slides"]):
        assert slide.notes_slide.notes_text_frame.text == slide_spec["speaker_notes"]
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
        if "assertion" in slide_spec:
            assert texts.count(slide_spec["assertion"]) == 1
        all_text += texts
    assert all_text.count("GEÇİŞ") == 1   # "language": "tr" ile process_arc'taki "geçiş" çipi

    layouts = [s["layout"] for s in spec["slides"]]
    assert _picture_blobs(prs.slides[layouts.index("evidence_bento")]) == [gif.read_bytes()]
    assert _picture_blobs(prs.slides[layouts.index("mental_model")]) == [(tmp_path / "chart.png").read_bytes()]


def _picture_blobs(slide):
    return [sh.image.blob for sh in slide.shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE]


def test_turkish_caps_keeps_dotted_capital_i():
    themes = _module("narrative-pptx", "themes")
    assert themes.get_theme(language="tr").caps("Geçiş ılık") == "GEÇİŞ ILIK"
    assert themes.get_theme(language="en").caps("title") == "TITLE"
    with pytest.raises(ValueError, match="language"):
        themes.get_theme(language="de")


def test_audit_flags_missing_notes_and_overflowing_text(tmp_path):
    deck_builder = _module("narrative-pptx", "deck_builder")
    validate_deck = _module("narrative-pptx", "validate_deck")
    spec = {"slides": [{"layout": "evidence_bento", "assertion": "Kısa iddia.",
                        "mechanism": {"title": "Neden", "body": "çok uzun bir açıklama " * 120}}]}
    report = validate_deck.audit_pptx(deck_builder.build_deck(spec, tmp_path / "bad.pptx"))
    assert sum("Konuşmacı notu" in w for w in report["warnings"]) == 1
    assert sum("Olası metin taşması" in w and "çok uzun" in w for w in report["warnings"]) == 1


def test_spec_warnings_flag_dropped_items_and_leftover_placeholders():
    deck_builder = _module("narrative-pptx", "deck_builder")
    # assets/example_spec.json'da ⟨.meas⟩ yer tutucusu 3., 4., 5. ve 6. slaytlarda (elle sayıldı)
    warnings = deck_builder.spec_warnings(_spec())
    assert [int(re.match(r"Slayt (\d+):", w).group(1)) for w in warnings] == [3, 4, 5, 6]
    assert all(w.count("yer tutucusu") == 1 for w in warnings)
    crowded = {"slides": [{"layout": "evidence_bento", "metrics": [{"value": "1"}] * 3},
                          {"layout": "evidence_bento", "metrics": [{"value": "1"}] * 2}]}
    assert deck_builder.spec_warnings(crowded) == ["Slayt 1: metrics 3 öğe; evidence_bento en çok 2 çizer, fazlası düşer."]


@pytest.mark.parametrize("language, gif_pill, badge", [("tr", "▶ CANLI GIF", "MEKANİZMA"),
                                                       ("en", "▶ LIVE GIF", "MECHANISM")])
def test_default_labels_follow_deck_language(tmp_path, language, gif_pill, badge):
    deck_builder = _module("narrative-pptx", "deck_builder")
    gif = _module("explainer-gif", "gif_encoder").encode_master_palette_gif(_frames(3), [1400, 60, 1500],
                                                                             tmp_path / "a.gif")
    spec = {"language": language,
            "slides": [{"layout": "evidence_bento", "image": gif.name, "mechanism": {"title": "x", "body": "y"}}]}
    prs = Presentation(str(deck_builder.build_deck(spec, tmp_path / "d.pptx", base_dir=tmp_path)))
    texts = [sh.text_frame.text for sh in prs.slides[0].shapes if sh.has_text_frame]
    assert texts.count(gif_pill) == 1 and texts.count(badge) == 1


def test_deck_builder_rejects_unknown_layout(tmp_path):
    deck_builder = _module("narrative-pptx", "deck_builder")
    with pytest.raises(ValueError, match="bilinmeyen şablon"):
        deck_builder.build_deck({"slides": [{"layout": "bullets"}]}, tmp_path / "x.pptx")


#: Slayta yazılmayan şema alanları; konuşmacı notu ayrı sınanır
_NOT_ON_SLIDE = {"layout", "accent", "role", "image", "speaker_notes"}
#: references/json_schema_guide.md: büyük harfe çevrilen alanlar
_CAPITALIZED = {"chapter", "eyebrow", "tag", "label", "phase", "badge", "scene_title"}


def _spec_text(node, key="", in_rows=False):
    """Şemadaki slayta yazılacak ``(alan, metin)`` yaprakları. ``rows`` içindeki ``label`` tablo etiketidir, çip değil."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k not in _NOT_ON_SLIDE:
                yield from _spec_text(v, k, in_rows or k == "rows")
    elif isinstance(node, list):
        for v in node:
            yield from _spec_text(v, key, in_rows)
    elif isinstance(node, str) and node:
        yield ("" if in_rows else key), node


def _slide_texts(slide):
    texts = []
    for shape in slide.shapes:
        if shape.has_text_frame:
            texts.append(shape.text_frame.text)
        if shape.has_table:
            texts += [cell.text_frame.text for row in shape.table.rows for cell in row.cells]
    return texts


def _png(path, size=(80, 40)):
    Image.new("RGB", size, (37, 99, 235)).save(path)
    return path


def test_every_text_field_of_the_example_spec_reaches_its_slide(tmp_path):
    """Yedi düzenin her alanı (tablo hücreleri, ikili karşılaştırma satırları, kapanış bandı dahil) slayta yazılmalı;
    biçim işaretleri (``**``, `````` ` ``````) metinden kalkar."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    caps = _module("narrative-pptx", "themes").get_theme(language="tr").caps
    _png(tmp_path / "p.png")
    spec = _spec()
    next(s for s in spec["slides"] if s["layout"] == "mental_model")["image"] = "p.png"
    prs = Presentation(str(deck_builder.build_deck(spec, tmp_path / "d.pptx", base_dir=tmp_path)))
    missing = []
    for number, (slide, slide_spec) in enumerate(zip(prs.slides, spec["slides"]), start=1):
        texts = _slide_texts(slide)
        for key, value in _spec_text(slide_spec):
            plain = re.sub(r"\*\*|`", "", value)
            expected = caps(plain) if key in _CAPITALIZED else plain
            if expected not in texts:
                missing.append((number, key, expected))
    assert missing == []


def test_inline_markup_becomes_formatted_runs(tmp_path):
    """references/json_schema_guide.md "Satır içi biçim"."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    theme = _module("narrative-pptx", "themes").get_theme()
    spec = {"slides": [{"layout": "evidence_bento", "mechanism": {"body": "**kalın** ve `kod` ve *eğik*"}}]}
    prs = Presentation(str(deck_builder.build_deck(spec, tmp_path / "d.pptx")))
    box = next(sh for sh in prs.slides[0].shapes if sh.has_text_frame and sh.text_frame.text == "kalın ve kod ve eğik")
    runs = {run.text: run for paragraph in box.text_frame.paragraphs for run in paragraph.runs}
    assert runs["kalın"].font.bold is True and runs[" ve "].font.bold is False
    assert runs["kod"].font.name == theme.font_mono
    assert runs["eğik"].font.italic is True


def test_images_keep_their_aspect_ratio(tmp_path):
    """references/json_schema_guide.md: en-boy oranı korunur. 400 × 100 piksel görsel 4:1 kalmalı."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    _png(tmp_path / "p.png", (400, 100))
    spec = {"slides": [{"layout": "evidence_bento", "image": "p.png"}]}
    prs = Presentation(str(deck_builder.build_deck(spec, tmp_path / "d.pptx", base_dir=tmp_path)))
    picture = next(sh for sh in prs.slides[0].shapes if sh.shape_type == MSO_SHAPE_TYPE.PICTURE)
    assert picture.width / picture.height == pytest.approx(4.0, rel=1e-3)


@pytest.mark.parametrize("summary", ["", "özet"])
def test_mental_model_image_is_drawn_with_and_without_summary(tmp_path, summary):
    """references/json_schema_guide.md: ``scene_summary`` yoksa görsel tam genişlik olur, ama yine çizilir."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    png = _png(tmp_path / "p.png")
    spec = {"slides": [{"layout": "mental_model", "image": "p.png", "scene_summary": summary}]}
    prs = Presentation(str(deck_builder.build_deck(spec, tmp_path / "d.pptx", base_dir=tmp_path)))
    assert _picture_blobs(prs.slides[0]) == [png.read_bytes()]


@pytest.mark.parametrize("layout, key, limit, text_key", [
    ("hero_cover", "pillars", 4, "title"),
    ("mental_model", "characters", 3, "title"),
    ("evidence_bento", "metrics", 2, "value"),
    ("process_arc", "stages", 4, "title"),
    ("synthesis_closing", "pillars", 3, "title"),
])
def test_item_limits_match_what_the_layout_draws(tmp_path, layout, key, limit, text_key):
    """SKILL.md "Sık hatalar": kapak en çok 4, ``characters`` 3, ``metrics`` 2, ``stages`` 4, kapanış 3 kart; fazlası düşer
    ve ``spec_warnings`` bildirir. Sınır ile çizim aynı sayıda olmalı."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    items = [{text_key: f"öğe{k}"} for k in range(limit + 1)]
    crowded = {"slides": [{"layout": layout, key: items}]}
    texts = _slide_texts(Presentation(str(deck_builder.build_deck(crowded, tmp_path / "d.pptx"))).slides[0])
    assert [f"öğe{k}" in texts for k in range(limit + 1)] == [True] * limit + [False]
    assert len(deck_builder.spec_warnings(crowded)) == 1
    assert deck_builder.spec_warnings({"slides": [{"layout": layout, key: items[:limit]}]}) == []


def test_locked_target_is_written_under_a_versioned_name(tmp_path, monkeypatch):
    """SKILL.md: hedef dosya PowerPoint'te açıksa ``_v2`` adıyla yazılır, dönen yol o olur. Kilit Windows'a özgü bir işletim
    sistemi durumu olduğu için kilidi veren tek yer sahtedir; kaydın kendisi gerçektir."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    target = tmp_path / "deck.pptx"
    real_save = PresentationObject.save

    def save(self, path):
        if Path(path) == target:
            raise PermissionError("PowerPoint'te açık")
        real_save(self, path)

    monkeypatch.setattr(PresentationObject, "save", save)
    out = deck_builder.build_deck({"slides": [{"layout": "hero_cover"}]}, target)
    assert out == tmp_path / "deck_v2.pptx"
    assert out.is_file() and not target.exists()


def test_every_locked_version_is_skipped(tmp_path, monkeypatch):
    """SKILL.md: ``_v2``, ``_v3``... adıyla yazılır. ``_v2`` de açıksa bir sonraki sürüme geçilmeli."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    locked = {tmp_path / "deck.pptx", tmp_path / "deck_v2.pptx"}
    real_save = PresentationObject.save

    def save(self, path):
        if Path(path) in locked:
            raise PermissionError("PowerPoint'te açık")
        real_save(self, path)

    monkeypatch.setattr(PresentationObject, "save", save)
    out = deck_builder.build_deck({"slides": [{"layout": "hero_cover"}]}, tmp_path / "deck.pptx")
    assert out == tmp_path / "deck_v3.pptx"
    assert out.is_file() and not (tmp_path / "deck_v2.pptx").exists()   # SKILL.md: sonraki adım dönen yolu denetler


def _hand_deck(path, *boxes):
    """Elle yerleştirilmiş tek slaytlık sunum: ``(x, y, en, boy, metin)`` inç. Not var: "boş not" uyarısı testi karıştırmasın."""
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.notes_slide.notes_text_frame.text = "not"
    for x, y, w, h, text in boxes:
        slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h)).text_frame.text = text
    prs.save(str(path))
    return path


def test_deck_audit_flags_shapes_off_the_canvas(tmp_path):
    """validate_deck belgesi: tuval taşması hatadır. python-pptx'in varsayılan tuvali 10 inç: 8 + 3 = 11 inç taşar."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    clean = validate_deck.audit_pptx(_hand_deck(tmp_path / "ok.pptx", (1, 1, 3, 1, "a")))
    assert clean["passed"] and clean["errors"] == [] and clean["warnings"] == []
    bad = validate_deck.audit_pptx(_hand_deck(tmp_path / "bad.pptx", (8, 1, 3, 1, "a")))
    assert not bad["passed"]
    assert len(bad["errors"]) == 1 and "tuval dışına" in bad["errors"][0]


def test_deck_audit_flags_overlapping_text_above_the_threshold(tmp_path):
    """validate_deck: 0,08 inç²'den büyük kesişim hata. 0,3 × 0,3 = 0,09 çakışır, 0,2 × 0,3 = 0,06 çakışmaz."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    bad = validate_deck.audit_pptx(_hand_deck(tmp_path / "b.pptx", (1, 1, 1, 1, "a"), (1.7, 1.7, 1, 1, "b")))
    assert len(bad["errors"]) == 1 and "çakışıyor" in bad["errors"][0]
    ok = validate_deck.audit_pptx(_hand_deck(tmp_path / "o.pptx", (1, 1, 1, 1, "a"), (1.8, 1.7, 1, 1, "b")))
    assert ok["errors"] == []


def test_deck_audit_warns_about_bullet_walls(tmp_path):
    """SKILL.md kural 3, "madde duvarı yok": dört madde uyarı, üç madde değil."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    bullets = [f"• madde {k}" for k in range(4)]
    wall = validate_deck.audit_pptx(_hand_deck(tmp_path / "w.pptx", (1, 1, 8, 3, "\n".join(bullets))))
    assert sum("Madde işareti duvarı" in w for w in wall["warnings"]) == 1
    short = validate_deck.audit_pptx(_hand_deck(tmp_path / "s.pptx", (1, 1, 8, 3, "\n".join(bullets[:3]))))
    assert short["warnings"] == []


def test_render_falls_back_to_wireframe_when_powerpoint_fails(tmp_path, monkeypatch):
    """SKILL.md: PowerPoint yoksa ya da yanıt vermezse kaba önizleme çıkar. Süreç gerçek bir sınırdır: PowerShell çağrısı
    başarısız döner, testler PowerPoint açmaz. Toplu ızgara 2 sütun: 3 slayt 2 satır, kare 920 × 518, boşluk 24."""
    deck_builder = _module("narrative-pptx", "deck_builder")
    validate_deck = _module("narrative-pptx", "validate_deck")
    monkeypatch.setattr(validate_deck.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    _png(tmp_path / "p.png")
    spec = {"slides": [{"layout": "evidence_bento", "image": "p.png"}, {"layout": "hero_cover"},
                       {"layout": "process_arc", "stages": [{"title": "aşama"}]}]}
    deck = deck_builder.build_deck(spec, tmp_path / "d.pptx", base_dir=tmp_path)
    out = tmp_path / "onizleme"
    out.mkdir()
    (out / "slide_99.png").write_bytes(b"eski")
    slides, sheet, engine = validate_deck.render_slides(deck, out)
    assert engine == "pillow_wireframe"
    assert [p.name for p in slides] == ["slide_01.png", "slide_02.png", "slide_03.png"]
    assert not (out / "slide_99.png").exists()
    with Image.open(sheet) as im:
        assert im.size == (2 * 920 + 3 * 24, 2 * 518 + 3 * 24)


def test_powerpoint_export_runs_the_system_powershell(tmp_path, monkeypatch):
    """Windows CreateProcess yalın adı sistem klasöründen önce çalışma klasöründe arar (Microsoft Learn, "CreateProcessW",
    lpCommandLine): yalın ``powershell`` oradaki bir ``powershell.exe``'yi çalıştırırdı (Python 3.14 ile ölçüldü, 2026-11-07;
    ``NoDefaultCurrentDirectoryInExePath`` tanımlıyken görünmez, Claude Code kabuğu onu tanımlar). Sunum ve çıktı yolları
    betik metnine değil ortam değişkenine yazılır."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    seen = {}

    def run(cmd, env, **kwargs):
        seen.update(cmd=cmd, env=env, timeout=kwargs["timeout"])
        (Path(env["NARRATIVE_OUT_DIR"]) / "slide_01.png").write_bytes(b"x")
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(validate_deck, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(validate_deck.subprocess, "run", run)
    monkeypatch.setenv("SystemRoot", r"D:\Windir")
    out = tmp_path / "o"
    out.mkdir()
    deck = tmp_path / "d.pptx"
    assert [p.name for p in validate_deck._export_via_powerpoint_com(deck, out)] == ["slide_01.png"]
    assert seen["cmd"][0] == r"D:\Windir\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert seen["env"]["NARRATIVE_PPTX_PATH"] == str(deck.resolve())
    assert seen["timeout"] == 90   # SKILL.md: PowerPoint 90 sn'de yanıt vermezse kaba önizleme


@pytest.mark.parametrize("system_root", [None, ""])
def test_powerpoint_export_stays_absolute_without_system_root(tmp_path, monkeypatch, system_root):
    """``SystemRoot`` yoksa ya da boşsa yol yine mutlak olmalı: boş taban göreli yola, yani düzeltilen duruma döner. Yollar
    betik metnine yazılmaz (tırnak içine giren klasör adı PowerShell betiğini bozardı). Başarısız koşumun klasörde bıraktığı
    yarım çıktı kabul edilmez: SKILL.md'ye göre PowerPoint çalışmadıysa kaba önizleme çıkar."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    seen = {}

    def run(cmd, **kwargs):
        seen.update(cmd=cmd)
        return SimpleNamespace(returncode=1)

    monkeypatch.setattr(validate_deck, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(validate_deck.subprocess, "run", run)
    if system_root is None:
        monkeypatch.delenv("SystemRoot", raising=False)
    else:
        monkeypatch.setenv("SystemRoot", system_root)
    deck = tmp_path / "sunum klasoru" / "d.pptx"
    (tmp_path / "slide_01.png").write_bytes(b"yarim")
    assert validate_deck._export_via_powerpoint_com(deck, tmp_path) == []
    assert seen["cmd"][0] == r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    assert seen["cmd"][-1].count(deck.parent.name) == 0 and seen["cmd"][-1].count(tmp_path.name) == 0


@pytest.mark.parametrize("failure", [subprocess.TimeoutExpired("powershell", 90), OSError("başlatılamadı")])
def test_powerpoint_export_gives_up_quietly_when_the_process_fails(tmp_path, monkeypatch, failure):
    """SKILL.md: PowerPoint yoksa ya da 90 sn'de yanıt vermezse kaba önizleme çıkar: süreç hatası yukarı çıkmamalı."""
    validate_deck = _module("narrative-pptx", "validate_deck")

    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(validate_deck, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(validate_deck.subprocess, "run", run)
    (tmp_path / "slide_01.png").write_bytes(b"yarim")   # zaman aşımından önce yazılmış yarım çıktı kabul edilmez
    assert validate_deck._export_via_powerpoint_com(tmp_path / "d.pptx", tmp_path) == []


def test_render_dir_option_writes_previews(tmp_path, monkeypatch, capsys):
    """SKILL.md'nin zorunlu denetim komutu ``--render-dir`` ister. PowerShell başarısız döner: PowerPoint açılmaz, kaba
    önizleme yazılır. Komut satırı aynı süreçte çağrılır, ayrı süreçte sahte PowerShell kurulamaz."""
    validate_deck = _module("narrative-pptx", "validate_deck")
    monkeypatch.setattr(validate_deck.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    deck = _hand_deck(tmp_path / "d.pptx", (1, 1, 3, 1, "a"))
    out = tmp_path / "onizleme"
    assert validate_deck.main([str(deck), "--render-dir", str(out)]) == 0
    assert (out / "slide_01.png").is_file() and (out / "contact_sheet.png").is_file()
    assert capsys.readouterr().out.splitlines()[-1] == "DURUM: PASSED"


# ------------------------------------------------------------------- SKILL.md'deki komutlar, ayrı süreçte
def _run(*args):
    return subprocess.run([sys.executable, *map(str, args)], capture_output=True, text=True, encoding="utf-8",
                          timeout=110, check=False, cwd=ROOT)


def test_documented_deck_commands_work_as_scripts(tmp_path):
    """SKILL.md: komut satırında görsel yolları JSON dosyasının klasörüne göre çözülür (çalışma klasörü depo kökü)."""
    gif = _module("explainer-gif", "gif_encoder").encode_master_palette_gif(_frames(3), [1400, 60, 1500],
                                                                             tmp_path / "anim.gif")
    spec = _spec()
    next(s for s in spec["slides"] if s["layout"] == "evidence_bento")["image"] = "anim.gif"
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "deck.pptx"
    built = _run(SKILLS / "narrative-pptx" / "scripts" / "deck_builder.py", spec_path, "-o", out)
    assert built.returncode == 0, built.stderr
    layouts = [s["layout"] for s in spec["slides"]]
    assert _picture_blobs(Presentation(str(out)).slides[layouts.index("evidence_bento")]) == [gif.read_bytes()]
    checked = _run(SKILLS / "narrative-pptx" / "scripts" / "validate_deck.py", out)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.splitlines()[-1] == "DURUM: PASSED"


def test_documented_gif_command_works_as_a_script(tmp_path):
    gif_encoder = _module("explainer-gif", "gif_encoder")
    gif = gif_encoder.encode_master_palette_gif(_frames(4), [1400, 60, 60, 1500], tmp_path / "a.gif",
                                                state_labels=["", "metin", "metin", "metin"])
    board = tmp_path / "sb.png"
    checked = _run(SKILLS / "explainer-gif" / "scripts" / "validate_gif.py", gif, "--storyboard", board)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    assert checked.stdout.splitlines()[-1] == "DURUM: PASSED"
    assert board.is_file()

    flash = gif_encoder.encode_master_palette_gif(_frames(4), [1400, 60, 60, 1500], tmp_path / "f.gif",
                                                  state_labels=["", "kısa", "uzun", "uzun"])
    failed = _run(SKILLS / "explainer-gif" / "scripts" / "validate_gif.py", flash)
    assert failed.returncode == 1
    assert failed.stdout.splitlines()[-1] == "DURUM: FAILED"


def test_storyboard_has_the_documented_size_and_failed_gifs_get_none(tmp_path):
    """archetypes.md: film şeridi 3 × 2; SKILL.md: yalnızca geçen GIF için yazılır. Kare genişliği 560, oran 32:24 -> 420,
    boşluk 18, başlık 28: 3 × 560 + 4 × 18 = 1752 ve 2 × (420 + 28) + 3 × 18 = 950 piksel."""
    gif_encoder = _module("explainer-gif", "gif_encoder")
    gif = gif_encoder.encode_master_palette_gif(_frames(10), [1400, *[60] * 8, 1500], tmp_path / "a.gif")
    board = tmp_path / "sb.png"
    assert _run(SKILLS / "explainer-gif" / "scripts" / "validate_gif.py", gif, "--storyboard", board).returncode == 0
    with Image.open(board) as im:
        assert im.size == (1752, 950)

    flash = gif_encoder.encode_master_palette_gif(_frames(4), [1400, 60, 60, 1500], tmp_path / "f.gif",
                                                  state_labels=["", "kısa", "uzun", "uzun"])
    none = tmp_path / "yok.png"
    assert _run(SKILLS / "explainer-gif" / "scripts" / "validate_gif.py", flash, "--storyboard", none).returncode == 1
    assert not none.exists()


def test_deck_commands_report_placeholders_and_bad_decks(tmp_path):
    """SKILL.md: komut satırı kalan yer tutucuları ``[UYARI]`` ile bildirir (örnek şemada 3-6. slaytlar, elle sayıldı);
    denetim başarısız olunca çıkış kodu 1 ve ``DURUM: FAILED``."""
    built = _run(SKILLS / "narrative-pptx" / "scripts" / "deck_builder.py", EXAMPLE_SPEC, "-o", tmp_path / "d.pptx")
    assert built.returncode == 0, built.stderr
    assert built.stdout.count("[UYARI]") == 4
    bad = _hand_deck(tmp_path / "bad.pptx", (8, 1, 3, 1, "a"))
    checked = _run(SKILLS / "narrative-pptx" / "scripts" / "validate_deck.py", bad)
    assert checked.returncode == 1
    assert checked.stdout.splitlines()[-1] == "DURUM: FAILED"


def test_documented_plain_imports_work_for_every_script(tmp_path):
    """archetypes.md ve SKILL.md: klasör ``sys.path``'e eklenir, modüller düz adla içe aktarılır. Bu dosyanın takma adlı
    paketi göreli içe aktarma dalını kullanır; ``else`` dalı ve koyu temanın geç içe aktarmaları ancak burada çalışır."""
    code = """
import sys
sys.path[:0] = sys.argv[1:3]
import numpy as np
import webgl_capture
from chart_artist import render_annotated_panels
from deck_builder import build_deck
from gif_archetypes import render_phosphor_scope_gif
from validate_deck import audit_pptx
from validate_gif import audit_gif
t = np.linspace(0.0, 1.0, 50)
event = {"t": 0.5, "xy": (0.5, 0.5), "text": "olay"}
gif = render_phosphor_scope_gif(sys.argv[3], t, [{"traces": [{"y": t}]}], events=[event], n_sweep_frames=3,
                                figsize=(2.0, 1.5), dpi=40)
print(audit_gif(gif)["frame_count"])
"""
    done = _run("-c", code, SKILLS / "explainer-gif" / "scripts", SKILLS / "narrative-pptx" / "scripts", tmp_path / "p.gif")
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["4"]


# ------------------------------------------------------------------- explainer-gif: HDR katmanları, balon, WebGL
def test_hdr_hud_stays_sharp_and_emitted_light_blooms():
    """hdr_compositor belgesi: yazı katmanı ışımadan sonra bindirilir (bulanıklaşmaz); ışık komşu piksellere yayılır."""
    hdr = _module("explainer-gif", "hdr_compositor")
    h, w = 32, 32
    black = np.zeros((h, w, 3), dtype=np.uint8)
    no_dither = np.zeros((h, w, 1), dtype=np.float32)
    hud = np.zeros((h, w, 4), dtype=np.uint8)
    hud[16, 16] = (200, 100, 50, 255)
    sharp = hdr.compose_hdr_frame(black, black, hud, bayer_cache=no_dither).astype(int)
    assert np.all(np.abs(sharp[16, 16] - (200, 100, 50)) <= 1)
    assert sharp[16, 17].max() == 0 and sharp[15, 16].max() == 0

    emit = black.copy()
    emit[16:18, 16:18] = 255                 # çift koordinatta 2x2: yarım çözünürlükte de görünür
    glow = hdr.compose_hdr_frame(black, emit, np.zeros((h, w, 4), dtype=np.uint8), bayer_cache=no_dither)
    assert glow[16, 21].max() > 0            # 4 piksel ötede ışık var; emit = 0 olan sahnede 0 olurdu


def _decode(path):
    with Image.open(path) as im:
        return [np.asarray(frame.convert("RGB"), dtype=float) for frame in ImageSequence.Iterator(im)]


def test_scope_sweep_callout_appears_at_its_moment_and_stays(tmp_path):
    """SKILL.md kural 4: balon tarama olay anına gelince açılır ve döngü sonuna kadar kalır.
    Karşılaştırma aynı anı yalnız vurgulayan GIF'le: örnek anları ve süreler aynı, fark yalnız balon."""
    archetypes = _module("explainer-gif", "gif_archetypes")
    panels = [{"traces": [{"y": OUT}]}]
    common = {"n_sweep_frames": 8, **SMALL}
    event = {"t": 2.0, "panel": 0, "xy": (2.0, float(1.0 - np.exp(-2.0))), "offset": (10, -20), "text": "olay"}
    with_callout = _decode(archetypes.render_scope_sweep_gif(tmp_path / "a.gif", T, panels, events=[event], **common))
    without = _decode(archetypes.render_scope_sweep_gif(tmp_path / "b.gif", T, panels, highlight_times=[2.0], **common))
    assert len(with_callout) == len(without) == 8 + 1
    shown = [float(np.mean(np.abs(a - b))) > 0.5 for a, b in zip(with_callout, without)]
    first = shown.index(True, 1)
    assert shown[0]                          # poster: bütün balonlar görünür
    assert first >= 2                        # olaydan önceki tarama karelerinde balon yok
    assert all(shown[first:])                # olaydan sonra hiç kaybolmaz


def test_webgl_capture_puts_poster_first_and_shifts_highlights(tmp_path, monkeypatch):
    """Kare 0 poster_index anıdır; sample_times_ms dizini k, GIF'te k + 1. karedir (vurgu da oraya düşer).
    Tarayıcı gerçek bir sınırdır: Playwright sahte; ekran görüntüsü o anki sanal zamanı kırmızı kanala yazar."""
    class Page:
        t = 0.0

        def add_init_script(self, script):
            pass

        def route(self, url, handler):
            pass

        def goto(self, url, wait_until):
            pass

        def locator(self, selector):
            return self

        def evaluate(self, script, t):
            self.t = t

        def screenshot(self, type):
            buf = io.BytesIO()
            Image.new("RGB", (8, 6), (int(self.t), 0, 0)).save(buf, "PNG")
            return buf.getvalue()

    browser = SimpleNamespace(new_page=lambda **kw: Page(), close=lambda: None)

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kw: browser))

    fake = ModuleType("playwright.sync_api")
    fake.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    webgl_capture = _module("explainer-gif", "webgl_capture")
    path = webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0, 60.0, 120.0, 180.0], tmp_path / "w.gif",
                                                   poster_index=3, highlight_indices=[1], width=8, height=6)
    frames = _decode(path)
    assert [round(f[..., 0].mean() / 60) for f in frames] == [3, 0, 1, 2, 3]   # 180, 0, 60, 120, 180 (± titreşim)
    with Image.open(path) as im:
        durations = [frame.info["duration"] for frame in ImageSequence.Iterator(im)]
    assert durations[2] >= 1200 and max(durations[1], durations[3]) < 1200


@pytest.fixture
def browser_log(monkeypatch):
    """Tarayıcı gerçek bir sınırdır: Playwright sahte. Çağrı sırası, sayfa seçenekleri, sahne yanıtı ve kapatma kaydedilir;
    ekran görüntüsü o anki sanal zamanı kırmızı kanala yazar."""
    log = SimpleNamespace(calls=[], page_options=None, launches=[], broken_channels=set(), closes=0, fulfilled=[],
                          evaluated=[], screenshot_error=None)

    class Route:
        def fulfill(self, body, content_type):
            log.fulfilled.append((body, content_type))

    class Page:
        t = 0.0

        def add_init_script(self, script):
            log.calls.append("add_init_script")

        def route(self, url, handler):
            log.calls.append("route")
            self.scene = url, handler

        def goto(self, url, wait_until):
            log.calls.append("goto")
            assert url == self.scene[0]
            self.scene[1](Route())

        def locator(self, selector):
            return self

        def evaluate(self, script, t):
            log.calls.append("evaluate")
            log.evaluated.append(script)
            self.t = t

        def screenshot(self, type):
            if log.screenshot_error is not None:
                raise log.screenshot_error
            buf = io.BytesIO()
            Image.new("RGB", (8, 6), (int(self.t), 0, 0)).save(buf, "PNG")
            return buf.getvalue()

    def new_page(**options):
        log.page_options = options
        return Page()

    def close():
        log.closes += 1

    def launch(**options):
        channel = options.get("channel")
        log.launches.append(channel)
        if channel in log.broken_channels:
            raise RuntimeError("tarayıcı yok")
        return SimpleNamespace(new_page=new_page, close=close)

    @contextmanager
    def sync_playwright():
        yield SimpleNamespace(chromium=SimpleNamespace(launch=launch))

    fake = ModuleType("playwright.sync_api")
    fake.sync_playwright = sync_playwright
    monkeypatch.setitem(sys.modules, "playwright", ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
    return log


def test_webgl_capture_prepares_the_page_before_loading_it(tmp_path, browser_log):
    """webgl_capture: ``add_init_script`` yalnızca gezinmede çalışır, sahne sahte adresten yüklenir. Sıra bozulursa sayfa
    betiği gerçek saati görür. Çağrılan ad sanal saat betiğinde tanımlı olmalı; yoksa gerçek tarayıcıda her kare hata verir."""
    webgl_capture = _module("explainer-gif", "webgl_capture")
    webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0, 60.0], tmp_path / "w.gif", width=8, height=6)
    assert browser_log.calls[:3] == ["add_init_script", "route", "goto"]
    assert set(browser_log.calls[3:]) == {"evaluate"}
    assert browser_log.fulfilled == [("<canvas></canvas>", "text/html")]
    name = re.fullmatch(r"\(t\) => window\.(\w+)\(t\)", browser_log.evaluated[0]).group(1)
    assert webgl_capture.VIRTUAL_CLOCK_INIT_JS.count(f"window.{name} = ") == 1
    assert browser_log.closes == 1


def test_webgl_scene_runs_without_network(tmp_path, browser_log):
    """archetypes.md: sayfa kendi kendine yeter, ağ kapalıdır. ``page.route`` WebSocket'i kesmez (bu dosyanın başı, ölçüm)."""
    webgl_capture = _module("explainer-gif", "webgl_capture")
    webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0, 60.0], tmp_path / "w.gif", width=8, height=6)
    assert browser_log.page_options["offline"] is True


def test_webgl_capture_uses_poster_index_and_sample_indices(tmp_path, browser_log):
    """archetypes.md: kare 0, ``sample_times_ms[poster_index]`` anının posteridir; ``state_label_fn(i, t)``'nin ``i``'si
    ``sample_times_ms`` dizinidir. Uçta olmayan bir poster dizini: sonuncuyu ya da ilkini seçen kod burada geçmez."""
    webgl_capture = _module("explainer-gif", "webgl_capture")
    seen = []

    def label(i, t):
        seen.append((i, t))
        return "metin"

    path = webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0, 60.0, 120.0, 180.0], tmp_path / "w.gif",
                                                   poster_index=2, state_label_fn=label, width=8, height=6)
    assert [round(f[..., 0].mean() / 60) for f in _decode(path)] == [2, 0, 1, 2, 3]
    assert seen == [(2, 120.0), (0, 0.0), (1, 60.0), (2, 120.0), (3, 180.0)]


def test_webgl_capture_falls_back_to_installed_browsers(tmp_path, browser_log):
    """webgl_capture belgesi: Chromium indirilmemişse kurulu Edge ya da Chrome kanalı denenir; hiçbiri yoksa açık hata."""
    webgl_capture = _module("explainer-gif", "webgl_capture")
    browser_log.broken_channels = {None}
    webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0, 60.0], tmp_path / "w.gif", width=8, height=6)
    assert browser_log.launches == [None, "msedge"]

    browser_log.launches.clear()
    browser_log.broken_channels = {None, "msedge", "chrome"}
    with pytest.raises(RuntimeError, match="playwright install chromium"):
        webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0], tmp_path / "y.gif", width=8, height=6)
    assert browser_log.launches == [None, "msedge", "chrome"]


def test_webgl_capture_closes_the_browser_when_a_frame_fails(tmp_path, browser_log):
    """Ekran görüntüsü başarısız olsa da başsız tarayıcı açık kalmamalı: açık kalan süreç bir sonraki koşuma yük olur."""
    webgl_capture = _module("explainer-gif", "webgl_capture")
    browser_log.screenshot_error = RuntimeError("ekran görüntüsü alınamadı")
    with pytest.raises(RuntimeError, match="ekran görüntüsü"):
        webgl_capture.capture_html_scene_to_gif("<canvas></canvas>", [0.0], tmp_path / "w.gif", width=8, height=6)
    assert browser_log.closes == 1


# ------------------------------------------------------------------- narrative-pptx: açıklamalı grafik
def test_chart_draws_every_panel_with_labelled_references(tmp_path):
    """references/json_schema_guide.md: panel başına ``hlines`` ve ``vspans`` etiketli olabilir. Boyut figsize × dpi;
    etiketler çizilmeseydi iki görsel aynı olurdu."""
    chart_artist = _module("narrative-pptx", "chart_artist")

    def panels(labelled):
        hline = {"y": 0.5, **({"label": "referans"} if labelled else {})}
        vspan = {"x0": 2.0, "x1": 3.0, **({"label": "aşama"} if labelled else {})}
        return [{"title": "üst", "traces": [{"x": T, "y": OUT, "label": "out"}], "hlines": [hline], "vspans": [vspan]},
                {"title": "alt", "xlabel": "t", "traces": [{"x": T, "y": STEP}]}]

    with_labels = chart_artist.render_annotated_panels(tmp_path / "a.png", panels(True), figsize=(4.0, 3.0), dpi=50)
    plain = chart_artist.render_annotated_panels(tmp_path / "b.png", panels(False), figsize=(4.0, 3.0), dpi=50)
    with Image.open(with_labels) as a, Image.open(plain) as b:
        assert a.size == (200, 150)
        assert float(np.mean(np.abs(np.asarray(a.convert("RGB"), dtype=float) - np.asarray(b.convert("RGB"), dtype=float)))) > 0.05


def test_chart_draws_the_second_panel(tmp_path):
    """references/json_schema_guide.md: her panel kendi izini çizer. Yalnız ikinci panelin izi değişince üst yarı aynı, alt
    yarı farklı olmalı (iki iz de 0-1 aralığında, eksenler kaymaz)."""
    chart_artist = _module("narrative-pptx", "chart_artist")

    def pixels(name, lower):
        path = chart_artist.render_annotated_panels(tmp_path / name, [{"traces": [{"x": T, "y": OUT}]},
                                                                      {"traces": [{"x": T, "y": lower}]}],
                                                    figsize=(4.0, 3.0), dpi=50)
        with Image.open(path) as im:
            return np.asarray(im.convert("RGB"), dtype=float)

    first, second = pixels("a.png", STEP), pixels("b.png", 1.0 - STEP)
    middle = first.shape[0] // 2
    assert float(np.mean(np.abs(first[:middle] - second[:middle]))) == 0.0
    assert float(np.mean(np.abs(first[middle:] - second[middle:]))) > 0.5
