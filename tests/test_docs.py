"""
docs/GUIDE.md, docs/KILAVUZ.md'nin İngilizce çevirisidir (CHANGELOG 1.5.0). İki belgenin yapısı aynı kalmalı:
biri değişip öbürü unutulursa okur eski bilgiyi görür. Kod bloklarında yalnız yorum metni çevrilir, kod çevrilmez.
"""

import re
from pathlib import Path

DOCS = Path(__file__).parent.parent / "docs"


def _code_without_comment_text(line):
    line = re.sub(r'^(.*""")\*.*$', r"\1*", line)  # Python metni içindeki netlist başlık satırı (SPICE yorumu)
    return re.sub(r"#.*$", "#", line).rstrip()


def _structure(name):
    """(başlık düzeyleri, tablo satırı sayısı, kod blokları): kod bloklarının içi başlık ya da tablo sayılmaz."""
    headings, table_rows, blocks = [], 0, []
    block = None
    for line in (DOCS / name).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            if block is None:
                block = [line.strip()]
            else:
                blocks.append(block)
                block = None
        elif block is not None:
            block.append(_code_without_comment_text(line))
        elif line.startswith("#"):
            headings.append(len(line) - len(line.lstrip("#")))
        elif line.startswith("|"):
            table_rows += 1
    assert block is None, f"{name}: kapanmamış kod bloğu"
    return headings, table_rows, blocks


def test_english_guide_has_the_same_structure_as_the_turkish_original():
    tr_headings, tr_tables, tr_blocks = _structure("KILAVUZ.md")
    en_headings, en_tables, en_blocks = _structure("GUIDE.md")
    assert len(tr_headings) > 20 and len(tr_blocks) > 10 and tr_tables > 20  # belgeler gerçekten okundu
    assert en_headings == tr_headings
    assert en_tables == tr_tables
    assert len(en_blocks) == len(tr_blocks)
    for number, (tr_block, en_block) in enumerate(zip(tr_blocks, en_blocks), 1):
        assert en_block == tr_block, f"{number}. kod bloğu iki belgede farklı (yalnız yorum metni çevrilir)"


_NUMBER = re.compile(r"(?<![\w.])[-+]?\d+(?:[.,]\d+)*(?:[eE][-+]?\d+)?")


def _prose_numbers(name):
    """Her başlık bölümünde, kod blokları ve satır içi kod dışındaki sayılar (sıralanmış: çeviri bir cümlenin içindeki
    sayıların sırasını değiştirebilir). Türkçe ondalık virgül noktaya çevrilir; bağlantı hedefindeki sayılar (dosya
    adları) sayılmaz. Başlıklar bölüm sınırıdır, sayıları ayrıca sayılmaz."""
    sections, current, in_code = [], [], False
    for line in (DOCS / name).read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif in_code:
            continue
        elif line.startswith("#"):
            sections.append(sorted(current))
            current = []
        else:
            line = re.sub(r"`[^`]*`", "", re.sub(r"\]\([^)]*\)", "]", line))
            current += [number.replace(",", ".") for number in _NUMBER.findall(line)]
    sections.append(sorted(current))
    return sections


def test_measured_numbers_in_prose_and_tables_are_the_same_in_both_guides():
    """Ölçüm sayıları (performans tabloları, süreler, oranlar) düzyazı ve tablolarda durur; yapı testi onları görmez.
    Çeviride biri değişirse okur İngilizce belgede başka bir ölçüm görür."""
    tr, en = _prose_numbers("KILAVUZ.md"), _prose_numbers("GUIDE.md")
    assert len(tr) == len(en) and sum(map(len, tr)) > 100  # belgeler gerçekten okundu
    for number, (tr_numbers, en_numbers) in enumerate(zip(tr, en)):
        assert en_numbers == tr_numbers, f"{number}. bölümün sayıları iki belgede farklı"


def test_the_two_guides_link_to_each_other():
    assert (DOCS / "KILAVUZ.md").read_text(encoding="utf-8").count("[GUIDE.md](GUIDE.md)") == 1
    assert (DOCS / "GUIDE.md").read_text(encoding="utf-8").count("[KILAVUZ.md](KILAVUZ.md)") == 1
