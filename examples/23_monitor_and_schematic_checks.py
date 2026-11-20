"""
Örnek 23: canlı monitörün ölçümleri. docs/GUIDE.md ve docs/KILAVUZ.md'deki sayılar bu betikten gelir.

1. Olay yazımının maliyeti: `ltspice_llm.events.emit` açıkken ve kapalıyken olay başına süre (beş tekrarın medyanı).
   Yazılan satır sayısı da denetlenir (her olay bir satır, dosya başına bir ``process`` başlığı).
2. Şema çizimi: LTspice'ın kurulu örnek şemaları (``<LTspice veri klasörü>/examples``) `asc_view.render_asc` ile çizilir.
   İstisna, bozuk SVG (XML) ve bulunamayan sembol sayılır; şema başına süre ölçülür. Biri bile varsa çıkış kodu 1 olur.
3. Kavşak kuralı (`asc_view.junctions`: ortasından geçilen kablo iki sayılır): LTspice'ın kendi netlisteri
   (``-netlist``) bölünmemiş T'yi ve eğik kablonun ortasına inen ucu bağlıyor mu, uçsuz kesişmeyi bağlamıyor mu? Her
   vakada sapanın ucundaki direnç ana kablonun düğümündeyse bağlıdır; görüntüleyicinin noktası da aynı cevabı vermeli.

    python examples/23_monitor_and_schematic_checks.py            # bütün örnek şemalar (~2 dk)
    python examples/23_monitor_and_schematic_checks.py --hizli    # adına göre sıralı ilk 150 şema

Ölçüm (2026-11-19, i7-1255U, Windows 11, LTspice 26.1.1):
- Olay başına açıkken 0,48 ms, kapalıyken 435 ns. Açık maliyet diskin ve virüs taramasının yüküne bağlı: aynı gün
  makine yüklüyken 1,23 ms ölçüldü.
- 4334 şema: 0 istisna, 0 bozuk XML, 0 bulunamayan sembol; medyan 16,8 ms, %99 77 ms, en uzun 192 ms.
- Kavşak (2026-11-20, i7-7500U, LTspice 26.1.1 ``-netlist``): bölünmemiş T, bölünmüş T ve eğik kablonun ortası bağlı,
  uçsuz kesişme bağlı değil; görüntüleyicinin noktaları dört vakada da LTspice'la aynı. 2026-11-19'da aynı makinede
  4334 şema yine 0 istisna, 0 bozuk XML, 0 bulunamayan sembol verdi.
"""

import os
import statistics
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ltspice_llm import LTSpiceAdapter, events  # noqa: E402
from ltspice_llm.asc_view import junctions, render_asc  # noqa: E402
from ltspice_llm.reference import ltspice_data_dir  # noqa: E402
from ltspice_llm.schematic import write_asc_file  # noqa: E402

#: --hizli kipinde çizilen şema sayısı
FAST_SCHEMATICS = 150

#: Kavşak vakaları: (ad, kablolar, ana kablonun V(+) ucu, öbür ucu (Rb), bağlanmalı mı). Sapa ya da kesen kablo
#: (0, 0)'dan geçip (0, 64)'te Ra'ya iner; vakalar x'te 400 birim aralıkla dizilir.
JUNCTION_CASES = (
    ("bölünmemiş T", [(-64, 0, 64, 0), (0, 0, 0, 64)], (-64, 0), (64, 0), True),
    ("bölünmüş T", [(-64, 0, 0, 0), (0, 0, 64, 0), (0, 0, 0, 64)], (-64, 0), (64, 0), True),
    ("eğik kablonun ortası", [(-64, -64, 64, 64), (0, 0, 0, 64)], (-64, -64), (64, 64), True),
    ("uçsuz kesişme", [(-64, 0, 64, 0), (0, -64, 0, 64)], (-64, 0), (64, 0), False),
)


def emit_cost(count):
    """(kapalı sn/olay, açık sn/olay, yazılan satır, beklenen satır)."""
    saved = {name: os.environ.get(name) for name in (events.ENV_EVENTS, events.ENV_EVENT_DIR)}
    try:
        with tempfile.TemporaryDirectory(prefix="olay_olcum_") as folder:
            os.environ[events.ENV_EVENT_DIR] = folder

            def per_event(on):
                os.environ[events.ENV_EVENTS] = "1" if on else "0"
                start = time.perf_counter()
                for _ in range(count):
                    events.emit("ltspice_exit", run="x", outcome="exited", returncode=0, seconds=0.1, crash=None)
                return (time.perf_counter() - start) / count

            off = statistics.median(per_event(False) for _ in range(5))
            on = statistics.median(per_event(True) for _ in range(5))
            lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in Path(folder).glob("*.jsonl"))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return off, on, lines, 5 * count + 1


def render_examples(limit):
    """Kurulu örnek şemaları çizer; LTspice'ın örnek klasörü yoksa None."""
    data = ltspice_data_dir()
    folder = data / "examples" if data else None
    if folder is None or not folder.is_dir():
        return None
    files = sorted(folder.rglob("*.asc"))
    if limit:
        files = files[:limit]
    errors, bad_xml, missing, times = [], [], Counter(), []
    for path in files:
        start = time.perf_counter()
        try:
            rendered = render_asc(path)
        except Exception as ex:  # ölçüm: her türlü istisna sayılır
            errors.append(f"{path.name}: {type(ex).__name__}: {ex}")
            continue
        times.append(time.perf_counter() - start)
        missing.update(rendered.missing_symbols)
        try:
            ET.fromstring(rendered.svg)
        except ET.ParseError as ex:
            bad_xml.append(f"{path.name}: {ex}")
    times.sort()
    return {"files": len(files), "errors": errors, "bad_xml": bad_xml, "missing": missing, "times": times}


def junction_schematic():
    """Vakaların şeması: V(+) ana kablonun bir ucunda (düğüm ``n<i>``), öbür uçta Rb, sapanın (0, 64) ucunda Ra; Ra ve Rb
    toprağa. Pin konumları ADI SCHEMATIC-REFERENCE "Standard Symbol Pin Offsets" tablosundan: res (16,16)/(16,96),
    voltage (0,16)/(0,96)."""
    lines = ["Version 4", "SHEET 1 1800 400"]
    for i, (_name, wires, (ax, ay), (bx, by), _connected) in enumerate(JUNCTION_CASES):
        dx = 400 * i
        lines += [f"WIRE {x1 + dx} {y1} {x2 + dx} {y2}" for x1, y1, x2, y2 in wires]
        ax, bx = ax + dx, bx + dx
        lines += [f"FLAG {ax} {ay} n{i}", f"FLAG {ax} {ay + 80} 0", f"FLAG {dx} 144 0", f"FLAG {bx} {by + 80} 0",
                  f"SYMBOL voltage {ax} {ay - 16} R0", f"SYMATTR InstName V{i}", "SYMATTR Value 1",
                  f"SYMBOL res {dx - 16} 48 R0", f"SYMATTR InstName RA{i}", "SYMATTR Value 1k",
                  f"SYMBOL res {bx - 16} {by - 16} R0", f"SYMATTR InstName RB{i}", "SYMATTR Value 1k"]
    return "\n".join(lines + ["TEXT 0 260 Left 2 !.op"]) + "\n"


def junction_rule():
    """[(ad, LTspice bağladı mı, beklenen, görüntüleyici nokta koydu mu)]; LTspice kurulu değilse None."""
    with tempfile.TemporaryDirectory(prefix="kavsak_") as folder:
        try:
            adapter = LTSpiceAdapter(workspace_dir=Path(folder) / "ws")
        except FileNotFoundError:
            return None
        asc = write_asc_file(Path(folder) / "kavsak.asc", junction_schematic())
        netlist = adapter.schematic_to_netlist(asc)
    nodes = {parts[0].upper(): parts[1].lower() for parts in (line.split() for line in netlist.splitlines())
             if len(parts) > 2 and parts[0][:1].upper() in "RV"}
    return [(name, nodes[f"RA{i}"] == f"n{i}", connected, (0, 0) in junctions(wires))
            for i, (name, wires, _a, _b, connected) in enumerate(JUNCTION_CASES)]


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    fast = "--hizli" in args
    off, on, lines, expected = emit_cost(500 if fast else 2000)
    print(f"Olay yazımı: kapalıyken {off * 1e9:.0f} ns/olay, açıkken {on * 1e3:.3f} ms/olay "
          f"(yazılan satır {lines}, beklenen {expected})")
    ok = lines == expected
    rule = junction_rule()
    if rule is None:
        print("LTspice bulunamadı; kavşak ölçümü atlandı.")
    else:
        for name, linked, connected, dot in rule:
            print(f"Kavşak, {name}: LTspice {'bağladı' if linked else 'bağlamadı'} (beklenen: "
                  f"{'bağlı' if connected else 'bağlı değil'}), görüntüleyici {'nokta koydu' if dot else 'nokta koymadı'}")
        ok = ok and all(linked == connected == dot for _name, linked, connected, dot in rule)
    census = render_examples(FAST_SCHEMATICS if fast else None)
    if census is None:
        print("LTspice örnek şemaları bulunamadı; çizim ölçümü atlandı.")
        return 0 if ok else 1
    times = census["times"]
    print(f"Şema çizimi: {census['files']} şema, {len(census['errors'])} istisna, {len(census['bad_xml'])} bozuk XML, "
          f"{sum(census['missing'].values())} bulunamayan sembol ({len(census['missing'])} tür)")
    if times:
        print(f"  süre: medyan {statistics.median(times) * 1e3:.1f} ms, %99 {times[int(0.99 * (len(times) - 1))] * 1e3:.0f}"
              f" ms, en uzun {times[-1] * 1e3:.0f} ms, toplam {sum(times):.1f} sn")
    for line in census["errors"][:5] + census["bad_xml"][:5]:
        print("  " + line)
    if census["missing"]:
        print("  en sık bulunamayan:", census["missing"].most_common(5))
    ok = ok and not census["errors"] and not census["bad_xml"] and not census["missing"]
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
