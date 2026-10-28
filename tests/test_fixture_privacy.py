"""
tests/data'daki gerçek LTspice kayıtları kaydı yapan makinenin yolunu taşımaz: depo public, gerçek yol kullanıcı adını
ve klasör düzenini gösterir. Kaydedici (examples/14) çalışma alanını ``C:\\ws`` yapar; logda, netlistte,
``LTspice.ini``'de ve ``.raw`` başlığında aynı kural. Eskiden ``.raw`` olduğu gibi kopyalanıyordu: 11 kaydın ``Title:``
satırı kayıt klasörünü (``C:\\<eski klasör>\\.scratch\\flow_fixtures``) gösteriyordu (2026-10-28).
"""

import importlib.util
import re
import sqlite3
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

DATA = Path(__file__).parent / "data"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
# Sürücülü mutlak Windows yolu (C:\..., c:/...); "http://" gibi önünde harf olan iki nokta sayılmaz
_DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"'<>|*?]*")
# UNC yolu (\\bilgisayar\paylaşım\...): bilgisayar adını ve klasörü gösterir
_UNC_PATH = re.compile(r"(?<![\\\w])\\\\[^\\\s]+\\")
_USER_DIR = re.compile(r"[\\/]users[\\/][^\\/\s]+", re.IGNORECASE)


def _text(path):
    """Kaydın metni: ``.db``'nin metin değerleri, ikili ``.raw``'ın başlığı, öbürlerinin tamamı."""
    if path.suffix == ".db":
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            tables = [row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            return "\n".join(v for t in tables for row in con.execute(f'SELECT * FROM "{t}"') for v in row
                             if isinstance(v, str))
        finally:
            con.close()
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data[2:].decode("utf-16-le")
    if data[1:2] == b"\x00":  # ikili .raw: UTF-16LE başlık, ardından sayılar
        return data[:data.index("Binary:".encode("utf-16-le"))].decode("utf-16-le")
    return data.decode("utf-8")


def test_recordings_carry_no_path_of_the_recording_machine():
    leaks = {}
    for path in sorted(p for p in DATA.rglob("*") if p.is_file()):
        name = path.relative_to(DATA).as_posix()
        try:
            text = _text(path)
        except (ValueError, UnicodeDecodeError, sqlite3.DatabaseError) as exc:  # BOM'suz UTF-16, cp1252...
            raise AssertionError(f"{name}: kayıt okunamadı ({exc})") from exc
        found = [m for m in _DRIVE_PATH.findall(text) if not re.match(r"C:\\ws(?:\\|$)", m)]
        found += _UNC_PATH.findall(text) + _USER_DIR.findall(text)
        if found:
            leaks[name] = sorted(set(found))
    assert leaks == {}


@pytest.fixture(scope="module")
def recorder():
    spec = importlib.util.spec_from_file_location("example_14_record_flow_fixtures",
                                                  EXAMPLES / "14_record_flow_fixtures.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# Yeni bir kaydı taklit eder: kayıttaki C:\ws, kayıt makinesinin çalışma alanına geri yazılır. İkili .raw'da başlık
# UTF-16LE (boşluklu, Türkçe harfli yol); ASCII .raw'da tek baytlık, yol ASCII (LTspice'ın ASCII dışı harfi ASCII
# .raw'a nasıl yazdığı ölçülmedi).
@pytest.mark.parametrize("name, workspace, enc, marker", [
    ("ref26_flow_rc.raw", r"D:\Kullanıcılar\Örnek Kullanıcı\ltspice-llm\.scratch\flow_fixtures", "utf-16-le", "Binary:"),
    ("ref26_step_dc_ascii.raw", r"D:\Users\someone\ltspice-llm\.scratch\flow_fixtures", "utf-8", "Values:"),
])
def test_scrub_raw_returns_a_fresh_recording_to_the_recorded_bytes(recorder, name, workspace, enc, marker):
    recorded = (DATA / name).read_bytes()
    end = recorded.index(marker.encode(enc))
    fresh = recorded[:end].decode(enc).replace("C:\\ws", workspace).encode(enc) + recorded[end:]
    assert fresh.count(workspace.encode(enc)) == 1  # kayıttaki Title: C:\ws\<ad>.net satırı
    assert recorder.scrub_raw(fresh, workspace) == recorded


@given(payload=st.binary(max_size=300))
def test_scrub_raw_never_touches_the_data_after_the_header(recorder, payload):
    """Veri kısmı çalışma alanının baytlarını da içerse değişmez; yalnızca başlık C:\\ws olur."""
    workspace = r"D:\Örnek Kullanıcı\flow_fixtures"
    enc = "utf-16-le"
    data = workspace.encode(enc) + payload
    raw = f"Title: {workspace}\\x.net\nDate: d\nBinary:\n".encode(enc) + data
    assert recorder.scrub_raw(raw, workspace) == "Title: C:\\ws\\x.net\nDate: d\nBinary:\n".encode(enc) + data


def test_scrub_raw_refuses_a_header_without_its_end_marker(recorder):
    """Binary:/Values: satırı yoksa başlığın nerede bittiği bilinmez: kayıt yazılmaz, hata verir."""
    with pytest.raises(ValueError, match="başlığı bulunamadı"):
        recorder.scrub_raw("Title: D:\\w\\x.net\nDate: d\n".encode("utf-16-le"), "D:\\w")


@pytest.mark.parametrize("title", [
    "D:\\Kullan?c?\\flow_fixtures\\x.net",  # çalışma alanı başlıkta başka yazılmış (ASCII .raw'da ölçülmedi)
    "C:\\eski-klasor\\.scratch\\flow_fixtures\\x.net",  # C: sürücüsünde başka bir kayıt klasörü (11 kayıttaki biçim)
    "\\\\BILGISAYAR\\paylasim\\flow_fixtures\\x.net",  # UNC yolu
])
def test_scrub_raw_refuses_a_header_that_keeps_a_path_of_the_recording_machine(recorder, title):
    """Çalışma alanı başlıktaki yazımla eşleşmezse yol kalır; scrub_raw onu sessizce yazmaz, hata verir."""
    raw = f"Title: {title}\nDate: d\nBinary:\n".encode("utf-16-le") + b"\x00\x01"
    with pytest.raises(ValueError, match="kayıt yolu kaldı"):
        recorder.scrub_raw(raw, "D:\\Kullanıcı\\flow_fixtures")
