"""
LTspice sembol (.asy) dosyalarından pin konumları (Gemini incelemesi Madde 2, 2026-10-06).

Birim testlerindeki .asy satırları LTspice 26.1.1 ``lib/sym`` dosyalarından birebir alıntıdır (dosya adı yanında).
Entegrasyon testleri gerçek kütüphaneyi ve LTspice'ın kendi netlisterını kullanır; LTspice yoksa atlanır.
Ölçümler: examples/16 (6642 sembol, 126'sı UTF-16LE, 7'sinde bazı pinlerin PinName'i yok).
"""

import os
import posixpath
import re
import types
from pathlib import Path

import pytest

import ltspice_llm.reference as reference_mod
from ltspice_llm import AscSchematic, LTSpiceAdapter, ltspice_data_dir, ltspice_user_dir
from ltspice_llm.symbols import find_symbol, read_symbol

# LTspice 26.1.1'in yazdığı ayar dosyasının başı (BOM'lu UTF-16LE; examples/14 ``ini``, yollar C:\ws\user)
INI = Path(__file__).parent / "data" / "ref26_LTspice.ini"

# OpAmps/LT1001.asy (PIN ve PINATTR satırları, dosya sırasıyla)
LT1001 = """Version 4
SymbolType CELL
SYMATTR Prefix X
SYMATTR Description Precision Operational Amplifier
PIN -32 80 NONE 0
PINATTR PinName In+
PINATTR SpiceOrder 1
PIN -32 48 NONE 0
PINATTR PinName In-
PINATTR SpiceOrder 2
PIN 0 32 NONE 0
PINATTR PinName V+
PINATTR SpiceOrder 3
PIN 0 96 NONE 0
PINATTR PinName V-
PINATTR SpiceOrder 4
PIN 32 64 NONE 0
PINATTR PinName OUT
PINATTR SpiceOrder 5
"""
# PowerProducts/LT8610.asy'nin ilk altı pini: SpiceOrder dosya sırasından farklı (PGND 13, FB 6)
LT8610_HEAD = """Version 4
SYMATTR Prefix X
PIN -144 -96 LEFT 8
PINATTR PinName SYNC
PINATTR SpiceOrder 1
PIN -144 96 LEFT 8
PINATTR PinName TR/SS
PINATTR SpiceOrder 2
PIN 64 240 BOTTOM 8
PINATTR PinName PGND
PINATTR SpiceOrder 13
PIN 144 192 RIGHT 8
PINATTR PinName FB
PINATTR SpiceOrder 6
"""
# OpAmps/ADA4254.asy'nin ilk üç pini: 1. ve 2. pinin PinName'i yok
ADA4254_HEAD = """Version 4
SYMATTR Prefix X
PIN -224 0 LEFT 8
PINATTR SpiceOrder 1
PIN -224 128 LEFT 8
PINATTR SpiceOrder 2
PIN -176 -96 TOP 8
PINATTR PinName VDDH
PINATTR SpiceOrder 3
"""


def _write(folder, rel, text, encoding="cp1252"):
    path = folder.joinpath(*rel.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", "\r\n").encode(encoding))
    return path


@pytest.fixture
def no_library(monkeypatch, tmp_path_factory):
    """Birim testleri kurulu ADI kütüphanesine düşmesin: alıntılanan LT1001.asy kütüphanedekiyle pin pin aynı, bu yüzden
    kütüphaneye düşen bir hata (ör. symbol_dirs'in yok sayılması) LTspice kurulu makinede görünmüyordu (test-reviewer).
    Kullanıcının LTspice klasörüne (LTspice.ini UserCmpDir) de düşmesin: APPDATA'da LTspice.ini yok."""
    monkeypatch.setattr("ltspice_llm.symbols.library_symbol_dir", lambda: None)
    monkeypatch.setenv("APPDATA", str(tmp_path_factory.mktemp("appdata_without_ini")))


@pytest.fixture
def user_dir(tmp_path, monkeypatch, no_library):
    """``%APPDATA%\\LTspice.ini``'nin UserCmpDir'i geçici bir klasörü gösterir (işletim sistemi sınırı: ortam
    değişkeni). Dosya LTspice'ın yazdığı kayıttır; yalnızca yol değişir, kodlama aynı kalır."""
    user = tmp_path / "user"
    user.mkdir()
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    data = INI.read_bytes()
    assert data[:2] == b"\xff\xfe"  # kayıt BOM'lu UTF-16LE
    text = data[2:].decode("utf-16-le").replace("C:\\ws\\user", str(user))
    (appdata / "LTspice.ini").write_bytes(data[:2] + text.encode("utf-16-le"))
    monkeypatch.setenv("APPDATA", str(appdata))
    return user


@pytest.mark.parametrize("encoding", ["cp1252", "utf-16-le"])  # lib/sym: 6516 cp1252, 126 UTF-16LE (BOM'suz)
def test_symbol_pins_are_read_in_spice_order(tmp_path, encoding):
    sym = read_symbol(_write(tmp_path, "OpAmps/LT1001.asy", LT1001, encoding))
    assert sym.name == "LT1001" and sym.prefix == "X"
    assert sym.description == "Precision Operational Amplifier"
    assert sym.spice_order == ["In+", "In-", "V+", "V-", "OUT"]
    assert sym.pin("OUT") == (32, 64) and sym.pin("in+") == (-32, 80)  # ad büyük/küçük harf duyarsız
    assert sym.pin("4") == (0, 96)  # SpiceOrder numarası


def test_netlist_order_follows_spice_order_not_file_order(tmp_path):
    sym = read_symbol(_write(tmp_path, "LT8610.asy", LT8610_HEAD))
    assert sym.spice_order == ["SYNC", "TR/SS", "FB", "PGND"]
    assert sym.pin("13") == sym.pin("PGND") == (64, 240)


def test_pin_without_a_name_is_reached_by_its_spice_order(tmp_path):
    sym = read_symbol(_write(tmp_path, "ADA4254.asy", ADA4254_HEAD))
    assert sym.pin("2") == (-224, 128) and sym.pin("VDDH") == (-176, -96)
    with pytest.raises(KeyError, match="VDDH"):  # hata mesajı tanımlı pinleri sayar
        sym.pin("IN-")


@pytest.mark.parametrize("name", ["OpAmps\\LT1001", "OpAmps\\\\LT1001", "opamps/lt1001", "LT1001"])
def test_symbol_is_found_by_library_path_or_bare_name(tmp_path, name, no_library):
    """.asc'de klasörlü ad iki ters bölüyle yazılır (ADI örnekleri: 'SYMBOL PowerProducts\\\\LT8610A'); yalın ad da
    olur ('SYMBOL LT8610'). Büyük/küçük harf: 'POWERPRODUCTS\\\\...' ve 'opamps\\\\...' ikisi de örneklerde var."""
    path = _write(tmp_path, "OpAmps/LT1001.asy", LT1001)
    assert find_symbol(name, [tmp_path]) == path


@pytest.mark.parametrize("name", ["Beta\\\\LT1001", "Beta\\LT1001", "beta/LT1001.asy"])
def test_library_path_wins_over_a_same_named_symbol_in_another_folder(tmp_path, name, no_library):
    """Yalın ad yalnızca yol eşleşmezse kullanılır; aynı adlı iki sembolde .asc'deki klasör belirleyicidir."""
    _write(tmp_path, "Alpha/LT1001.asy", LT1001)
    beta = _write(tmp_path, "Beta/LT1001.asy", LT1001)
    assert find_symbol(name, [tmp_path]) == beta


def test_dotted_symbol_name_keeps_its_dot(tmp_path, no_library):
    """PowerProducts/ADP7182-1.2.asy (UTF-16LE): uzantıdan önceki nokta adın parçası."""
    path = _write(tmp_path, "PowerProducts/ADP7182-1.2.asy", LT1001, "utf-16-le")
    assert find_symbol("PowerProducts\\\\ADP7182-1.2", [tmp_path]) == path
    assert find_symbol("ADP7182-1", [tmp_path]) is None


def test_first_search_directory_wins(tmp_path, no_library):
    """ADI SCHEMATIC-REFERENCE 'Default Search Priority': şemanın klasörü, kullanıcı yolları, ... ADI kütüphanesi."""
    mine = _write(tmp_path / "mine", "LT1001.asy", LT1001)
    _write(tmp_path / "lib", "OpAmps/LT1001.asy", LT1001)
    assert find_symbol("OpAmps\\LT1001", [tmp_path / "mine", tmp_path / "lib"]) == mine


def test_user_files_directory_is_read_from_ltspice_ini(user_dir):
    """LTspice kullanıcı klasörünü ``LTspice.ini``'deki ``UserCmpDir``'den alır. Bu makinede Belgeler OneDrive'a
    yönlendirilmiş; ``~/Documents/LTspice`` yanlış klasör olurdu (2026-10-06)."""
    assert ltspice_user_dir() == user_dir


def test_without_ltspice_ini_there_is_no_user_files_directory(no_library):
    assert ltspice_user_dir() is None


def test_user_files_directory_that_does_not_exist_is_none(user_dir):
    """UserCmpDir silinmiş ya da başka makineden gelmiş bir klasörü gösterebilir (test-reviewer)."""
    user_dir.rmdir()
    assert ltspice_user_dir() is None


def _set_user_cmp_dir(user_dir, value):
    """``user_dir`` fixture'ının LTspice.ini'sinde UserCmpDir'i ``value`` yapar (kodlama aynı, yalnızca yol değişir)."""
    ini = Path(os.environ["APPDATA"]) / "LTspice.ini"
    data = ini.read_bytes()
    text = data[2:].decode("utf-16-le").replace(str(user_dir), value)
    ini.write_bytes(data[:2] + text.encode("utf-16-le"))


def test_user_files_directory_may_use_an_environment_variable(user_dir, monkeypatch):
    """ADI SCHEMATIC-REFERENCE "Configuration (Tools > Settings > Search Paths)": "Environment variables supported in
    all paths"."""
    # Windows'ta LTspice'ın yazdığı %LTSPICE_LLM_TEST_ROOT%\user; Linux CI'da ayırıcı / (fixture'daki yol gibi)
    _set_user_cmp_dir(user_dir, os.path.join("%LTSPICE_LLM_TEST_ROOT%", "user"))
    monkeypatch.setenv("LTSPICE_LLM_TEST_ROOT", str(user_dir.parent))
    assert ltspice_user_dir() == user_dir


def test_user_files_variable_is_expanded_on_posix_too(user_dir, monkeypatch):
    """LTspice.ini Windows biçimindedir: %VAR% her işletim sisteminde açılmalı. Linux'ta os.path = posixpath %VAR%'ı
    açmaz; CI'ın Ubuntu job'ları bu yüzden kırmızıydı (2026-10-11). İşletim sistemi sınırı: modülün gördüğü os.path
    POSIX'inki olur, yol POSIX'teki gibi / ile ayrılır (Windows da / kabul eder)."""
    _set_user_cmp_dir(user_dir, "%LTSPICE_LLM_TEST_ROOT%/user")
    monkeypatch.setenv("LTSPICE_LLM_TEST_ROOT", str(user_dir.parent))
    monkeypatch.setattr(reference_mod, "os", types.SimpleNamespace(environ=os.environ, path=posixpath))
    assert ltspice_user_dir() == user_dir


@pytest.mark.parametrize("name,rel,found", [
    ("zz_usersym", "zz_usersym.asy", True),
    ("sub\\\\zz_subsym", "sub/zz_subsym.asy", True),
    ("zz_subsym", "sub/zz_subsym.asy", False),
    ("zz_usersym2", "sym/zz_usersym2.asy", False),
    ("zz_userlib", "lib/sym/zz_userlib.asy", False),
])
def test_user_files_symbols_are_found_as_ltspice_finds_them(user_dir, name, rel, found):
    """ADI SCHEMATIC-REFERENCE "Default Search Priority" 3: User Files klasörü (Gemini, 2026-10-06). Ölçüm (LTspice
    26.1.1 ``-netlist``, ``-ini`` ile geçici UserCmpDir): klasörün kendisindeki sembol yalın adla, alt klasördeki yalnızca
    göreli yolla bulunuyor; ``sym/`` ve ``lib/sym/`` alt klasörleri de yalın adla aranmıyor."""
    path = _write(user_dir, rel, LT1001)
    assert find_symbol(name) == (path if found else None)


def test_user_files_come_after_search_directories_and_before_the_library(tmp_path, user_dir, monkeypatch):
    """Sıra: şemanın klasörü (search_dirs), ..., User Files, ADI kütüphanesi."""
    library = tmp_path / "lib_sym"
    lib = _write(library, "OpAmps/LT1001.asy", LT1001)
    monkeypatch.setattr("ltspice_llm.symbols.library_symbol_dir", lambda: library)
    mine = _write(tmp_path / "mine", "LT1001.asy", LT1001)
    user = _write(user_dir, "LT1001.asy", LT1001)
    assert find_symbol("LT1001", [tmp_path / "mine"]) == mine
    mine.unlink()
    assert find_symbol("LT1001", [tmp_path / "mine"]) == user
    user.unlink()
    assert find_symbol("LT1001", [tmp_path / "mine"]) == lib


def test_schematic_wires_a_library_symbol_by_pin_name(tmp_path, no_library):
    sch = AscSchematic(symbol_dirs=[tmp_path])
    _write(tmp_path, "OpAmps/LT1001.asy", LT1001)
    u1 = sch.add_symbol("OpAmps\\\\LT1001", 400, 200, inst_name="U1")
    assert sch.get_pin(u1, "OUT") == (432, 264)
    u2 = sch.add_symbol("OpAmps\\\\LT1001", 400, 200, rotation="R90", inst_name="U2")
    assert sch.get_pin(u2, "OUT") == (336, 232)  # R90: (x - py, y + px), ADI SCHEMATIC-REFERENCE
    with pytest.raises(KeyError, match="In\\+"):
        sch.get_pin(u1, "VCC")


def test_unknown_symbol_still_raises(tmp_path, no_library):
    sch = AscSchematic(symbol_dirs=[tmp_path])
    sym = sch.add_symbol("NoSuchPart", 0, 0)
    with pytest.raises(KeyError):
        sch.get_pin(sym, "1")


# --------------------------------------------------------------------------- gerçek kütüphane + LTspice
@pytest.fixture(scope="module")
def library():
    data = ltspice_data_dir()
    sym = data / "lib" / "sym" if data else None
    if sym is None or not sym.is_dir():
        pytest.skip("LTspice sembol kütüphanesi (lib/sym) bu makinede yok")
    return sym


def test_library_symbols_are_found_without_search_directories(library):
    """Arama dizini verilmezse ADI kütüphanesi aranır; pinler yukarıdaki alıntıyla (OpAmps/LT1001.asy) aynı."""
    assert find_symbol("LT1001") == library / "OpAmps" / "LT1001.asy"
    sym = read_symbol(find_symbol("OpAmps\\\\LT1001"))
    assert sym.spice_order == ["In+", "In-", "V+", "V-", "OUT"] and sym.pin("OUT") == (32, 64)
    sch = AscSchematic()
    assert sch.get_pin(sch.add_symbol("PowerProducts\\\\LT8610", 0, 0), "PGND") == (64, 240)  # LT8610.asy alıntısı


@pytest.fixture(scope="module")
def adapter(tmp_path_factory, library):
    try:
        return LTSpiceAdapter(workspace_dir=tmp_path_factory.mktemp("sym_ws"))
    except FileNotFoundError:
        pytest.skip("LTspice bu makinede kurulu değil")


def test_every_lt8610_pin_lands_on_its_net_in_spice_order(adapter):
    """13 pinli bir ADI parçası: her pinin koordinatına adıyla bir ağ etiketi konur. LTspice'ın netlisterı her pini
    o ağa bağlamalı (NC_xx yok) ve X satırındaki düğüm sırası PINATTR SpiceOrder olmalı."""
    sch = AscSchematic()
    order = read_symbol(find_symbol("LT8610")).spice_order
    assert len(order) == 13
    for rotation, x in (("R0", 400), ("R90", 1200)):
        u = sch.add_symbol("PowerProducts\\\\LT8610", x, 400, rotation=rotation, inst_name=f"XU_{rotation}")
        for name in order:
            sch.add_net_label(*sch.get_pin(u, name), f"{rotation}_{re.sub(r'[^A-Za-z0-9]', '_', name)}")
    sch.add_directive(0, 800, ".op")
    report = adapter.inspect_schematic(sch.render(), treat_as_text=True)
    assert report["unconnected_pins"] == [] and report["dangling_nets"] == []
    for rotation in ("R0", "R90"):
        # LTspice 26.1.1 netlisterı alt devre örneğine "X§" ekler: "X§XU_R0 R0_SYNC ... LT8610 ;§pnba SYNC)..."
        line = next(ln for ln in report["netlist"].splitlines() if ln.startswith(f"X§XU_{rotation} "))
        code, _, comment = line.partition(";")
        assert code.split()[1:1 + len(order)] == [f"{rotation}_{re.sub(r'[^A-Za-z0-9]', '_', n)}" for n in order]
        assert comment.strip().removeprefix("§pnba ").split(")") == order  # LTspice'ın kendi pin adı sırası


def test_lt1001_follower_built_from_library_pins_follows_its_input(adapter, tmp_path):
    """Gerilim izleyici (In- = OUT): ideal ise V(out) = V(in) (elle). LT1001 ofseti µV mertebesi; 1 mV tolerans.
    Yanlış pin eşlemesi besleme ya da geri beslemeyi kopartır, çıkış raya gider."""
    sch = AscSchematic()
    u1 = sch.add_symbol("OpAmps\\\\LT1001", 400, 200, inst_name="U1")
    vin = sch.add_symbol("voltage", 160, 240, inst_name="V1", value="1.25")
    vp = sch.add_symbol("voltage", 640, 80, inst_name="V2", value="5")
    vn = sch.add_symbol("voltage", 640, 320, inst_name="V3", value="5")  # + toprakta, - ucu vee = -5 V
    sch.connect(sch.get_pin(vin, "+"), sch.get_pin(u1, "In+"))
    sch.connect(sch.get_pin(u1, "OUT"), (496, sch.get_pin(u1, "OUT")[1]))
    sch.add_net_label(*sch.get_pin(u1, "OUT"), "out")
    sch.add_net_label(*sch.get_pin(u1, "In-"), "out")
    sch.add_net_label(*sch.get_pin(u1, "V+"), "vcc")
    sch.add_net_label(*sch.get_pin(vp, "+"), "vcc")
    sch.add_net_label(*sch.get_pin(u1, "V-"), "vee")
    sch.add_net_label(*sch.get_pin(vn, "-"), "vee")
    for src, pin in ((vin, "-"), (vp, "-"), (vn, "+")):
        sch.add_ground(*sch.get_pin(src, pin))
    sch.add_directive(0, 480, ".op")
    sch.add_directive(0, 520, ".meas op vout FIND V(out)")
    assert adapter.inspect_schematic(sch.render(), treat_as_text=True)["unconnected_pins"] == []
    res = adapter.run_netlist(sch.save(tmp_path / "follower.asc"), circuit_name="sym_follower")
    assert res.success, res.errors
    assert res.measurements["vout"] == pytest.approx(1.25, abs=1e-3)
