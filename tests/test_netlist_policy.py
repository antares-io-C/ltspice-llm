"""
Güvenilmeyen (LLM'den gelen) netlistte dosya erişimi denetimi (LTspice gerektirmez).

Kaynak: ADI LTspice AI Reference (``%LOCALAPPDATA%\\LTspice\\reference``).
- Dosya yazanlar: SIMULATION-COMMANDS-REFERENCE ".WAVE — Output WAV File" (``.wave <filename.wav> ...``, örnek
  ``.wave C:\\output.wav``), ".SAVEBIAS — Save Operating Point" (``.savebias <filename>``), ``.savestate [<filename>]``
  ve ``.tran`` seçeneği ``savestate[=<file>]``.
- Dosya okuyanlar: ".INCLUDE — Include File", ".LIB — Include Library", ``.loadstate [<filename>]``, ``.tran``
  seçeneği ``loadstate[=<file>]``, ``.ac/.dc/.noise/.step ... file=<filename>``; CIRCUIT-ELEMENTS-REFERENCE
  ``Vxxx n+ n- PWL FILE=<filename>`` ve ``Vxxx n+ n- wavefile=<filename>``.

Eskiden LLM'in netlisti olduğu gibi LTspice'a gidiyordu (inceleme 2026-10-11): ``.wave`` istenen yola dosya yazabilir,
``.include C:\\...`` çalışma alanı dışındaki bir dosyayı okutabilirdi.
"""

import pytest

from ltspice_llm.netlist import file_access_problems


def _net(line: str) -> str:
    return f"* başlık\nV1 a 0 SINE(0 1 1k)\nR1 a 0 1k\n.tran 5m\n{line}\n.end\n"


@pytest.mark.parametrize("line", [
    ".wave C:\\out.wav 16 44.1K V(a)",       # ADI örneği
    ".wave out.wav 16 44.1K V(a)",           # göreli ad da olsa yazar
    ".WAVE out.wav 16 8k V(a)",              # direktifler büyük/küçük harf duyarsız
    ".savebias bias.txt",
    ".savestate s.state",
    ".tran 1m savestate=x.state",
    ".tran 1m savestate",
])
def test_directives_that_write_files_are_refused(line):
    problems = file_access_problems(_net(line))
    assert len(problems) == 1 and "yazar" in problems[0]


@pytest.mark.parametrize("line", [
    ".include C:\\secret.txt",
    ".lib \\\\server\\share\\x.lib",
    ".inc ../../x.sub",
    ".include /etc/passwd",
    ".include \"C:\\Program Files\\x.lib\"",
    ".lib ~/x.lib",
    "V2 b 0 PWL file=C:\\data.txt",
    "V2 b 0 wavefile=..\\x.wav",
    ".step param R file=C:\\vals.txt",
    ".tran 1m loadstate=C:\\s.state",
    ".loadstate C:\\s.state",
])
def test_file_reads_outside_the_workspace_are_refused(line):
    problems = file_access_problems(_net(line))
    assert len(problems) == 1 and "çalışma alanı dışı" in problems[0]


@pytest.mark.parametrize("line", [
    ".include %LTSPICE_LLM_PROBE%\\probe.lib",      # S3 ölçümü: LTspice okudu
    ".include \"%LTSPICE_LLM_PROBE%\\probe.lib\"",  # S3 ölçümü: tırnaklı da okudu
    ".include %ltspice_llm_probe%\\probe.lib",      # S3 ölçümü: küçük harfli ad da (Windows'ta ad harf duyarsız)
    ".include %LTSPICE_LLM_PROBE(X)%\\probe.lib",   # S3 ölçümü: parantezli ad da (%ProgramFiles(x86)% gibi)
    ".lib %LTSPICE_LLM_PROBE%\\probe.sub",          # S3 ölçümü: .lib de okudu
    "V2 b 0 PWL file=%TEMP%\\d.txt",                # S3 ölçümü: okumadı (mutlak yollu kontrol okudu); güvenli yön
    ".include %USERPROFILE%\\.ssh\\config",          # politika genellemesi (başka değişken), ölçülmedi
    ".inc sub/%TEMP%/x.sub",                        # politika genellemesi (değişken yolun ortasında), ölçülmedi
    ".tran 1m loadstate=%TEMP%\\s.state",          # politika genellemesi, ölçülmedi
])
def test_file_reads_through_environment_variables_are_refused(line):
    """
    LTspice 26.1.1 netlistteki ``.include`` ve ``.lib`` yolunda ``%VAR%``'ı açıyor ve çalışma alanı dışındaki dosyayı
    okuyor: tırnaklı, küçük harfli ve parantezli ad dahil (examples/12_ltspice_reference_checks.py S3, ölçüm
    2026-10-28; her yapının mutlak yollu kontrol koşumu okudu). ADI belgesi ortam değişkenini yalnızca Settings >
    Search Paths için yazar (SCHEMATIC-REFERENCE "Symbol and Library Search Paths"). Değişkenin değeri denetim anında
    bilinmez; aynı yol denetimi her okuma yapısına uygulanır.
    """
    problems = file_access_problems(_net(line))
    assert len(problems) == 1 and "çalışma alanı dışı" in problems[0]


@pytest.mark.parametrize("line", [
    ".lib standard.dio",                     # LTspice kütüphanesi (lib/cmp, lib/sub)
    ".include opamp.sub",
    ".lib LTC.lib",
    ".lib Misc/x.lib",                       # kütüphane alt klasörü
    "V2 b 0 PWL file=pwl.txt",               # netlistin klasörü: çalışma alanı
    ".step param R list 1k 2k",
    ".meas tran x FIND V(a) AT 1m",
    "R2 a b 1k ; .wave C:\\x.wav 16 8k V(a)",  # ';' sonrası yorum
    "* .include C:\\secret.txt",               # yorum satırı
])
def test_library_names_and_workspace_files_are_allowed(line):
    assert file_access_problems(_net(line)) == []


def test_only_what_ltspice_reads_is_checked():
    """İlk satır başlıktır, ilk .end'den sonrası okunmaz (ADI SPICE-SYNTAX-REFERENCE; netlist.ltspice_visible)."""
    net = ".wave C:\\title.wav 16 8k V(a)\nR1 a 0 1k\n.op\n.end\n.include C:\\after_end.txt\n"
    assert file_access_problems(net) == []


def test_schematic_directives_and_values_are_checked():
    """.asc'de direktifler 'TEXT ... !' satırlarındadır; çok satırlı direktif metninde satırlar '\\n' ile ayrılır
    (şemaya gömülü bir .subckt bloğu gibi). Kaynak dosya değerleri SYMATTR Value satırındadır."""
    asc = (
        "Version 4\nSHEET 1 880 680\n"
        "SYMBOL voltage 0 0 R0\nSYMATTR InstName V1\nSYMATTR Value PWL file=C:\\data.txt\n"
        "TEXT 0 0 Left 2 !.tran 1m\\n.include C:\\secret.lib\n"
        "TEXT 0 40 Left 2 ;.wave C:\\comment_only.wav 16 8k V(a)\n"
        "TEXT 0 80 Left 2 !.wave out.wav 16 8k V(a)\n"
    )
    problems = file_access_problems(asc)
    assert len(problems) == 3
    assert sum("C:\\data.txt" in p for p in problems) == 1 and sum("C:\\secret.lib" in p for p in problems) == 1
    assert sum("yazar" in p for p in problems) == 1
