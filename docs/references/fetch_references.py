"""Dış kaynakları (app note, datasheet, el kitabı) indirir, doğrular ve aranabilir metne çevirir.

Kullanım:
    python docs/references/fetch_references.py            # eksikleri indir, README.md'yi üret
    python docs/references/fetch_references.py --force    # hepsini yeniden indir
    python docs/references/fetch_references.py --only adi-an88 adi-mt094
    python docs/references/fetch_references.py --catalog  # yalnızca README.md'yi üret
    python docs/references/fetch_references.py --recheck  # yerel PDF'leri indirmeden yeniden doğrula

Liste ``manifest.json``'da. PDF'ler ``files/<kategori>/<id>.pdf``'e, düz metinleri ``text/<id>.txt``'ye yazılır;
ikisi de git'e girmez (üretici belgeleri: kişisel/kurum içi kullanım, yeniden dağıtım yok). ``files/index.json``
her belgenin kaynağını (doğrudan / Wayback), SHA-256'sını ve sayfa sayısını tutar.

analog.com ve st.com (Akamai) tarayıcı başlıkları (Accept-Language, Sec-Fetch-*) olmayan isteği düşürüyor; Python
``requests``'i (OpenSSL) başlıklarla da 60 sn bekletip düşürüyor, başsız Edge'e "Access Denied" veriyor. Windows'un
Schannel kullanan ``curl``'ü tarayıcı başlıklarıyla 0,6 sn'de indiriyor (ölçüldü 2026-10-02). Bu yüzden indirme
``curl`` ile yapılır. Doğrudan indirme yine başarısız olursa belge Internet Archive'daki ham kopyadan (``id_``)
alınır.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote, urlparse

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "manifest.json"
FILES = ROOT / "files"
TEXT = ROOT / "text"
INDEX = FILES / "index.json"
README = ROOT / "README.md"

CURL = shutil.which("curl")
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}
CONNECT_TIMEOUT = 20  # saniye
MAX_TIME = 300  # saniye, tek indirmenin üst sınırı
EXPECT_PAGES = 3  # 'expect' metni ilk kaç sayfada aranır

# Bir koşumda bağlantısı kopan ya da zaman aşımına uğrayan sunucular: sonraki belgelerde doğrudan denenmez.
# Engelleyen bir sunucu her istekte 60 sn bekletebiliyor; 15 belgede bu 15 dk kayıp demek.
_blocked_hosts: set[str] = set()
CURL_NETWORK_ERRORS = {7, 28, 35, 52, 55, 56}  # bağlanamadı, zaman aşımı, TLS, boş yanıt, gönderme/alma

# Internet Archive ardışık isteklerde HTTP 429 döndürüyor (ölçüldü: 1 sn arayla ~15 istekten sonra).
WAYBACK_GAP = 5  # saniye, iki Archive isteği arası
RETRIES_429 = 3


def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def load_index() -> dict:
    if INDEX.exists():
        return json.loads(INDEX.read_text(encoding="utf-8"))
    return {}


def _curl(url: str, out: Path) -> tuple[int, str]:
    """URL'yi ``out``'a indirir; (HTTP kodu, Content-Type). Ağ hatasında ``OSError``."""
    cmd = [CURL, "-sS", "-L", "--compressed", "--connect-timeout", str(CONNECT_TIMEOUT),
           "--max-time", str(MAX_TIME), "-o", str(out), "-w", "%{http_code} %{content_type}"]
    for key, value in BROWSER_HEADERS.items():
        cmd += ["-H", f"{key}: {value}"]
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=MAX_TIME + 30)
    if proc.returncode:
        raise OSError(proc.returncode, proc.stderr.strip() or f"curl çıkış kodu {proc.returncode}")
    code, _, kind = proc.stdout.partition(" ")
    return int(code) if code.isdigit() else 0, kind


def _get(url: str) -> tuple[int, str, bytes] | None:
    """HTTP 429'da bekleyip yeniden dener. Ağ hatasında sunucuyu engelli sayar ve None döner."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "body"
        for attempt in range(1, RETRIES_429 + 1):
            try:
                code, kind = _curl(url, out)
            except OSError as exc:
                print(f"    curl {exc.args[0]}: {url}")
                if exc.args[0] in CURL_NETWORK_ERRORS:
                    _blocked_hosts.add(urlparse(url).hostname)
                return None
            if code != 429 or attempt == RETRIES_429:
                break
            print(f"    HTTP 429 (hız sınırı), {60 * attempt} sn bekleniyor")
            time.sleep(60 * attempt)
        body = out.read_bytes() if out.exists() else b""
    return code, kind, body


def _get_pdf(url: str) -> bytes | None:
    got = _get(url)
    if got is None:
        return None
    code, kind, body = got
    if code != 200 or not body.startswith(b"%PDF"):
        print(f"    HTTP {code}, {kind or '?'}, PDF değil: {url}")
        return None
    return body


def _wayback(url: str) -> tuple[bytes, str] | None:
    got = _get(f"https://archive.org/wayback/available?url={quote(url, safe='')}")
    snap = {}
    if got and got[0] == 200:
        try:
            snap = json.loads(got[2]).get("archived_snapshots", {}).get("closest") or {}
        except ValueError:
            print("    Wayback yanıtı JSON değil")
    stamps = [snap["timestamp"]] if snap.get("status") == "200" else []
    stamps.append("2")  # en yakın kopyaya yönlendirir
    for stamp in stamps:
        time.sleep(WAYBACK_GAP)
        data = _get_pdf(f"https://web.archive.org/web/{stamp}id_/{url}")
        if data:
            return data, f"wayback:{stamp}"
    return None


def fetch(doc: dict) -> tuple[bytes, str] | None:
    host = urlparse(doc["url"]).hostname
    if host in _blocked_hosts:
        print(f"    {host} bu koşumda engelledi, doğrudan denenmiyor")
    else:
        data = _get_pdf(doc["url"])
        if data:
            return data, "direct"
    print("    Internet Archive deneniyor")
    return _wayback(doc["url"])


def inspect_pdf(data: bytes, expect: str | None) -> tuple[int, str, bool]:
    """Sayfa sayısı, sayfa işaretli düz metin ve 'expect' bulundu mu."""
    import pymupdf

    with pymupdf.open(stream=data, filetype="pdf") as pdf:
        pages = [page.get_text() for page in pdf]
    text = "".join(f"\n=== s.{i} ===\n{t}" for i, t in enumerate(pages, 1))
    head = " ".join(pages[:EXPECT_PAGES]).lower()
    found = expect is None or expect.lower() in " ".join(head.split())
    return len(pages), text, found


def target_path(doc: dict) -> Path:
    return FILES / doc["category"] / f"{doc['id']}.pdf"


def process(doc: dict, index: dict, force: bool, recheck: bool = False) -> str:
    target = target_path(doc)
    old = index.get(doc["id"], {})
    if target.exists() and not force and old and not recheck:
        return "var"
    print(f"  {doc['id']}")
    if recheck and target.exists() and old:
        # Yerel dosyayı indirmeden yeniden doğrula ('expect' ya da metin çıkarma değiştiyse).
        data, source = target.read_bytes(), old["source"]
    elif doc.get("manual"):
        # Giriş isteyen belge: otomatik indirilmez, elle konan dosya işlenir.
        if not target.exists():
            return f"ELLE İNDİR: {doc['manual']} -> {target.relative_to(ROOT).as_posix()}"
        data, source = target.read_bytes(), "elle"
        if not data.startswith(b"%PDF"):
            return f"BAŞARISIZ: {target.name} PDF değil"
    else:
        got = fetch(doc)
        if got is None:
            return "BAŞARISIZ"
        data, source = got
    pages, text, found = inspect_pdf(data, doc.get("expect"))
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    TEXT.mkdir(parents=True, exist_ok=True)
    (TEXT / f"{doc['id']}.txt").write_text(text, encoding="utf-8")
    index[doc["id"]] = {
        "path": target.relative_to(ROOT).as_posix(),
        "source": source,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "pages": pages,
        "text_chars": len(text),
        "expect_found": found,
        "fetched": old.get("fetched") if recheck and old else dt.date.today().isoformat(),
    }
    status = f"{source}, {pages} sayfa, {len(data) // 1024} KB"
    if not found:
        status += f", UYARI: '{doc['expect']}' ilk {EXPECT_PAGES} sayfada yok"
    if len(text.strip()) < 200 * pages:
        status += ", metni az (taranmış görüntü olabilir)"
    return status


LOCAL_RESOURCES = """\
## Kurulumda zaten olanlar (indirmeye gerek yok)

| Kaynak | Konum | Ne işe yarar |
|---|---|---|
| LTspice 26 yardımı (752 HTML sayfası) | `%LOCALAPPDATA%\\Programs\\ADI\\LTspice\\LTspiceHelp\\` | Resmî ve güncel eleman, komut, `.meas` ve seçenek belgeleri. İnternetteki XVII yardım PDF'leri eski. |
| ADI LTspice AI Reference (15 `.md`) | `%LOCALAPPDATA%\\LTspice\\reference\\` | `ReferenceDocs` / `search_ltspice_reference` ile aranır. Doğrulaması: `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md`. |
| ADI uygulama devreleri (~4200 `.asc`) | `%LOCALAPPDATA%\\LTspice\\examples\\Applications\\` | Her ADI parçasının datasheet'teki tipik uygulamasının çalışan şeması: referans tasarım. Ör. `LT8610.asc`, `LT1117-3.3.asc`. |
| Eğitim örnekleri (142) | `%LOCALAPPDATA%\\LTspice\\examples\\Educational\\` | `LoopGain*.asc`, `FRA\\` (frekans tepkisi analizi), `MonteCarlo.asc`, `SampleAndHold.asc`, `Wien.asc`, `butter.asc`, `noise.asc`, `steptemp.asc`. |
| LTspice MCP köprüsü | `%LOCALAPPDATA%\\Programs\\ADI\\LTspice\\ltspice-mcp-bridge.exe` | LTspice 26'nın yerleşik MCP sunucusu; yardımda "Third Party AI Tools" sayfası. |
"""


def write_catalog(manifest: dict, index: dict) -> None:
    lines = [
        "# Dış kaynaklar: app note, datasheet, el kitabı",
        "",
        "Bu dosya `fetch_references.py` tarafından `manifest.json`'dan üretilir; elle düzenleme, manifest'i "
        "düzenleyip betiği çalıştır.",
        "",
        "```",
        "python docs/references/fetch_references.py",
        "```",
        "",
        "PDF'ler `files/`'a, düz metinleri `text/`'e iner; ikisi de git'e girmez (üretici belgeleri: kişisel ve "
        "kurum içi kullanım, yeniden dağıtım yok). Belgede arama: `text/` altında grep (ör. `rg -i \"inductor\" "
        "docs/references/text`). Metinde `=== s.N ===` satırı N. sayfanın başıdır.",
        "",
        "Kurallar (GEMINI.md): bir app note ya da datasheet iddiasını kullanmadan önce buradaki belgeden doğrula ve "
        "sayfa numarasıyla atıf yap. Belge ile ölçüm çelişirse ölçüme güven ve ders belgesine yaz. analog.com ve "
        "st.com tarayıcı başlıkları olmayan isteği engelliyor; betik tarayıcı başlıklarıyla ister, olmazsa "
        "Internet Archive kopyasına düşer (`files/index.json` `source`).",
        "",
        LOCAL_RESOURCES,
    ]
    for key, label in manifest["categories"].items():
        docs = [d for d in manifest["documents"] if d["category"] == key]
        if not docs:
            continue
        lines += [f"## {label}", "", "| Belge | Neden | Projede | Sayfa |", "|---|---|---|---|"]
        for d in docs:
            pages = index.get(d["id"], {}).get("pages", "—")
            title = f"[{d['title']}]({d['url']})<br>{d['publisher']} · `{d['id']}`"
            if d.get("manual"):
                title += f"<br>**Elle indir:** {d['manual']}"
            lines.append(f"| {title} | {d['why']} | {d['relates']} | {pages} |")
        lines.append("")
    README.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    # Konsol cp1252 iken (çıktı boruya yönlendirildiğinde) 'İ' yazdırmak UnicodeEncodeError veriyordu (2026-10-05).
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="var olanları da yeniden indir")
    parser.add_argument("--only", nargs="+", metavar="ID", help="yalnızca bu belgeler")
    parser.add_argument("--catalog", action="store_true", help="yalnızca README.md'yi üret")
    parser.add_argument("--recheck", action="store_true", help="yerel PDF'leri indirmeden yeniden doğrula")
    args = parser.parse_args()

    manifest = load_manifest()
    ids = [d["id"] for d in manifest["documents"]]
    if len(ids) != len(set(ids)):
        print("manifest.json'da yinelenen id var", file=sys.stderr)
        return 2
    index = load_index()

    failed, manual = [], []
    if not args.catalog:
        docs = manifest["documents"]
        if args.only:
            unknown = set(args.only) - set(ids)
            if unknown:
                print(f"Bilinmeyen id: {', '.join(sorted(unknown))}", file=sys.stderr)
                return 2
            docs = [d for d in docs if d["id"] in args.only]
        if CURL is None:
            print(r"curl bulunamadı (Windows 10+ ile gelir: C:\Windows\System32\curl.exe)", file=sys.stderr)
            return 2
        for doc in docs:
            status = process(doc, index, args.force, args.recheck)
            print(f"{doc['id']:<34} {status}")
            if status.startswith("ELLE"):
                manual.append(doc["id"])
            elif status.startswith("BAŞARISIZ") or not index.get(doc["id"], {}).get("expect_found", True):
                failed.append(doc["id"])
            FILES.mkdir(parents=True, exist_ok=True)
            INDEX.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    write_catalog(manifest, index)
    print(f"\n{len(index)}/{len(ids)} belge yerelde. Katalog: {README.relative_to(ROOT.parents[1])}")
    if manual:
        print(f"Elle indirilecek: {', '.join(manual)}")
    if failed:
        print(f"Sorunlu: {', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
