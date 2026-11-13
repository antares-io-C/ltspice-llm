"""
20 - KiCad Kart Okuyucusunu KiCad'in Kendi Hesabıyla Karşılaştırma
===================================================================
``ltspice_llm.board`` bir ``.kicad_pcb`` dosyasını KiCad olmadan okur. Bu betik okuyucunun her kartta KiCad'le aynı
şeyi gördüğünü ölçer: KiCad'in kendi Python'u (``pcbnew``) kartı yükler ve hesabını (tanık) JSON'a yazar
(``examples/kicad_oracle.py``), okuyucu aynı kartı okur, ikisi karşılaştırılır:

- net başına toplam iz uzunluğu (KiCad'in iz uzunluğu; ark dahil),
- pad konumu, açısı, boyutu ve bakır katmanları,
- izle / via'yla / padle (döküm hariç) birbirine bağlı pad adaları,
- düz izlerin altındaki dolu dökümün örtme oranı ve döküm içindeki örnek noktalar.

Uyuşmazlık varsa çıkış kodu 1. KiCad yoksa 2.

Güvenlik: kart **her zaman geçici bir kopyada** açılır. ``pcbnew.LoadBoard`` kartın yanına ``.kicad_pro`` /
``.kicad_prl`` yazar; kullanıcının proje klasöründe hiçbir şey çalışmaz ve yazılmaz. Çıktıda yalnız dosya adı ve
SHA-256'nın başı görünür, klasör yolu görünmez.

Çalıştırma (KiCad 10 kurulu olmalı; başka yerdeyse ``KICAD_PYTHON=<KiCad>/bin/python.exe``):
    python examples/20_kicad_board_checks.py                      # tests/data/kicad10_*.kicad_pcb
    python examples/20_kicad_board_checks.py kart.kicad_pcb ...   # verilen kartlar
    python examples/20_kicad_board_checks.py --demolar            # KiCad'in demo kartları (64 MB altı)
    python examples/20_kicad_board_checks.py --kaydet             # sentetik kartları kurar, tests/data'ya yazar
"""

import glob
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from ltspice_llm.board import read_kicad_board  # noqa: E402

ORACLE = Path(__file__).resolve().parent / "kicad_oracle.py"
DATA = ROOT / "tests" / "data"
#: Sentetik test kartları: (kurucu adı, tests/data'daki dosya adının kökü)
SYNTHETIC = (("paths", "kicad10_paths"), ("4layer", "kicad10_4layer"))
#: KiCad tanığı mm'yi 6 ondalıkla yazar (1 nm); uzunluk ve konum farkı bunun üstündeyse uyuşmazlıktır
LENGTH_TOL_MM = 2e-6
#: Arkta KiCad'in iz uzunluğu, dosyadaki üç noktadan geçen yayın uzunluğundan biraz farklı: en büyük fark r = 0.24 mm'lik
#: arkta 4.9e-5 mm (bağıl 1.3e-4; One-Air-Max demosu, 2026-11-13). Merkezi nm'ye yuvarlamak farkı açıklamadı; nedeni
#: ölçülmedi. Net başına tolerans: LENGTH_TOL_MM + ARC_REL_TOL x netteki ark uzunluğu.
ARC_REL_TOL = 2e-4
POSITION_TOL_MM = 2e-6
#: Örtme oranı karşılaştırılan en çok düz iz sayısı (büyük kartta süre sınırı; eşit aralıkla seçilir)
MAX_COVERAGE_TRACKS = 1500
KICAD_TIMEOUT_S = 300
#: --demolar bu boyuttan büyük demoları atlar: KiCad'in örtme örneklemesi büyük kartta dakikalar sürüyor (2026-11-12:
#: video.kicad_pcb 5.9 MB, 7 dakikada bitmedi). Okuyucunun kendi sınırı DEFAULT_MAX_BYTES (64 MB).
DEMO_MAX_BYTES = 3 * 1024 * 1024


# --------------------------------------------------------------------------------------------------- KiCad
def find_kicad_python():
    """KiCad'in Python'u: ``KICAD_PYTHON`` ortam değişkeni, yoksa ``C:\\Program Files\\KiCad\\<sürüm>\\bin\\python.exe``."""
    explicit = os.environ.get("KICAD_PYTHON")
    if explicit:
        return Path(explicit) if Path(explicit).is_file() else None
    found = sorted(glob.glob(os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "KiCad", "*", "bin",
                                          "python.exe")), key=lambda p: [int(x) if x.isdigit() else 0 for x in
                                                                         Path(p).parent.parent.name.split(".")])
    return Path(found[-1]) if found else None


def kicad_run(python, args, cwd):
    """
    ``kicad_oracle.py``'yi KiCad'in Python'uyla, izole kipte (``-I``) ve zaman sınırıyla koşar. Zaman aşımı hatadır
    (süreç öldürülür): KiCad bir pencerede cevap bekliyor olabilir (kicad_oracle.py "ölçülen tuzaklar").
    """
    try:
        proc = subprocess.run([str(python), "-I", str(ORACLE), *args], cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=KICAD_TIMEOUT_S, stdin=subprocess.DEVNULL,
                              check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"KiCad betiği {KICAD_TIMEOUT_S} sn'de bitmedi ve durduruldu (asılı kaldı; ekranda "
                           "cevap bekleyen bir KiCad penceresi olabilir).") from exc
    if proc.returncode != 0 or "OK" not in proc.stdout.split():
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
        raise RuntimeError(f"KiCad betiği başarısız (rc {proc.returncode}): " + " | ".join(tail))


def kicad_oracle(python, board_path, workdir):
    """Kartın geçici kopyasında KiCad'in hesabı (tanık). Kullanıcının klasörüne dokunulmaz."""
    copy = Path(workdir) / Path(board_path).name
    shutil.copyfile(board_path, copy)
    out = copy.with_suffix(".oracle.json")
    kicad_run(python, ["dump", copy.name, out.name], cwd=workdir)
    return json.loads(out.read_text(encoding="utf-8"))


def demo_boards(python):
    """KiCad kurulumuyla gelen demo kartları; `DEMO_MAX_BYTES`'tan büyükler atlanır (KiCad'in hesabı uzun sürer)."""
    demos = python.parent.parent / "share" / "kicad" / "demos"
    boards, skipped = [], []
    for path in sorted(demos.rglob("*.kicad_pcb")):
        (boards if path.stat().st_size <= DEMO_MAX_BYTES else skipped).append(path)
    return boards, skipped


# --------------------------------------------------------------------------------------------------- karşılaştırma
def _key(net, layer, start, end):
    return net, layer, round(start[0], 5), round(start[1], 5), round(end[0], 5), round(end[1], 5)


def compare(board, oracle):
    """Okuyucunun ``board``'u ile KiCad tanığı arasındaki farklar (sayılar ve ilk birkaç örnek)."""
    problems = []
    if oracle["sha256"] != board.sha256:
        problems.append("tanık başka bir dosyadan (SHA-256 farklı)")
    if sorted(oracle["copper_layers"]) != sorted(board.copper_layers):   # KiCad katman numarası sırasıyla yazar
        problems.append(f"bakır katmanlar: KiCad {oracle['copper_layers']}, okuyucu {list(board.copper_layers)}")

    # 1) net başına iz uzunluğu
    worst, worst_net, over = 0.0, "", []
    reader_nets = {net for net in board.nets()}
    for net, stat in oracle["nets"].items():
        diff = abs(board.net_track_length_mm(net) - stat["track_length_mm"])
        arcs = sum(t.length_mm for t in board.tracks if t.net == net and t.mid is not None)
        if diff > LENGTH_TOL_MM + ARC_REL_TOL * arcs:
            over.append(f"{net} {diff:.3g} mm")
        tracks = sum(1 for t in board.tracks if t.net == net)
        vias = sum(1 for v in board.vias if v.net == net)
        if (tracks, vias) != (stat["tracks"], stat["vias"]):
            problems.append(f"{net}: iz/via sayısı KiCad {stat['tracks']}/{stat['vias']}, okuyucu {tracks}/{vias}")
        if diff > worst:
            worst, worst_net = diff, net
    if over:
        problems.append(f"uzunluk farkı toleransın üstünde: {', '.join(over[:5])}")
    missing = sorted(set(oracle["nets"]) - reader_nets - {""})
    if missing:
        problems.append(f"okuyucunun görmediği netler: {missing[:5]}")

    # 2) padler
    by_label = {}
    for pad in board.pads:
        by_label.setdefault((pad.ref, pad.number), []).append(pad)
    pad_bad = []
    for entry in oracle["pads"]:
        candidates = by_label.get((entry["ref"], entry["number"]), [])
        match = [p for p in candidates if math.hypot(p.at[0] - entry["at"][0], p.at[1] - entry["at"][1]) <= POSITION_TOL_MM]
        if not match:
            pad_bad.append(f"{entry['ref']}.{entry['number']} konum")
            continue
        pad = match[0]
        if (abs((pad.angle_deg - entry["angle_deg"] + 180.0) % 360.0 - 180.0) > 1e-4
                or set(pad.layers) != set(entry["layers"]) or pad.net != entry["net"]):
            pad_bad.append(f"{pad.label} açı/katman/net")
        elif pad.shape in ("circle", "oval", "rect", "roundrect") and any(
                abs(a - b) > 1e-5 for a, b in zip(sorted(pad.size_mm), sorted(entry["size_mm"]))):
            pad_bad.append(f"{pad.label} boyut")
    if len(board.pads) != len(oracle["pads"]):
        pad_bad.append(f"pad sayısı KiCad {len(oracle['pads'])}, okuyucu {len(board.pads)}")

    # 3) adalar
    island_bad = []
    for net, groups in oracle["islands"].items():
        mine = board.connected_pad_groups(net)
        if mine != groups:
            # okuyucu bakır çizimleri (gr_rect...) bakır saymaz, KiCad sayar: neden yanında yazılır
            island_bad.append(net + (" (bakır çizimli net)" if net in board.drawing_nets else ""))

    # 4) örtme oranı: KiCad'in örnekleme kuralıyla (n = ceil(uzunluk / adım), t = (i + 0.5) / n)
    step = oracle["coverage_step_mm"]
    index = {}
    for track in board.tracks:
        if track.mid is None:
            index.setdefault(_key(track.net, track.layer, track.start, track.end), track)
    straight = [t for t in oracle["tracks"] if t["type"] == "segment" and t.get("plane_under")]
    stride = max(1, math.ceil(len(straight) / MAX_COVERAGE_TRACKS))
    coverage_bad, coverage_checked = [], 0
    for entry in straight[::stride]:
        track = index.get(_key(entry["net"], entry["layer"], entry["start"], entry["end"]))
        if track is None:
            coverage_bad.append(f"{entry['net']} izi bulunamadı")
            continue
        coverage_checked += 1
        count = max(1, math.ceil(entry["length_mm"] / step))
        (x1, y1), (x2, y2) = track.start, track.end
        for layer, expected in entry["plane_under"].items():
            inside = sum(board.copper_fill_at(layer, x1 + (x2 - x1) * (i + 0.5) / count, y1 + (y2 - y1) * (i + 0.5) / count)
                         for i in range(count))
            if abs(inside / count - expected) > 1.5 / count:   # KiCad noktayı nm'ye yuvarlar: sınırda bir nokta
                coverage_bad.append(f"{entry['net']} {entry['layer']}->{layer} {inside / count:.3f}/{expected:.3f}")

    # 5) döküm örnekleri ve dolu olma durumu
    # Aynı net ve katmanda birden çok döküm olabilir: tanığın örnekleri o tek dökümündür, dökümler dosya sırasıyla eşlenir
    zone_bad, samples, used = [], 0, {}
    for entry in oracle["zones"]:
        key = (entry["net"], tuple(sorted(entry["layers"])))
        same = [z for z in board.zones if (z.net, tuple(sorted(z.layers))) == key]
        index = used[key] = used.get(key, -1) + 1
        if index >= len(same):
            zone_bad.append(f"{entry['net']} dökümü yok")
            continue
        zone = same[index]
        if entry["is_filled"] != bool(zone.fills):
            zone_bad.append(f"{entry['net']} dolu/boş")
        for layer, x, y, inside in entry["samples"]:
            samples += 1
            if inside != _inside(zone, layer, x, y):
                zone_bad.append(f"{entry['net']} {layer} ({x:g}, {y:g})")
    return {
        "nets": len(oracle["nets"]), "worst_mm": worst, "worst_net": worst_net,
        "pads": len(oracle["pads"]), "pad_bad": pad_bad,
        "islands": len(oracle["islands"]), "island_bad": island_bad,
        "coverage_tracks": coverage_checked, "coverage_bad": coverage_bad,
        "zone_samples": samples, "zone_bad": zone_bad, "problems": problems,
    }


def _inside(zone, layer, x, y):
    from ltspice_llm.board import _inside_fill
    import numpy as np

    for flat in zone.fills.get(layer, ()):
        xs, ys = np.asarray(flat[0::2], dtype=float), np.asarray(flat[1::2], dtype=float)
        if _inside_fill(x, y, (xs, ys, np.roll(xs, -1), np.roll(ys, -1),
                               (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max())))):
            return True
    return False


def failed(result):
    return bool(result["problems"] or result["pad_bad"] or result["island_bad"] or result["coverage_bad"]
                or result["zone_bad"])


def print_row(name, board, result, seconds):
    print(f"{name[:34]:34} {board.format_version:>8} {result['nets']:>5} {result['worst_mm']:>10.2e} "
          f"{len(result['pad_bad']):>4}/{result['pads']:<5} {len(result['island_bad']):>3}/{result['islands']:<4} "
          f"{len(result['coverage_bad']):>3}/{result['coverage_tracks']:<5} {len(result['zone_bad']):>3}/"
          f"{result['zone_samples']:<5} {seconds:>6.1f}  {'UYUŞMAZ' if failed(result) else 'aynı'}")
    for label, items in (("sorun", result["problems"]), ("pad", result["pad_bad"]), ("ada", result["island_bad"]),
                         ("örtme", result["coverage_bad"]), ("döküm", result["zone_bad"])):
        if items:
            print(f"    {label}: {len(items)} -> {', '.join(items[:6])}{' ...' if len(items) > 6 else ''}")


HEADER = (f"{'kart':34} {'biçim':>8} {'net':>5} {'uzunlukΔmm':>10} {'pad':>10} {'ada':>8} {'örtme':>9} "
          f"{'döküm':>9} {'sn':>6}  sonuç")


def check_boards(python, paths):
    print(HEADER)
    bad = 0
    with tempfile.TemporaryDirectory(prefix="kicad_check_") as workdir:
        for path in paths:
            path = Path(path)
            start = time.perf_counter()
            sub = Path(workdir) / f"{len(os.listdir(workdir)):03d}"
            sub.mkdir()
            try:
                oracle = kicad_oracle(python, path, sub)
                board = read_kicad_board(sub / path.name)
                result = compare(board, oracle)
            except Exception as exc:   # bir kartın hatası ötekileri durdurmasın
                print(f"{path.name[:34]:34} HATA: {type(exc).__name__}: {exc}")
                bad += 1
                continue
            print_row(path.name, board, result, time.perf_counter() - start)
            bad += failed(result)
    return bad


def record(python):
    """Sentetik kartları geçici klasörde kurar, KiCad'e hesaplatır, karşılaştırır; aynıysa tests/data'ya kopyalar."""
    with tempfile.TemporaryDirectory(prefix="kicad_build_") as workdir:
        for kind, stem in SYNTHETIC:
            pcb = Path(workdir) / f"{stem}.kicad_pcb"
            kicad_run(python, ["build", kind, pcb.name], cwd=workdir)
            kicad_run(python, ["dump", pcb.name, f"{stem}.oracle.json"], cwd=workdir)
            oracle = json.loads((Path(workdir) / f"{stem}.oracle.json").read_text(encoding="utf-8"))
            result = compare(read_kicad_board(pcb), oracle)
            if failed(result):
                print_row(pcb.name, read_kicad_board(pcb), result, 0.0)
                raise SystemExit(f"{stem}: okuyucu KiCad'le uyuşmuyor; tests/data'ya yazılmadı.")
            for suffix in (".kicad_pcb", ".oracle.json"):
                shutil.copyfile(Path(workdir) / f"{stem}{suffix}", DATA / f"{stem}{suffix}")
            print(f"yazıldı: tests/data/{stem}.kicad_pcb + .oracle.json (KiCad {oracle['kicad_version']}, "
                  f"sha256 {oracle['sha256'][:12]})")


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    python = find_kicad_python()
    if python is None:
        print("KiCad'in Python'u bulunamadı (KICAD_PYTHON ortam değişkeniyle verin).")
        return 2
    if "--kaydet" in args:
        record(python)
        return 0
    paths = [a for a in args if not a.startswith("--")]
    if "--demolar" in args:
        boards, skipped = demo_boards(python)
        for path in skipped:
            print(f"atlandı: {path.name} ({path.stat().st_size / 2**20:.1f} MB > {DEMO_MAX_BYTES / 2**20:.0f} MB)")
        paths += boards
    if not paths:
        paths = [DATA / f"{stem}.kicad_pcb" for _, stem in SYNTHETIC]
    bad = check_boards(python, paths)
    print(f"\n{len(paths) - bad}/{len(paths)} kart KiCad'le aynı.")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
