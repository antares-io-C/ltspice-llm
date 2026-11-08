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

import sys

import pytest

from ltspice_llm import LTSpiceAdapter
from ltspice_llm.netlist import file_access_problems
from ltspice_llm.tools import handle_tool_call


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


# --------------------------------------------------------------------------- dosya türü: netlist mi, şema mı
_WAVE_TEXT = "TEXT 0 0 Left 2 !.wave C:/x.wav 16 8k V(a)"


@pytest.mark.parametrize("asc", [
    "\ufeffVersion 4\nSHEET 1 880 680\n" + _WAVE_TEXT + "\n",   # BOM'lu UTF-8 kaydeden düzenleyici
    "Version\t4\nSHEET 1 880 680\n" + _WAVE_TEXT + "\n",
    "version 4\nSHEET 1 880 680\n" + _WAVE_TEXT + "\n",
    "Version 4\n" + _WAVE_TEXT + "\n",                           # SHEET satırı yok
], ids=["bom", "sekme", "kucuk-harf", "sheet-yok"])
def test_schematic_directives_are_checked_whatever_the_header_looks_like(asc):
    """Eski hata (bağımsız inceleme, 2026-11-08): şema yalnız 'Version ' ile başlayıp 'SHEET ' içerince şema sayılıyordu;
    bu dört biçimde TEXT direktifi hiç taranmadı. Türü bilmeyen çağrı iki biçimde de tarar, araç türü kendisi verir."""
    for kind in ("auto", "asc"):
        problems = file_access_problems(asc, kind=kind)
        assert len(problems) == 1 and problems[0].count("dosya yazar (.wave)") == 1, kind


def test_netlist_with_a_schematic_looking_title_is_still_checked():
    """Ters yön: başlığı 'Version' ile başlayıp 'SHEET ' geçen netlist şema sanılıyor, .wave satırı taranmıyordu.
    LTspice .net dosyasını netlist okur, ilk satır başlıktır (ADI SPICE-SYNTAX-REFERENCE "First line")."""
    net = "Version 4 of the filter\n* SHEET notes\nV1 a 0 1\n.wave C:/x.wav 16 8k V(a)\n.op\n.end\n"
    for kind in ("auto", "netlist"):
        problems = file_access_problems(net, kind=kind)
        assert len(problems) == 1 and problems[0].count("dosya yazar (.wave)") == 1, kind


def test_kind_follows_how_ltspice_reads_the_file():
    """.net'teki 'TEXT ... !' satırı LTspice için bir T elemanıdır, direktif değil; .asc'de satır başındaki '.wave'
    LTspice'a gitmez (şemada direktif yalnız TEXT satırındadır)."""
    asc = "Version 4\nSHEET 1 880 680\n" + _WAVE_TEXT + "\n"
    assert file_access_problems(asc, kind="netlist") == []
    net = "* t\nV1 a 0 1\n.wave C:/x.wav 16 8k V(a)\n.op\n.end\n"
    assert file_access_problems(net, kind="asc") == []
    with pytest.raises(ValueError, match="kind"):
        file_access_problems(net, kind="cir")


def _adapter(tmp_path):
    """Gerçek LTspice çalıştırmayan ama çalışma alanı mantığını kullanan adaptör (tests/test_tools_and_agent.py)."""
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path)


def test_schematic_tool_checks_the_file_as_a_schematic(tmp_path):
    """Araç .asc yazdığını bilir: başlığı ne olursa olsun TEXT direktifi denetlenir, dosya yazılmaz."""
    adapter = _adapter(tmp_path)
    asc = "\ufeffVersion 4\nSHEET 1 880 680\n" + _WAVE_TEXT + "\n"
    res = handle_tool_call("create_or_replace_schematic", {"circuit_name": "s", "asc_content": asc}, adapter=adapter)
    assert res["status"] == "ERROR" and res["message"].count("dosya yazar (.wave)") == 1
    assert not (tmp_path / "s.asc").exists()


def test_monte_carlo_does_not_turn_an_escaped_value_into_a_directive(tmp_path):
    """Eski hata (bağımsız inceleme, 2026-11-08): toleranslı .param değeri re.sub'ın yerine koyma şablonuna gömülüyordu;
    şablon '\\n'yi satır sonuna çevirir (Python belgesi, re.sub "repl" kaçışları). Oluşturulurken tek .param satırı
    olan metin Monte Carlo netlistinde '.wave' satırı oldu. LTspice'a giden netlist adaptörün yazdığı c_mc.net'tir."""
    adapter = _adapter(tmp_path)
    payload = "1,0)}\\n.wave\\tC:/x.wav\\t16\\t8000\\tV(a)\\n*"   # gerçek satır sonu yok: ters bölü + n
    assert "\n" not in payload and "\t" not in payload
    net = f"* t\nV1 a 0 1\nR1 a 0 1k\n.param x={payload}\n.op\n.end\n"
    assert file_access_problems(net, kind="netlist") == []
    (tmp_path / "c.net").write_text(net, encoding="utf-8")
    handle_tool_call("simulate_monte_carlo", {"circuit_name": "c", "tolerances": {"x": 0.1}, "runs": 2}, adapter=adapter)
    sent = (tmp_path / "c_mc.net").read_text(encoding="utf-8")
    assert file_access_problems(sent, kind="netlist") == []
    assert sent.count(payload) == 1


def test_monte_carlo_checks_the_netlist_it_sends(tmp_path):
    """Kayıtlı netlist yalnız oluşturulurken denetleniyordu; dönüşmüş netlist denetlenmeden LTspice'a gidiyordu. Çalışma
    alanındaki dosya başka bir yoldan da gelmiş olabilir (ör. allow_file_access=True ile oluşturulmuş)."""
    adapter = _adapter(tmp_path)
    (tmp_path / "c.net").write_text("* t\nV1 a 0 1\nR1 a 0 {x}\n.param x=1k\n.wave C:/x.wav 16 8k V(a)\n.op\n.end\n",
                                    encoding="utf-8")
    res = handle_tool_call("simulate_monte_carlo", {"circuit_name": "c", "tolerances": {"x": 0.1}, "runs": 2},
                           adapter=adapter)
    assert res["status"] == "ERROR" and res["message"].count("dosya yazar (.wave)") == 1
    assert not (tmp_path / "c_mc.net").exists()


def test_auto_kind_reads_the_text_both_ways():
    """'auto' türü içerikten seçmez, iki yorumu birleştirir. İlki: netlistte ilk .end'den sonrası okunmaz, ama orada bir
    TEXT satırı var diye metin şema sayılırsa .wave kaçar. İkincisi: her yorumda bir yapı, ikisi de bildirilir."""
    trailing = "* t\nV1 a 0 1\n.wave C:/x.wav 16 8k V(a)\n.op\n.end\nTEXT 0 0 Left 2 !.op\n"
    problems = file_access_problems(trailing)
    assert len(problems) == 1 and problems[0].count("dosya yazar (.wave)") == 1
    mixed = "* t\nV1 a 0 1\n.wave C:/x.wav 16 8k V(a)\nTEXT 0 0 Left 2 !.include C:/secret.lib\n.op\n.end\n"
    problems = file_access_problems(mixed)
    assert len(problems) == 2
    assert sum(p.count("dosya yazar (.wave)") for p in problems) == 1
    assert sum(p.endswith("dosyayı okur (C:/secret.lib).") for p in problems) == 1


def test_each_writing_line_is_reported_and_each_outside_path_once():
    """file_access_problems belgesi: her yazan satır ayrı, dışarıyı okuyan yapılar yola göre bir kez bildirilir."""
    two_waves = "* t\nV1 a 0 1\n.wave C:/a.wav 16 8k V(a)\n.wave C:/b.wav 16 8k V(a)\n.op\n.end\n"
    assert len(file_access_problems(two_waves, kind="netlist")) == 2
    same_path = "* t\n.include C:/a.lib\nR1 a 0 1k\n.lib C:/a.lib\n.op\n.end\n"
    assert len(file_access_problems(same_path, kind="netlist")) == 1


def test_modify_parameters_checks_the_stored_netlist_without_parameters(tmp_path):
    """Eski hata (inceleme 2026-11-08): parametre verilmeyince kayıtlı .net hiç denetlenmeden koşuyordu. Dosya başka bir
    yoldan gelmiş olabilir (Monte Carlo testiyle aynı gerekçe)."""
    adapter = _adapter(tmp_path)
    (tmp_path / "m.net").write_text("* t\nV1 a 0 1\nR1 a 0 1k\n.wave C:/x.wav 16 8k V(a)\n.op\n.end\n", encoding="utf-8")
    res = handle_tool_call("modify_parameters", {"circuit_name": "m"}, adapter=adapter)
    assert res["status"] == "ERROR" and res["message"].count("dosya yazar (.wave)") == 1


@pytest.mark.parametrize("value, path", [("PWL file=C:/data.txt", "C:/data.txt"), ("wavefile=C:/in.wav", "C:/in.wav")])
def test_schematic_value_reading_a_file_is_checked_as_a_schematic(value, path):
    """Şema aracı türü 'asc' verir; orada SYMATTR değerindeki file=/wavefile= yalnız şema yorumuyla görülür. 'auto' iki
    yorumu birleştirdiği için bu dalı tek başına sınamaz: haftalık iş (2026-11-08) bu dalı kapatan mutantın sağ
    kaldığını gösterdi. Kaynak: CIRCUIT-ELEMENTS-REFERENCE "PWL FILE=" ve "wavefile=" (bu dosyanın başı)."""
    asc = f"Version 4\nSHEET 1 880 680\nSYMBOL voltage 0 0 R0\nSYMATTR InstName V1\nSYMATTR Value {value}\n"
    problems = file_access_problems(asc, kind="asc")
    assert len(problems) == 1 and problems[0].endswith(f"dosyayı okur ({path}).")
