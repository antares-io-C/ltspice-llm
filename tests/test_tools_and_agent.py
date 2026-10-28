"""
Araç şemaları, araç katmanı ve ajan döngüsü testleri (LTspice gerektirmez).
"""

import base64
import json
import math
import re
import sys
from pathlib import Path

import pytest

import ltspice_llm
from ltspice_llm import (
    AutonomousCircuitAgent,
    CircuitOptimizer,
    LTSpiceAdapter,
    SimulationResult,
    convert_tool_schemas,
    get_ltspice_tool_schemas,
    handle_tool_call,
)
from ltspice_llm.netlist import ltspice_visible
from ltspice_llm.parser import RawParser


@pytest.fixture
def adapter(tmp_path):
    """Gerçek LTspice çalıştırmayan ama çalışma alanı mantığını kullanan adaptör."""
    return LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws")


# ------------------------------------------------------------------ şema biçimleri
def test_generic_schema_has_all_ten_tools():
    names = [t["name"] for t in get_ltspice_tool_schemas()]
    assert names == [
        "create_or_replace_circuit", "modify_parameters", "plot_transient_waveforms", "plot_bode_diagram",
        "get_circuit_netlist", "list_circuits", "quantize_parameters", "diagnose_circuit_issue",
        "optimize_parameter", "search_ltspice_reference",
    ]


def test_openai_format():
    tools = get_ltspice_tool_schemas("openai")
    assert all(t["type"] == "function" for t in tools)
    assert tools[0]["function"]["name"] == "create_or_replace_circuit"
    assert "properties" in tools[0]["function"]["parameters"]


def test_anthropic_format():
    tools = get_ltspice_tool_schemas("anthropic")
    assert set(tools[0]) == {"name", "description", "input_schema"}
    assert tools[0]["input_schema"]["type"] == "object"


def test_gemini_format_strips_unsupported_keys_and_empty_parameters():
    (group,) = get_ltspice_tool_schemas("gemini")
    decls = {d["name"]: d for d in group["function_declarations"]}
    assert "parameters" not in decls["list_circuits"]  # parametresiz araç
    text = json.dumps(group)
    assert "additionalProperties" not in text and '"default"' not in text
    assert "parameters" in decls["modify_parameters"]


def test_conversion_does_not_mutate_the_source_schema():
    generic = get_ltspice_tool_schemas()
    snapshot = json.dumps(generic, sort_keys=True)
    convert_tool_schemas(generic, "gemini")
    convert_tool_schemas(generic, "openai")
    assert json.dumps(generic, sort_keys=True) == snapshot


def test_unknown_schema_format_is_rejected():
    with pytest.raises(ValueError, match="Bilinmeyen"):
        get_ltspice_tool_schemas("xml")


def test_unknown_tool_is_named_as_unknown_even_without_ltspice(monkeypatch, tmp_path):
    """Eskiden önce adaptör kuruluyordu: LTspice yoksa modelin yazım hatası 'LTspice bulunamadı' diye görünüyordu."""
    monkeypatch.setenv(LTSpiceAdapter.ENV_EXECUTABLE, str(tmp_path / "yok" / "LTspice.exe"))
    res = handle_tool_call("simulate_circiut", {"circuit_name": "x"})
    assert res == {"status": "ERROR", "message": "Bilinmeyen araç çağrısı: 'simulate_circiut'"}


@pytest.mark.parametrize("alias, current, args", [
    ("simulate_circuit", "create_or_replace_circuit", {"circuit_name": "x"}),       # boş netlist
    ("tune_circuit_parameters", "modify_parameters", {"circuit_name": "yok"}),      # devre yok
    ("plot_circuit_waveforms", "plot_transient_waveforms", {"circuit_name": "yok"}),  # .raw yok (.tran mesajı)
    ("create_schematic", "create_or_replace_schematic", {"circuit_name": "x"}),     # şema verisi yok
])
def test_legacy_tool_names_answer_like_the_current_ones(alias, current, args, adapter):
    """Eski araç adları geriye dönük uyumluluk için duruyor: aynı girdiye yeni adla aynı yanıt."""
    expected = handle_tool_call(current, args, adapter=adapter)
    assert expected["status"] == "ERROR" and "Bilinmeyen" not in expected["message"]
    assert handle_tool_call(alias, args, adapter=adapter) == expected


def test_every_schema_tool_reaches_its_handler(monkeypatch, tmp_path):
    """Modele sunulan her araç adı (şema) bir işleyiciye gider; hiçbiri 'bilinmeyen araç' sayılmaz."""
    monkeypatch.setenv(LTSpiceAdapter.ENV_EXECUTABLE, str(tmp_path / "yok" / "LTspice.exe"))
    for schema in get_ltspice_tool_schemas(include_extended=True):
        res = handle_tool_call(schema["name"], {})
        assert "Bilinmeyen araç" not in res.get("message", ""), schema["name"]


def test_version_matches_pyproject_and_changelog():
    """Sürüm tek yerde tanımlı olmalı ve CHANGELOG'un en üstteki sürümüyle aynı kalmalı."""
    root = Path(__file__).parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert 'attr = "ltspice_llm.__version__"' in pyproject  # tek doğruluk kaynağı
    assert 'version = "' not in pyproject.split("[project]")[1].split("[project.optional")[0]

    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    latest = re.search(r"^## (\d+\.\d+\.\d+)", changelog, re.MULTILINE)
    assert latest, "CHANGELOG'da sürüm başlığı bulunamadı"
    assert ltspice_llm.__version__ == latest.group(1)


def _required_imports(source: str):
    """Kaynakta ``except ImportError`` ile korunmayan mutlak importların üst modül adları. O handler'ın içindeki
    import da yedektir (``from spicelib ...`` olmazsa ``from PyLTSpice ...``) ve zorunlu sayılmaz."""
    import ast

    def guarded(node):
        return any(
            isinstance(h.type, ast.Name) and h.type.id in ("ImportError", "ModuleNotFoundError")
            for h in getattr(node, "handlers", [])
        )

    found = set()

    def walk(node, inside_guard):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                found.update(a.name.split(".")[0] for a in child.names if not inside_guard)
            elif isinstance(child, ast.ImportFrom) and child.level == 0 and not inside_guard:
                found.add(child.module.split(".")[0])
            if isinstance(child, ast.Try):
                for stmt in child.body + [s for h in child.handlers for s in h.body]:
                    walk_stmt(stmt, inside_guard or guarded(child))
                for stmt in child.orelse + child.finalbody:
                    walk_stmt(stmt, inside_guard)
            else:
                walk(child, inside_guard)

    def walk_stmt(stmt, inside_guard):
        wrapper = ast.Module(body=[stmt], type_ignores=[])
        walk(wrapper, inside_guard)

    walk(ast.parse(source), False)
    return found


def test_every_required_third_party_import_is_a_declared_dependency():
    """Korumasız (zorunlu) her üçüncü taraf import pyproject bağımlılığı olmalı. Bağlam: agent.py scipy kullanıyordu
    ama pyproject listelemiyordu. Eski kodda import ``except ImportError`` ile korunduğundan bu test o hali tek başına
    yakalamaz (yeşildi, ölçüldü); sessiz yedeği ``..._raises_without_scipy_...`` yakalar. Bu test, yedek kalkınca
    bağımlılığın bildirilmesini sabitler."""
    import importlib.metadata

    root = Path(__file__).parent.parent
    pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")  # tomllib 3.11'de geldi, proje >=3.10
    block = re.search(r"^dependencies = \[(.*?)^\]", pyproject, re.MULTILINE | re.DOTALL).group(1)
    declared = {d.lower().replace("_", "-") for d in re.findall(r'^\s*"([A-Za-z0-9_.-]+)', block, re.MULTILINE)}
    dists = importlib.metadata.packages_distributions()
    missing = {}
    for path in sorted((root / "ltspice_llm").glob("*.py")):
        for module in _required_imports(path.read_text(encoding="utf-8")):
            if module in sys.stdlib_module_names or module == "ltspice_llm":
                continue
            names = {n.lower().replace("_", "-") for n in dists.get(module, [module])}
            if not names & declared:
                missing.setdefault(module, []).append(path.name)
    assert missing == {}


def test_optimize_multi_parameters_raises_without_scipy_instead_of_a_silent_single_run(monkeypatch):
    """Eski hata: scipy yokken başlangıç noktasını bir kez koşup "yakınsamadı" diye dönüyordu (hata görünmüyordu)."""
    monkeypatch.setitem(sys.modules, "scipy", None)
    monkeypatch.setitem(sys.modules, "scipy.optimize", None)
    with pytest.raises(ImportError):
        _tau_opt(9.4e-6)


# ------------------------------------------------------------------- argüman toleransı
class _Recorder:
    def __init__(self, workspace: Path):
        self.workspace_dir = workspace
        self.kw = None

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        self.kw = dict(content=netlist_content_or_path, name=circuit_name, parameters=parameters, **kw)
        return SimulationResult(success=True, netlist_path=self.workspace_dir / "x.net")


WAVE_NETLIST = "* w\nV1 a 0 SINE(0 1 1k)\nR1 a 0 1k\n.tran 5m\n.wave C:\\out.wav 16 44.1k V(a)\n.end\n"
PLAIN_SOURCE = "* t\nV1 in 0 DC 5\nR1 in out 1k\nR2 out 0 1k\n.op\n.end\n"


def test_untrusted_netlist_that_writes_a_file_is_refused_unless_allowed(tmp_path):
    """ADI SIMULATION-COMMANDS-REFERENCE '.WAVE': istenen yola .wav yazar (örnek '.wave C:\\output.wav'). Eskiden LLM'in
    netlisti olduğu gibi LTspice'a gidiyordu (inceleme 2026-10-11); kural tests/test_netlist_policy.py'de."""
    rec = _Recorder(tmp_path)
    res = handle_tool_call("create_or_replace_circuit", {"circuit_name": "w", "netlist_code": WAVE_NETLIST}, adapter=rec)
    assert res["status"] == "ERROR" and len(res["file_access"]) == 1
    assert res["file_access"][0].endswith("dosya yazar (.wave).") and "allow_file_access=True" in res["message"]
    assert rec.kw is None  # simüle edilmedi
    ok = handle_tool_call("create_or_replace_circuit", {"circuit_name": "w", "netlist_code": WAVE_NETLIST},
                          adapter=rec, allow_file_access=True)
    assert ok["status"] == "SUCCESS" and rec.kw["content"] == WAVE_NETLIST


def test_agent_passes_its_file_access_decision_and_it_does_not_leak(tmp_path):
    """İzin çağrı boyunca geçerlidir: izinli ajanın çağrısından sonra izinsiz ajanın aynı netlisti yine reddedilir."""
    rec = _Recorder(tmp_path)
    for allowed, status in ((True, "SUCCESS"), (False, "ERROR")):
        agent = AutonomousCircuitAgent(adapter=rec, allow_file_access=allowed)
        res = agent.execute_tool("create_or_replace_circuit", {"circuit_name": "w", "netlist_code": WAVE_NETLIST})
        assert res["status"] == status


def test_parameter_value_cannot_smuggle_a_file_reference(tmp_path):
    """Değer doğrulaması satır sonunu ve ';'ü engeller ama boşluğu değil: 'DC 5' alanına '0 wavefile=C:\\x.wav'
    yazılırsa LTspice kaynağı o dosyadan okur (CIRCUIT-ELEMENTS-REFERENCE 'wavefile='). Sonuç netlist denetlenir."""
    (tmp_path / "c.net").write_text(PLAIN_SOURCE, encoding="utf-8")
    rec = _Recorder(tmp_path)
    res = handle_tool_call("modify_parameters", {"circuit_name": "c", "parameters": {"V1": "0 wavefile=C:\\x.wav"}},
                           adapter=rec)
    assert res["status"] == "ERROR" and len(res["file_access"]) == 1
    assert res["file_access"][0].endswith("(C:\\x.wav).") and rec.kw is None


def test_untrusted_schematic_that_reads_outside_the_workspace_is_refused(tmp_path):
    asc = "Version 4\nSHEET 1 880 680\nTEXT 0 0 Left 2 !.op\nTEXT 0 40 Left 2 !.include C:\\secret.lib\n"
    rec = _Recorder(tmp_path)
    res = handle_tool_call("create_or_replace_schematic", {"circuit_name": "s", "asc_content": asc}, adapter=rec)
    assert res["status"] == "ERROR" and len(res["file_access"]) == 1
    assert res["file_access"][0].endswith("(C:\\secret.lib).")
    assert not (tmp_path / "s.asc").exists() and rec.kw is None  # yazılmadı, LTspice'a gitmedi


def test_openai_style_json_string_arguments_are_parsed(tmp_path):
    """OpenAI, araç argümanlarını sözlük değil JSON metni olarak gönderir."""
    ad = _Recorder(tmp_path)
    args = json.dumps({"circuit_name": "c", "netlist_code": "* t\nR1 a 0 1k\n.end\n", "parameters": {"R1": "2k"}})
    res = handle_tool_call("create_or_replace_circuit", args, adapter=ad)
    assert res["status"] == "SUCCESS"
    assert ad.kw["name"] == "c" and ad.kw["parameters"] == {"R1": "2k"} and ad.kw["treat_as_text"] is True


def test_parameters_given_as_json_string_are_accepted(tmp_path):
    ad = _Recorder(tmp_path)
    handle_tool_call(
        "create_or_replace_circuit",
        {"circuit_name": "c", "netlist_code": "* t\n.end\n", "parameters": '{"R1": "2k"}'},
        adapter=ad,
    )
    assert ad.kw["parameters"] == {"R1": "2k"}


def test_quantize_tool_works_without_ltspice(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError("LTspice yok")

    monkeypatch.setattr("ltspice_llm.tools.LTSpiceAdapter", boom)
    res = handle_tool_call("quantize_parameters", {"parameters": {"R": "4.67k"}, "series": "E12"})
    assert res["status"] == "SUCCESS"
    assert res["quantized_parameters"]["R"] == "4.7k"
    assert handle_tool_call("list_circuits", {})["status"] == "ERROR"  # LTspice gerektiren araç hâlâ hata döner


def test_signals_given_as_json_array_string(monkeypatch, adapter):
    (adapter.workspace_dir / "c.raw").write_text("x")
    seen = {}

    def fake_plot(raw_path, signals, title):
        seen["signals"] = signals
        return Path(raw_path).with_suffix(".png")

    monkeypatch.setattr("ltspice_llm.tools.WaveformVisualizer.plot_traces", staticmethod(fake_plot))
    handle_tool_call("plot_transient_waveforms", {"circuit_name": "c", "signals": '["V(in)", "V(out)"]'},
                     adapter=adapter)
    assert seen["signals"] == ["V(in)", "V(out)"]


# ----------------------------------------------------------------- çizim doğrulamaları
def test_plot_tools_reject_the_wrong_analysis_type(monkeypatch, adapter):
    (adapter.workspace_dir / "c.raw").write_text("x")
    monkeypatch.setattr(RawParser, "analysis_kind", classmethod(lambda cls, p: "tran"))
    res = handle_tool_call("plot_bode_diagram", {"circuit_name": "c"}, adapter=adapter)
    assert res["status"] == "ERROR" and ".ac" in res["message"]

    monkeypatch.setattr(RawParser, "analysis_kind", classmethod(lambda cls, p: "ac"))
    res = handle_tool_call("plot_transient_waveforms", {"circuit_name": "c"}, adapter=adapter)
    assert res["status"] == "ERROR" and "plot_bode_diagram" in res["message"]


def test_plot_tools_can_return_base64(monkeypatch, adapter):
    raw = adapter.workspace_dir / "c.raw"
    raw.write_text("x")
    monkeypatch.setattr(RawParser, "analysis_kind", classmethod(lambda cls, p: "ac"))

    def fake_bode(raw_path, out_node, in_node, title):
        png = Path(raw_path).with_name("c_bode.png")
        png.write_bytes(b"\x89PNG-fake")
        return png

    monkeypatch.setattr("ltspice_llm.tools.WaveformVisualizer.plot_bode", staticmethod(fake_bode))
    res = handle_tool_call("plot_bode_diagram", {"circuit_name": "c", "include_image_base64": True}, adapter=adapter)
    assert res["status"] == "SUCCESS"
    assert base64.b64decode(res["image_base64"]) == b"\x89PNG-fake"
    res = handle_tool_call("plot_bode_diagram", {"circuit_name": "c"}, adapter=adapter)
    assert "image_base64" not in res


# ------------------------------------------------------------------------ optimizer
class _FakeSim:
    """metric = f(param); oluşturulan/silinen devre adlarını kaydeder."""

    def __init__(self, f):
        self.f, self.names, self.removed = f, [], []
        self.workspace_dir = Path(".")

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        self.names.append(circuit_name)
        (p,) = parameters.values()
        return SimulationResult(success=True, netlist_path=Path("x.net"), measurements={"m": self.f(p)})

    def remove_circuit(self, name):
        self.removed.append(name)
        return []


def _opt(sim, **kw):
    args = dict(base_netlist="n", circuit_name="c", param_name="P", target_meas_name="m")
    args.update(kw)
    return CircuitOptimizer(adapter=sim).optimize_single_parameter(**args)


def test_optimizer_cleans_up_intermediate_circuits_by_default():
    sim = _FakeSim(lambda p: p)
    res = _opt(sim, target_value=30.0, min_param=0.0, max_param=100.0, max_iterations=20)
    assert res.converged is True and res.message == ""
    assert sim.names and sorted(sim.removed) == sorted(sim.names)


def test_optimizer_can_keep_intermediate_circuits():
    sim = _FakeSim(lambda p: p)
    _opt(sim, target_value=30.0, min_param=0.0, max_param=100.0, cleanup_intermediate=False)
    assert sim.removed == []


def test_optimizer_uses_unique_circuit_names_per_run():
    a, b = _FakeSim(lambda p: p), _FakeSim(lambda p: p)
    _opt(a, target_value=30.0, min_param=0.0, max_param=100.0)
    _opt(b, target_value=30.0, min_param=0.0, max_param=100.0)
    assert not set(a.names) & set(b.names)  # eşzamanlı optimizasyonlar dosya ezmez
    assert all(n.startswith("c_opt_") for n in a.names + b.names)


def test_optimizer_explains_insensitive_metric():
    """Parametre netlistte kullanılmıyorsa ölçüm iki uçta da aynıdır; neden açıklanmalı."""
    res = _opt(_FakeSim(lambda p: 5.0), target_value=3.0, min_param=1.0, max_param=100.0)
    assert res.converged is False
    assert "duyarsız" in res.message and "'P'" in res.message


def test_optimizer_explains_target_outside_range():
    res = _opt(_FakeSim(lambda p: p), target_value=500.0, min_param=1.0, max_param=100.0)
    assert res.converged is False and "genişletin" in res.message


def test_optimizer_measures_a_microsecond_target_relatively():
    """Eski hata (çok parametreli yolda düzeltilmiş, tek parametrelide kalmıştı): |hedef| <= 1e-5 iken tolerans mutlak
    uygulanıyordu; 5 µs hedefi 1 µs'de, 2 simülasyonda "yakınsadı" sayıldı (ölçüldü, 2026-10-11). Elle: τ = R·1 nF,
    R 1k..10k -> τ 1..10 µs; 5 µs hedefi R = 5k'dır, tolerans %1 (τ R ile doğrusal: bağıl hatalar aynı)."""
    res = _opt(_FakeSim(lambda r: r * 1e-9), target_value=5e-6, min_param=1e3, max_param=1e4, tolerance=0.01)
    assert res.converged is True
    assert abs(res.best_value - 5e-6) <= 0.01 * 5e-6
    assert res.best_parameters["P"] == pytest.approx(5e3, rel=0.01)


def test_optimizer_reports_an_out_of_range_microsecond_target():
    """Elle: R 1k..10k -> τ 1..10 µs; 50 ns aralığın dışında (en yakın uç 1 µs, bağıl hata 19). Eski hata: mutlak
    tolerans 0.01 ile "yakınsadı" sayıldı ve "aralığı genişletin" mesajı silindi."""
    res = _opt(_FakeSim(lambda r: r * 1e-9), target_value=50e-9, min_param=1e3, max_param=1e4, tolerance=0.01)
    assert res.converged is False and "genişletin" in res.message
    assert res.total_iterations == 2  # uçlar dışında simülasyon yapılmaz


def test_optimizer_explains_iteration_exhaustion():
    res = _opt(_FakeSim(lambda p: p), target_value=30.0, min_param=0.0, max_param=100.0,
               max_iterations=3, tolerance=1e-9)
    assert res.converged is False and "tolerans sağlanamadı" in res.message


def test_optimize_parameter_tool_end_to_end_with_fake_adapter(tmp_path):
    class Sim(_FakeSim):
        def __init__(self):
            super().__init__(lambda p: p * 2.0)
            self.workspace_dir = tmp_path

    (tmp_path / "c.net").write_text("* t\n.param P=1\n.end\n", encoding="utf-8")
    res = handle_tool_call(
        "optimize_parameter",
        {"circuit_name": "c", "param_name": "P", "target_meas_name": "m", "target_value": 40,
         "min_value": 1, "max_value": 100},
        adapter=Sim(),
    )
    assert res["status"] == "SUCCESS" and res["converged"] is True
    assert res["best_parameters"]["P"] == pytest.approx(20.0, rel=0.02)
    assert "modify_parameters" in res["hint"]
    assert res["history"][0]["iteration"] == 1


def test_optimize_parameter_tool_validates_arguments(tmp_path):
    (tmp_path / "c.net").write_text("* t\n.end\n", encoding="utf-8")
    ad = _FakeSim(lambda p: p)
    ad.workspace_dir = tmp_path
    missing = handle_tool_call("optimize_parameter", {"circuit_name": "c", "param_name": "P"}, adapter=ad)
    assert missing["status"] == "ERROR" and "target_value" in missing["message"]
    bad_range = handle_tool_call(
        "optimize_parameter",
        {"circuit_name": "c", "param_name": "P", "target_meas_name": "m", "target_value": 1,
         "min_value": 10, "max_value": 1},
        adapter=ad,
    )
    assert bad_range["status"] == "ERROR" and "min_value" in bad_range["message"]
    no_file = handle_tool_call(
        "optimize_parameter",
        {"circuit_name": "zzz", "param_name": "P", "target_meas_name": "m", "target_value": 1,
         "min_value": 1, "max_value": 10},
        adapter=ad,
    )
    assert no_file["status"] == "ERROR"


# ---------------------------------------------------------------------------- ajan
def test_agent_message_helpers_build_provider_neutral_history(adapter):
    agent = AutonomousCircuitAgent(adapter=adapter)
    agent.add_user_message("merhaba")
    agent.add_assistant_message("çağırıyorum", [{"id": "1", "name": "list_circuits", "arguments": {}}])
    agent.add_tool_result("list_circuits", {"status": "SUCCESS"}, "1")
    hist = agent.get_history()
    assert [m["role"] for m in hist] == ["system", "user", "assistant", "tool"]
    assert hist[2]["tool_calls"][0]["name"] == "list_circuits"
    assert json.loads(hist[3]["content"]) == {"status": "SUCCESS"} and hist[3]["tool_call_id"] == "1"


def test_agent_does_not_pretend_to_pick_a_provider(adapter):
    """Ajan hiçbir sağlayıcıya bağlanmaz (circuit_agent modül notu). Eski varsayılan model_name='gemini-2.5-flash'
    tersini düşündürüyordu ve hiçbir yerde kullanılmıyordu (inceleme 2026-10-11)."""
    agent = AutonomousCircuitAgent(adapter=adapter)
    assert agent.model_name is None and agent.api_key is None and agent.base_url is None


def test_plot_image_is_kept_out_of_the_text_history(adapter):
    """Eskiden include_image_base64 sonucu geçmişe JSON metni olarak giriyordu: basit RC grafiği 72.524 karakter
    (ölçüldü, inceleme 2026-10-11) metin token'ı olarak gider, model görseli göremez. Görsel artık mesajın 'images'
    alanında durur; sağlayıcı dönüştürücüleri onu görsel bloğuna çevirir (ltspice_llm.providers)."""
    import base64

    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "rc.raw").write_bytes((DATA / "ref26_flow_rc.raw").read_bytes())
    agent = AutonomousCircuitAgent(adapter=adapter)
    res = agent.execute_tool("plot_transient_waveforms",
                             {"circuit_name": "rc", "signals": ["V(out)"], "include_image_base64": True})
    assert res["status"] == "SUCCESS"
    agent.add_tool_result("plot_transient_waveforms", res, "p1")
    message = agent.get_history()[-1]
    assert "image_base64" not in message["content"] and len(message["content"]) < 1000
    assert json.loads(message["content"])["image_attached"] is True
    (image,) = message["images"]
    assert image["media_type"] == "image/png" and base64.b64decode(image["data"])[:8] == b"\x89PNG\r\n\x1a\n"
    assert res["image_base64"] == image["data"]  # araç sonucu değişmez, yalnız geçmiş ayrıştırır


def test_get_history_returns_an_independent_copy(adapter):
    agent = AutonomousCircuitAgent(adapter=adapter)
    agent.get_history()[0]["content"] = "bozuldu"
    assert agent.conversation_history[0]["content"] != "bozuldu"


def test_execute_tool_is_recorded_in_tool_log(adapter):
    agent = AutonomousCircuitAgent(adapter=adapter)
    agent.execute_tool("list_circuits", {})
    agent.execute_tool("nope", {})
    assert [(e["tool"], e["status"]) for e in agent.tool_log] == [("list_circuits", "SUCCESS"), ("nope", "ERROR")]
    assert agent.tool_log[0]["elapsed_seconds"] >= 0
    agent.reset()
    assert agent.tool_log == [] and len(agent.get_history()) == 1


def test_run_loop_executes_tool_calls_until_model_answers(adapter):
    agent = AutonomousCircuitAgent(adapter=adapter)
    seen = []

    def llm(messages, tools):
        seen.append(([m["role"] for m in messages], len(tools)))
        if len(seen) == 1:
            return {"content": None, "tool_calls": [{"id": "a", "name": "list_circuits", "arguments": "{}"}]}
        return {"content": "bitti", "tool_calls": []}

    out = agent.run("devreleri listele", llm)
    assert out == {"finished": True, "steps": 2, "final_message": "bitti"}
    assert seen[0] == (["system", "user"], 10)
    assert seen[1][0] == ["system", "user", "assistant", "tool"]
    assert [m["role"] for m in agent.get_history()] == ["system", "user", "assistant", "tool", "assistant"]
    assert agent.tool_log[0]["tool"] == "list_circuits"


def test_agent_can_offer_the_extended_tools_to_the_model(adapter):
    """Gemini (2026-10-06): ajan şemaları hep varsayılanla alıyordu; Monte Carlo, SOA/derating, iz parazitikleri,
    hot-plug ve şema çizimi araçları modele gösterilemiyordu. Varsayılan temel 10 araç kalır (yukarıdaki test)."""
    agent = AutonomousCircuitAgent(adapter=adapter, include_extended=True)
    seen = []
    out = agent.run("x", lambda m, tools: seen.append({t["name"] for t in tools}) or {"content": "ok", "tool_calls": []})
    assert out["finished"] is True and len(seen[0]) == 16
    assert {"simulate_monte_carlo", "check_design_safety", "calculate_trace_parasitics", "analyze_hotplug_transient",
            "create_or_replace_schematic", "optimize_parameters", "list_circuits"} <= seen[0]


def test_run_loop_stops_at_max_steps(adapter):
    agent = AutonomousCircuitAgent(adapter=adapter)
    out = agent.run(
        "sonsuz",
        lambda m, t: {"content": "düşünüyorum", "tool_calls": [{"id": "x", "name": "list_circuits", "arguments": {}}]},
        max_steps=3,
    )
    assert out["finished"] is False and out["steps"] == 3 and out["final_message"] == "düşünüyorum"
    assert len(agent.tool_log) == 3


def test_run_design_step_logs_instead_of_printing(adapter, caplog, capsys):
    agent = AutonomousCircuitAgent(adapter=adapter)
    with caplog.at_level("INFO", logger="ltspice_llm.circuit_agent"):
        res = agent.run_design_step("list_circuits", {})
    assert res["status"] == "SUCCESS"
    assert capsys.readouterr().out == ""
    assert any("list_circuits" in r.getMessage() for r in caplog.records)


def test_list_circuits_method_lists_the_workspace_netlists(adapter):
    """Araç şeması: kayıtlı devreler ve ilişkili çıktı dosyaları (.raw, .log, .png); devre = .net dosyası."""
    for name in ("rc.net", "rc.raw", "rc.log", "rc_bode.png", "bare.net", "notes.txt"):
        (adapter.workspace_dir / name).write_text("x", encoding="utf-8")
    res = AutonomousCircuitAgent(adapter=adapter).list_circuits()
    assert res["status"] == "SUCCESS" and res["total_circuits"] == 2
    flags = {c["name"]: (c["has_raw"], c["has_log"], c["has_plot"]) for c in res["circuits"]}
    assert flags == {"rc": (True, True, True), "bare": (False, False, False)}


@pytest.mark.parametrize("code", ["", "   \n"])
def test_empty_netlist_is_an_argument_error_and_never_simulated(tmp_path, code):
    """docs/KILAVUZ.md: ERROR = araç çağrısı yürütülemedi (eksik argüman); boş netlist LTspice'a gönderilmez."""
    rec = _Recorder(tmp_path)
    res = handle_tool_call("create_or_replace_circuit", {"circuit_name": "x", "netlist_code": code}, adapter=rec)
    assert res["status"] == "ERROR" and "boş olamaz" in res["message"]
    assert rec.kw is None


def test_optimizer_uses_absolute_tolerance_when_the_target_is_zero():
    """Hedef 0'da bağıl hata tanımsız, tolerans mutlak uygulanır. Elle hesap: f(p) = p - 37, kökü 37; ikiye bölme
    noktaları (50, 25, 37.5, ...) köke denk gelmez, 37.5'teki 0.5 hata 0.05 toleransını aşar."""
    res = _opt(_FakeSim(lambda p: p - 37.0), target_value=0.0, min_param=0.0, max_param=100.0, tolerance=0.05)
    assert res.converged is True
    assert abs(res.best_value) <= 0.05


def test_optimize_multi_parameters_two_variables():
    """Çok değişkenli optimizasyon testi: m1 = R + C, m2 = 2*R - C. Hedef m1=30, m2=0 -> R=10, C=20."""
    class MultiSim:
        def __init__(self):
            self.names = []
            self.removed = []
            self.workspace_dir = Path(".")

        def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
            self.names.append(circuit_name)
            r = parameters.get("R", 1.0)
            c = parameters.get("C", 1.0)
            m1 = r + c
            m2 = 2.0 * r - c
            return SimulationResult(success=True, netlist_path=Path("x.net"), measurements={"m1": m1, "m2": m2})

        def remove_circuit(self, name):
            self.removed.append(name)
            return []

    sim = MultiSim()
    opt = CircuitOptimizer(adapter=sim)
    res = opt.optimize_multi_parameters(
        base_netlist="* multi\n.param R=1 C=1\n.end\n",
        circuit_name="multi_test",
        param_bounds={"R": (1.0, 50.0), "C": (1.0, 50.0)},
        target_metrics={"m1": 30.0, "m2": 0.0},
        tolerance=0.05,
        max_iterations=80,
    )
    assert res.converged is True
    assert math.isclose(res.best_parameters["R"], 10.0, rel_tol=0.05)
    assert math.isclose(res.best_parameters["C"], 20.0, rel_tol=0.05)
    assert sorted(sim.removed) == sorted(sim.names)
    llm_dict = res.to_llm_dict()
    assert llm_dict["status"] == "SUCCESS"
    assert "target_metrics" in llm_dict


class _TauSim:
    """τ = R·C ölçen sahte LTspice sınırı (R = 4.7k sabit, C taranır)."""

    workspace_dir = Path(".")

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        return SimulationResult(success=True, netlist_path=Path("x.net"), measurements={"tau": 4.7e3 * parameters["C"]})

    def remove_circuit(self, name):
        return []


def _tau_opt(target_tau):
    return CircuitOptimizer(adapter=_TauSim()).optimize_multi_parameters(
        base_netlist="* rc\n.end\n", circuit_name="tau", param_bounds={"C": (1e-9, 100e-9)},
        target_metrics={"tau": target_tau}, tolerance=0.02, max_iterations=30,
    )


def test_optimize_multi_parameters_does_not_converge_on_an_unreachable_microsecond_target():
    """Eski hata: |hedef| <= 1e-5 iken hata mutlak alınıyordu; 2 µs hedef 13.5 µs'de "converged" sayıldı (ölçüldü).
    Elle: R = 4.7k, C >= 1n -> τ >= 4.7 µs, 2 µs hedefine bağıl hata >= (4.7 - 2) / 2 = 1.35."""
    res = _tau_opt(2e-6)
    assert res.converged is False
    assert res.final_error >= 1.35


class _FnSim:
    """Ölçümleri verilen fonksiyondan üreten sahte LTspice sınırı; simülasyon sayısını tutar."""

    workspace_dir = Path(".")

    def __init__(self, fn):
        self.fn, self.calls, self.seen = fn, 0, []

    def run_netlist(self, netlist_content_or_path, circuit_name, parameters=None, **kw):
        self.calls += 1
        self.seen.append(dict(parameters))
        return SimulationResult(success=True, netlist_path=Path("x.net"), measurements=self.fn(parameters))

    def remove_circuit(self, name):
        return []


def test_optimize_parameters_tool_reaches_two_targets(tmp_path):
    """Çok parametreli optimizasyon yalnızca Python API'siydi; artık bir araç (inceleme 2026-10-11). Elle: m1 = R + C,
    m2 = 2R - C; hedef m1 = 30, m2 = 0 -> R = 10, C = 20."""
    sim = _FnSim(lambda p: {"m1": p["R"] + p["C"], "m2": 2 * p["R"] - p["C"]})
    sim.workspace_dir = tmp_path
    (tmp_path / "c.net").write_text("* t\n.param R=1 C=1\n.end\n", encoding="utf-8")
    res = handle_tool_call("optimize_parameters", {
        "circuit_name": "c", "param_bounds": {"R": [1, 50], "C": [1, 50]}, "target_metrics": {"m1": 30, "m2": 0},
        "tolerance": 0.05, "max_iterations": 80}, adapter=sim)
    assert res["status"] == "SUCCESS" and res["converged"] is True
    assert res["best_parameters"]["R"] == pytest.approx(10.0, rel=0.05)
    assert res["best_parameters"]["C"] == pytest.approx(20.0, rel=0.05)
    assert res["hint"].startswith("En iyi değerleri devreye kalıcı uygulamak için modify_parameters")
    assert sim.calls <= 80


@pytest.mark.parametrize("args,start", [
    ({"param_bounds": {"R": [5, 1]}, "target_metrics": {"m": 1}}, "'R' için alt sınır"),
    ({"param_bounds": {"R": "x"}, "target_metrics": {"m": 1}}, "param_bounds:"),
    ({"param_bounds": {}, "target_metrics": {"m": 1}}, "param_bounds en az"),
    ({"param_bounds": {"R": [1, 5]}, "target_metrics": {"m": "abc"}}, "target_metrics:"),
    ({"param_bounds": {"R": [1, 5]}, "target_metrics": {}}, "target_metrics en az"),
])
def test_optimize_parameters_tool_validates_arguments(tmp_path, args, start):
    sim = _FnSim(lambda p: {"m": 1.0})
    sim.workspace_dir = tmp_path
    (tmp_path / "c.net").write_text("* t\n.end\n", encoding="utf-8")
    res = handle_tool_call("optimize_parameters", {"circuit_name": "c", **args}, adapter=sim)
    assert res["status"] == "ERROR" and res["message"].startswith(start) and sim.calls == 0


def test_optimize_multi_parameters_explores_a_log_range_centred_on_one():
    """Eski hata: 0.1..10 log aralığında başlangıç log10(1) = 0 olduğundan scipy'nin simpleksi 0.00025 adımla kuruldu;
    2 simülasyonda durup p = 1.0006'da kaldı (ölçüldü). Elle: m = p, hedef 5 -> p = 5."""
    sim = _FnSim(lambda p: {"m": p["p"]})
    res = CircuitOptimizer(adapter=sim).optimize_multi_parameters(
        "* x\n.end\n", "logc", {"p": (0.1, 10.0)}, {"m": 5.0}, tolerance=0.02, max_iterations=35,
    )
    assert res.converged is True
    assert math.isclose(res.best_parameters["p"], 5.0, rel_tol=0.02)
    assert sim.calls <= 35  # bütçe simülasyon sayısıdır


def test_optimize_multi_parameters_reaches_a_tight_tolerance():
    """Elle: m = p, hedef 5 -> p = 5; tolerans %0.5, bütçe 40 simülasyon."""
    sim = _FnSim(lambda p: {"m": p["p"]})
    res = CircuitOptimizer(adapter=sim).optimize_multi_parameters(
        "* x\n.end\n", "tight", {"p": (0.1, 10.0)}, {"m": 5.0}, tolerance=0.005, max_iterations=40,
    )
    assert res.converged is True
    assert math.isclose(res.best_parameters["p"], 5.0, rel_tol=0.005)
    assert sim.calls <= 40


def test_optimize_multi_parameters_starts_at_the_given_initial_point():
    """İlk simülasyon initial_params'ta koşar: R log ekseninde (100..10k), G doğrusal (1..5, oran < 10)."""
    sim = _FnSim(lambda p: {"m": p["R"] * p["G"]})
    CircuitOptimizer(adapter=sim).optimize_multi_parameters(
        "* x\n.end\n", "init", {"R": (100.0, 10e3), "G": (1.0, 5.0)}, {"m": 1000.0}, max_iterations=5,
        initial_params={"R": 470.0, "G": 2.0},
    )
    assert sim.seen[0]["R"] == pytest.approx(470.0, rel=1e-9)
    assert sim.seen[0]["G"] == pytest.approx(2.0, rel=1e-9)
    # G tek dekad (oran 5 < 10) -> doğrusal eksen. Başlangıç simpleksinin G köşesi z'de +0.5 rad: t0 = (2-1)/(5-1),
    # G = 1 + 4 * (1 + sin(asin(2 t0 - 1) + 0.5)) / 2 = 2.95281 (log eksende 2.97 olurdu)
    assert sim.seen[2]["G"] == pytest.approx(2.95281, rel=1e-5)
    assert sim.calls <= 5


def test_optimize_multi_parameters_converged_means_the_best_point_meets_every_target():
    """m1 = m2 = p, hedefler 1.0 ve 1.035, tolerans %2. Elle: |p - 1| <= 0.02 ve |p - 1.035| / 1.035 <= 0.02 ->
    p in [1.0143, 1.02]. Eski hata: optimizer 6 simülasyonda durup ortalama hatası en küçük ama uygun olmayan p = 1.0'ı
    (e2 = %3.4) seçti; ayrıca "converged" herhangi bir adımdan, en iyi nokta ortalamadan seçiliyordu."""
    sim = _FnSim(lambda p: {"m1": p["p"], "m2": p["p"]})
    res = CircuitOptimizer(adapter=sim).optimize_multi_parameters(
        "* x\n.end\n", "pair", {"p": (0.5, 1.5)}, {"m1": 1.0, "m2": 1.035}, tolerance=0.02, max_iterations=40,
    )
    assert res.converged is True
    assert 1.0143 <= res.best_parameters["p"] <= 1.02
    assert sim.calls <= 40


def test_optimize_multi_parameters_finds_a_microsecond_target():
    """Elle: τ = 9.4 µs, R = 4.7k -> C = 9.4µ / 4.7k = 2 nF."""
    res = _tau_opt(9.4e-6)
    assert res.converged is True
    assert math.isclose(res.best_parameters["C"], 2e-9, rel_tol=0.02)


# ------------------------------------------------------------------ genişletilmiş araçlar
def test_extended_tool_schemas():
    tools = get_ltspice_tool_schemas(include_extended=True)
    assert len(tools) == 16
    extended_names = {
        "create_or_replace_schematic",
        "calculate_trace_parasitics",
        "analyze_hotplug_transient",
        "check_design_safety",
        "simulate_monte_carlo",
        "optimize_parameters",  # inceleme 2026-10-11: çok parametreli optimizasyon araç oldu
    }
    tool_names = {t["name"] for t in tools}
    assert extended_names.issubset(tool_names)

    # Sağlayıcı dönüşümleri genişletilmiş araçlarda da çalışmalı
    openai_tools = get_ltspice_tool_schemas("openai", include_extended=True)
    assert len(openai_tools) == 16
    anthropic_tools = get_ltspice_tool_schemas("anthropic", include_extended=True)
    assert len(anthropic_tools) == 16
    gemini_tools = get_ltspice_tool_schemas("gemini", include_extended=True)
    assert len(gemini_tools[0]["function_declarations"]) == 16


def test_tool_calculate_trace_parasitics():
    # Elle (MT-094 Denk. 3-4, Bl.12 s.7; W 0.254, H 1.6, T 35 µm, er varsayılan 4.0, 50 mm):
    # Z0 = 87/sqrt(5.41) * ln(9.568/0.2382) = 138.136 Ohm, C = 1.93206 pF, L = Z0^2 C = 36.8669 nH,
    # R = 1.724e-8 * 0.05 / (0.254e-3 * 35e-6) = 0.0969629 Ohm
    res = handle_tool_call(
        "calculate_trace_parasitics",
        {"length_mm": 50.0, "width_mm": 0.254, "height_mm": 1.6, "copper_thickness_um": 35.0},
    )
    assert res["status"] == "SUCCESS"
    assert res["resistance_ohm"] == pytest.approx(0.0969629, rel=1e-5)
    assert res["inductance_nh"] == pytest.approx(36.8669, rel=1e-5)
    assert res["capacitance_pf"] == pytest.approx(1.93206, rel=1e-5)
    assert res["characteristic_impedance_ohm"] == pytest.approx(138.136, rel=1e-5)
    # MT-094 Denk. 5: 1.017 * sqrt(0.475 * 4 + 0.67) ns/ft * 50 / 304.8 = 267.450 ps
    assert res["propagation_delay_ps"] == pytest.approx(267.450, rel=1e-5)
    assert "subckt" in res["spice_subcircuit"].lower()
    # Varsayılan dışı girdiler de hesaba girer. Elle (er 4.4, H 0.8, T 70 µm): Z0 = 103.330 Ohm, R = 0.0484814 Ohm
    other = handle_tool_call(
        "calculate_trace_parasitics",
        {"length_mm": 50.0, "width_mm": 0.254, "height_mm": 0.8, "copper_thickness_um": 70.0,
         "relative_permittivity": 4.4},
    )
    assert other["characteristic_impedance_ohm"] == pytest.approx(103.330, rel=1e-5)
    assert other["resistance_ohm"] == pytest.approx(0.0484814, rel=1e-5)

    # Hatalı argüman
    err = handle_tool_call("calculate_trace_parasitics", {"length_mm": -10, "width_mm": 0.2})
    assert err["status"] == "ERROR"


def test_tool_analyze_hotplug_transient():
    # 1 m kablo: L = 2 * 100 cm * 12 nH/cm = 2.4 µH (TI SLOD006 s.362 aralığının üst ucu)
    res = handle_tool_call(
        "analyze_hotplug_transient",
        {"vin_step": 12.0, "cin_uf": 10.0, "cable_length_m": 1.0, "esr_mohm": 10.0},
    )
    assert res["status"] == "SUCCESS"
    assert res["cable_inductance_nh"] == pytest.approx(2400.0)
    # Elle: zeta = 0.01/2 * sqrt(10µ/2.4µ) = 0.0102062; V_peak = 12 (1 + exp(-pi zeta / sqrt(1 - zeta^2))) = 23.6213 V
    assert res["damping_ratio_zeta"] == pytest.approx(0.0102062, rel=1e-5)
    assert res["peak_voltage"] == pytest.approx(23.6213, rel=1e-5)
    assert res["overshoot_percent"] == pytest.approx(96.8443, rel=1e-5)  # 100 exp(-pi zeta / sqrt(1 - zeta^2))
    assert res["recommended_series_r_ohm"] == pytest.approx(0.969796, rel=1e-5)  # 2 sqrt(2.4µ/10µ) - 0.01
    assert "risk_assessment" in res
    assert res["recommended_damping_c_bulk_f"] == pytest.approx(4 * 10e-6)
    assert res["recommended_damped_peak_voltage"] < res["peak_voltage"]

    err = handle_tool_call("analyze_hotplug_transient", {"vin_step": -5.0, "cin_uf": 10.0})
    assert err["status"] == "ERROR"


def test_hotplug_tool_uses_measured_cable_values_and_the_bulk_ratio():
    """Ölçülmüş girdiler: 1000 nH, 50 mΩ, 10 µF, ESR 0. Elle: zeta = 0.05/2 * sqrt(10µ/1µ) = 0.0790569,
    V_peak = 12 (1 + exp(-pi zeta / sqrt(1 - zeta^2))) = 21.3536 V; bulk_ratio = 2 -> C_b = 20 µF."""
    res = handle_tool_call(
        "analyze_hotplug_transient",
        {"vin_step": 12.0, "cin_uf": 10.0, "cable_inductance_nh": 1000.0, "cable_resistance_mohm": 50.0,
         "bulk_ratio": 2.0},
    )
    assert res["cable_inductance_nh"] == pytest.approx(1000.0)
    assert res["damping_ratio_zeta"] == pytest.approx(0.0790569, rel=1e-5)
    assert res["peak_voltage"] == pytest.approx(21.3536, rel=1e-5)
    assert res["recommended_damping_c_bulk_f"] == pytest.approx(20e-6)
    (schema,) = [t for t in get_ltspice_tool_schemas(include_extended=True) if t["name"] == "analyze_hotplug_transient"]
    # Eski açıklama öneriyi AN-88 s.4 örneğinin oranlarına atfediyordu; kod 4·C_in ve aranmış ESR kullanıyor
    assert "oranlarıyla" not in schema["description"] and "bulk_ratio" in schema["parameters"]["properties"]


def test_hotplug_tool_rejects_the_removed_cable_presets_and_judges_against_the_rating():
    """Eski hata: şema tanımsız 'bench_leads_2m'/'barrel_jack_1m' öneriyordu ve hazır kablo değerlerinin kaynağı yoktu;
    kaldırıldı, yine verilirse sessizce yok sayılmaz. Güvenlik kararı max_voltage'a göredir (verilmezse güvenli değil)."""
    (schema,) = [t for t in get_ltspice_tool_schemas(include_extended=True) if t["name"] == "analyze_hotplug_transient"]
    assert "cable_type" not in schema["parameters"]["properties"]
    old = handle_tool_call("analyze_hotplug_transient", {"vin_step": 12.0, "cin_uf": 10.0, "cable_type": "usb_1m"})
    assert old["status"] == "ERROR" and "cable_length_m" in old["message"]
    args = {"vin_step": 12.0, "cin_uf": 10.0, "cable_length_m": 1.0}
    unrated = handle_tool_call("analyze_hotplug_transient", args)
    assert unrated["risk_assessment"] == "NO_RATING" and unrated["is_safe"] is False
    # R = 0: ideal tepe 2 * 12 = 24 V (sönümsüz seri RLC)
    assert unrated["peak_voltage"] == pytest.approx(24.0)
    # Öneriyle tepe: LTspice 26.1.1 "12 V · 1 m kablo · 10 µF + araç önerisi" 16.4694 V (examples/15 T2); bildirilen
    # ESR o tepeyi veren ESR'dir
    from ltspice_llm.parasitics import hotplug_step_peak

    assert unrated["recommended_damped_peak_voltage"] == pytest.approx(16.4694, rel=5e-6)
    assert hotplug_step_peak(12.0, 2.4e-6, 10e-6, 0.0, unrated["recommended_damping_c_bulk_f"],
                             unrated["recommended_damping_esr_ohm"]) == pytest.approx(
        unrated["recommended_damped_peak_voltage"], rel=1e-9)
    rated = handle_tool_call("analyze_hotplug_transient", {**args, "max_voltage": 30.0})
    assert rated["risk_assessment"] == "WITHIN_RATING" and rated["is_safe"] is True
    over = handle_tool_call("analyze_hotplug_transient", {**args, "max_voltage": 20.0})
    assert over["risk_assessment"] == "EXCEEDS_RATING" and over["is_safe"] is False


def test_tool_create_or_replace_schematic(adapter, monkeypatch):
    """Şema kaydedilir, ama yürütülebilir sahte (python.exe) olduğundan ``-netlist`` bağlantıyı doğrulayamaz: sonuç
    ERROR olur ve simülasyon (``-b``) hiç başlatılmaz. Eski hata: doğrulama yapılamasa da SUCCESS dönüyordu.
    Bağlı/kopuk şemanın gerçek LTspice ile yolları: tests/test_integration_reference.py."""
    import ltspice_llm.adapter as adapter_mod

    real_popen, commands = adapter_mod.subprocess.Popen, []

    def spy(cmd, *args, **kwargs):  # gerçek sınır: süreç başlatılır, yalnızca komut kaydedilir
        commands.append([str(c) for c in cmd])
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", spy)
    sch_data = {
        "symbols": [
            {"name": "voltage", "x": 100, "y": 200, "inst_name": "V1", "value": "5V"},
            {"name": "res", "x": 300, "y": 200, "inst_name": "R1", "value": "1k"},
        ],
        "connections": [{"from_inst": "V1", "from_pin": "+", "to_inst": "R1", "to_pin": "1"}],
        "grounds": [[100, 296]],
        "directives": [".op"],
    }
    for extra in ({"inspect_only": True}, {}):  # yalnızca denetim / denetim + simülasyon (varsayılan)
        commands.clear()
        res = handle_tool_call(
            "create_or_replace_schematic",
            {"circuit_name": "sch_test", "schematic_data": sch_data, **extra},
            adapter=adapter,
        )
        assert res["status"] == "ERROR", extra
        assert "doğrulanamadı" in res["message"] and "netlist üretmedi" in res["message"]
        assert any("-netlist" in c for c in commands), extra  # doğrulama denendi
        assert not any("-b" in c for c in commands), extra  # simülasyon başlatılmadı
    assert (adapter.workspace_dir / "sch_test.asc").exists()


# Elle yazılmış log kullanan eski test_tool_check_design_safety kaldırıldı (GEMINI.md: LTspice davranışını taklit eden
# fixture gerçek log olmalı); aynı senaryo (FAIL + MARGINAL -> FAIL) gerçek logla
# test_design_safety_overall_result_is_the_worst_component'te.
# Gerçek LTspice 26.1.1 logu (examples/14 ``safety``): V1 a 0 30, C10 a-0, R1 a-b 1k, R2 b-0 1k, D1 0-a (ters),
# V2 c 0 -30, C11 c-0. Elle: v_c10 = MAX V(a) = 30 V ve ondan sonra vhalf_c10 = MAX V(b) = 15 V (C10'un en kötüsü
# 30 V); pavg_r1 = AVG V(a,b)*I(R1) = 15^2 / 1k = 0.225 W; v_d1 = 30 V; v_c11 = MIN V(c) = -30 V. C1, C7 ölçülmedi.
SAFETY_LOG = Path(__file__).parent / "data" / "ref26_safety.log.txt"


def _safety(adapter, ratings, **extra):
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "saf.log").write_bytes(SAFETY_LOG.read_bytes())
    args = {"circuit_name": "saf", "component_ratings": ratings, **extra}
    return handle_tool_call("check_design_safety", args, adapter=adapter)


def test_design_safety_matches_whole_component_names(adapter):
    """Eski hata: alt dizgi eşleşmesi; C1, v_c10 = 30 V ölçümünü kendisinin sanıp FAIL aldı."""
    comps = {c["component"]: c for c in _safety(adapter, {"C1": {"max_voltage": 25}, "C10": {"max_voltage": 25}})["components"]}
    assert comps["C10"]["status"] == "FAIL" and comps["C10"]["measured_v_peak"] == pytest.approx(30.0)
    assert comps["C1"]["status"] == "UNCHECKED" and comps["C1"]["measured_v_peak"] is None


def test_design_safety_never_passes_an_unmeasured_component(adapter):
    """Eski hata: ölçümü olmayan bileşen (C7) hiçbir denetimden geçmeden PASS aldı, genel sonuç da PASS oldu."""
    res = _safety(adapter, {"C7": {"max_voltage": 25}})
    assert res["components"][0]["status"] == "UNCHECKED"
    assert res["overall_safety"] == "INCOMPLETE"
    assert res["unchecked"] == ["C7"]


def test_design_safety_reads_average_power_as_power(adapter):
    """Eski hata: 'pavg_r1' adındaki AVG'nin V'si yüzünden ölçüm gerilim sayıldı, güç denetlenmeden PASS verildi.
    Elle: 0.225 W, 0.25 W'lık direncin varsayılan %50 derating sınırı 0.125 W'ı aşar -> MARGINAL."""
    (r1,) = _safety(adapter, {"R1": {"max_power": 0.25}})["components"]
    assert r1["measured_power"] == pytest.approx(0.225, rel=1e-6)
    assert r1["measured_v_peak"] is None
    assert r1["status"] == "MARGINAL"


def test_design_safety_applies_the_given_derating_and_does_not_claim_a_standard(adapter):
    """Eski hata: ``derating_standard`` ("IPC-9592B"/"MIL-HDBK-1547") yalnızca çıktıya yazılıyordu, hiçbir katsayıyı
    değiştirmiyordu; iki standart da docs/references'ta yok. Katsayı artık girdi (varsayım) ve sonuçta görünür.
    Elle: 0.225 W < 0.95 * 0.25 = 0.2375 W -> PASS; 0.225 W > 0.8 * 0.25 = 0.2 W -> MARGINAL."""
    loose = _safety(adapter, {"R1": {"max_power": 0.25}}, derating={"power": 0.95})
    assert loose["components"][0]["status"] == "PASS" and loose["overall_safety"] == "PASS"
    assert loose["derating_applied"]["power"] == 0.95
    tight = _safety(adapter, {"R1": {"max_power": 0.25}}, derating={"power": 0.8})
    assert tight["components"][0]["status"] == "MARGINAL"
    (schema,) = [t for t in get_ltspice_tool_schemas(include_extended=True) if t["name"] == "check_design_safety"]
    text = json.dumps(schema) + json.dumps(loose)
    assert "IPC-9592" not in text and "MIL-HDBK" not in text


def test_design_safety_voltage_derating_default_and_override(adapter):
    """Elle: C10 üzerinde 30 V. 35 V'luk kondansatör: varsayılan 0.8 * 35 = 28 V < 30 V -> MARGINAL;
    ``derating={"voltage": 0.9}``: 0.9 * 35 = 31.5 V > 30 V -> PASS."""
    (default,) = _safety(adapter, {"C10": {"max_voltage": 35}})["components"]
    assert default["status"] == "MARGINAL"
    (loose,) = _safety(adapter, {"C10": {"max_voltage": 35}}, derating={"voltage": 0.9})["components"]
    assert loose["status"] == "PASS"


def test_design_safety_semiconductors_get_their_own_voltage_factor(adapter):
    """Elle: ters kutuplu D1, kesimdeki Q1 (C-E) ve C10 üzerinde 30 V, sınır 38 V. Q/D katsayısı 0.75 * 38 = 28.5 V
    < 30 V -> MARGINAL; kondansatör katsayısı 0.8 * 38 = 30.4 V > 30 V -> PASS."""
    res = _safety(adapter, {"D1": {"max_voltage": 38}, "Q1": {"max_voltage": 38}, "C10": {"max_voltage": 38}})
    statuses = {c["component"]: c["status"] for c in res["components"]}
    assert statuses == {"D1": "MARGINAL", "Q1": "MARGINAL", "C10": "PASS"}


def test_design_safety_uses_the_magnitude_of_a_negative_voltage(adapter):
    """Elle: v_c11 = MIN V(c) = -30 V; 25 V'luk C11 için |-30| = 30 V > 25 V -> FAIL."""
    (c11,) = _safety(adapter, {"C11": {"max_voltage": 25}})["components"]
    assert c11["status"] == "FAIL" and c11["measured_v_peak"] == pytest.approx(30.0)


def test_design_safety_overall_result_is_the_worst_component(adapter):
    """Gerçek log: R1 0.225 W > 0.125 W -> MARGINAL, C10 30 V > 25 V -> FAIL; genel sonuç FAIL (FAIL > MARGINAL)."""
    res = _safety(adapter, {"R1": {"max_power": 0.25}, "C10": {"max_voltage": 25}})
    assert {c["component"]: c["status"] for c in res["components"]} == {"R1": "MARGINAL", "C10": "FAIL"}
    assert res["overall_safety"] == "FAIL"


def test_design_safety_rejects_an_unknown_derating_key(adapter):
    """Yazım hatalı katsayı ('votlage') sessizce varsayılana düşmemeli."""
    res = _safety(adapter, {"R1": {"max_power": 0.25}}, derating={"votlage": 0.9})
    assert res["status"] == "ERROR" and "votlage" in res["message"]


# ------------------------------------------------------- Monte Carlo (gerçek LTspice kaydı yeniden oynatılır)
# Adaptörü mock'layan eski test_tool_simulate_monte_carlo_with_mock_adapter kaldırıldı (GEMINI.md: mock yalnızca gerçek
# sınırda); sahte verisi asıl hatayı (liste biçimi) saklıyordu. Aynı kapsam aşağıdaki replay testlerinde.
DATA = Path(__file__).parent / "data"
# Kaydın girdisi (examples/14 ``mc`` yazar): testler kayıtla aynı devreyi kullanır, kopya tutulmaz
MC_DIVIDER = (DATA / "ref26_mc_divider.net").read_text(encoding="utf-8")


@pytest.fixture
def replay_ltspice(monkeypatch):
    """Gerçek sınır ``subprocess.Popen``: LTspice'a giden netlisti saklar, gerçek LTspice çıktılarını yanına yazar."""
    import ltspice_llm.adapter as adapter_mod

    class _Replay:
        outputs = {}  # {".log": kayıt, ".raw": kayıt}
        netlists = []

        def __init__(self, cmd, **kwargs):
            netlist = Path(cmd[-1])
            type(self).netlists.append(netlist.read_text(encoding="utf-8"))
            for suffix, source in type(self).outputs.items():
                netlist.with_suffix(suffix).write_bytes(source.read_bytes())
            self.pid, self.returncode = 999, 0

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Replay)
    return _Replay


def test_monte_carlo_nominal_params_are_not_written_on_the_title_line(adapter, replay_ltspice):
    """ADI SPICE-SYNTAX-REFERENCE: "First line: Always treated as a comment (title line)". Eski hata: ``.param R1_nom``
    netlistin başına yazılıyordu; LTspice 26.1.1 onu başlık sayıp 'No such parameter defined' verdi."""
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "div.net").write_text(MC_DIVIDER, encoding="utf-8")
    handle_tool_call(
        "simulate_monte_carlo", {"circuit_name": "div", "tolerances": {"R1": 0.05, "R2": 0.05}, "runs": 5}, adapter=adapter
    )
    (sent,) = replay_ltspice.netlists
    assert sent.splitlines()[0] == "* mc divider"
    body = ltspice_visible(sent).splitlines()[1:]
    assert ".param R1_nom=10k" in body and ".param R2_nom=10k" in body
    # LTspice yardımı: mc(x,y) x*(1-y)..x*(1+y); tolerans aynen, nominal parametre üzerinden
    assert "R1 in out {mc({R1_nom}, 0.05)}" in body and "R2 out 0 {mc({R2_nom}, 0.05)}" in body
    assert ".step param run 1 5 1" in body


def test_monte_carlo_yield_reads_the_stepped_ltspice_log(adapter, replay_ltspice):
    """Eski hata: ``stepped_measurements`` ({ölçüm: [adım satırları]}, adapter.SimulationResult) liste sanılıyordu:
    "'str' object has no attribute 'get'". Log: bu bölücünün aracın ürettiği netlistle 5 adımlık LTspice 26.1.1
    kaydı (examples/14 ``mc``). Elle sınır: R1 = R2 = 10k ±%5 -> V(out) 4.75-5.25 V (LTspice yardımı: mc(x,y)
    x*(1-y) ile x*(1+y) arasında), yani her adım içeride, 5.25 V üstünde hiçbiri yok."""
    replay_ltspice.outputs = {".log": DATA / "ref26_mc_divider.log.txt", ".raw": DATA / "ref26_mc_divider.raw"}
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "div.net").write_text(MC_DIVIDER, encoding="utf-8")

    def run(spec):
        args = {"circuit_name": "div", "tolerances": {"R1": 0.05, "R2": 0.05}, "runs": 5, "target_specs": {"v_out": spec}}
        return handle_tool_call("simulate_monte_carlo", args, adapter=adapter)

    inside = run({"min": 4.75, "max": 5.25})
    assert inside["status"] == "SUCCESS", inside.get("message")
    assert inside["monte_carlo_runs"] == 5
    assert inside["yield_analysis"]["v_out"] == {
        "total_runs": 5, "passed_runs": 5, "yield_percent": 100.0, "min_spec": 4.75, "max_spec": 5.25,
    }
    above = run({"min": 5.25})
    assert above["yield_analysis"]["v_out"]["passed_runs"] == 0
    assert above["yield_analysis"]["v_out"]["yield_percent"] == 0.0


def _mc_replay(adapter, replay_ltspice, specs):
    replay_ltspice.outputs = {".log": DATA / "ref26_mc_divider.log.txt", ".raw": DATA / "ref26_mc_divider.raw"}
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "div.net").write_text(MC_DIVIDER, encoding="utf-8")
    args = {"circuit_name": "div", "tolerances": {"R1": 0.05, "R2": 0.05}, "runs": 5, "target_specs": specs}
    return handle_tool_call("simulate_monte_carlo", args, adapter=adapter)


def _logged_v_out():
    """Kayıttaki adım değerleri (LTspice 26.1.1, examples/14 ``mc``): test beklentileri bunlardan hesaplanır."""
    text = (DATA / "ref26_mc_divider.log.txt").read_text(encoding="utf-8")
    table = text.split("Measurement: v_out", 1)[1].split("Measurement", 1)[0]
    return [float(row.split()[1]) for row in table.strip().splitlines()[1:]]


def test_monte_carlo_yield_checks_both_bounds(adapter, replay_ltspice):
    v_out = _logged_v_out()
    expected = sum(5.0 <= v <= 5.18 for v in v_out)
    assert any(v < 5.0 for v in v_out) and any(v > 5.18 for v in v_out)  # bant iki uçtan da adım eler
    y = _mc_replay(adapter, replay_ltspice, {"v_out": {"min": 5.0, "max": 5.18}})["yield_analysis"]["v_out"]
    assert (y["passed_runs"], y["total_runs"]) == (expected, 5)
    assert y["yield_percent"] == pytest.approx(100.0 * expected / 5)


def test_monte_carlo_yield_bounds_are_inclusive(adapter, replay_ltspice):
    """Şema: min/max sınırları dahildir. Sınırlar kayıttaki en küçük ve en büyük adım değeriyse her adım geçer."""
    v_out = _logged_v_out()
    y = _mc_replay(adapter, replay_ltspice, {"v_out": {"min": min(v_out), "max": max(v_out)}})["yield_analysis"]["v_out"]
    assert y["passed_runs"] == 5


def test_monte_carlo_reports_no_yield_for_a_failed_simulation(adapter, replay_ltspice):
    """Başarısız koşumda ölçüm yoktur; verim "%0" diye raporlanmamalı. Log: gerçek LTspice 26.1.1 sözdizimi hatası
    (``ref26_syntax_bad_line``, examples/14 ``flow``)."""
    replay_ltspice.outputs = {".log": DATA / "ref26_syntax_bad_line.log.txt"}
    adapter.workspace_dir.mkdir(parents=True, exist_ok=True)
    (adapter.workspace_dir / "div.net").write_text(MC_DIVIDER, encoding="utf-8")
    args = {"circuit_name": "div", "tolerances": {"R1": 0.05}, "runs": 5, "target_specs": {"v_out": {"min": 4.75}}}
    res = handle_tool_call("simulate_monte_carlo", args, adapter=adapter)
    assert res["status"] == "FAILED"
    assert "yield_analysis" not in res


def test_monte_carlo_yield_counts_failed_steps_as_not_passing(adapter, replay_ltspice):
    """t_hi yalnızca V(out) > 5.1 V olan adımlarda üretilir (elle: V(r)*V(out)/10 en çok V(out)'a çıkar); diğer
    adımların hücresi 'failed'. Başarısız adım verimden düşmez, sağlamamış sayılır."""
    expected = sum(v > 5.1 for v in _logged_v_out())
    assert 0 < expected < 5
    y = _mc_replay(adapter, replay_ltspice, {"t_hi": {"min": 0}})["yield_analysis"]["t_hi"]
    assert (y["passed_runs"], y["total_runs"]) == (expected, 5)


def test_monte_carlo_yield_reports_a_measurement_that_failed_in_every_step(adapter, replay_ltspice):
    """LTspice 26.1.1 her adımda başarısız ölçüm için tablo yazmaz, tek satır 'Measurement "v_never" FAIL'ed' yazar.
    Eski hata: spec verim sonucundan sessizce düşüyordu; hiç üretilmeyen ölçüm %0 verimdir."""
    y = _mc_replay(adapter, replay_ltspice, {"v_never": {"min": 0}})["yield_analysis"]["v_never"]
    assert (y["passed_runs"], y["total_runs"], y["yield_percent"]) == (0, 5, 0.0)
    assert "hiçbir adımda üretilmedi" in y["note"]


def test_measurement_named_like_an_error_does_not_fail_the_run(adapter, replay_ltspice):
    """ref26_meas_names (examples/14 ``measnames``): gerçek LTspice 26.1.1 logu ve .raw'ı. Eski hata: 'gain_error: ...'
    satırı hata sayıldı; araç FAILED döndü, ölçüm kayboldu ve teşhis var olmayan bir hata için UNKNOWN_ERROR dedi.
    Bağımsız tanık: aynı koşumda aynı ifadeyi (MAX V(out)) ölçen v_max."""
    replay_ltspice.outputs = {".log": DATA / "ref26_meas_names.log.txt", ".raw": DATA / "ref26_meas_names.raw"}
    netlist = (DATA / "ref26_meas_names.net").read_text(encoding="utf-8")
    res = handle_tool_call("create_or_replace_circuit", {"circuit_name": "names", "netlist_code": netlist},
                           adapter=adapter)
    assert res["status"] == "SUCCESS", res["errors"]
    assert res["errors"] == [] and "diagnostics" not in res
    assert res["measurements"]["gain_error"] == res["measurements"]["v_max"]


def test_monte_carlo_yield_matches_ltspice_lowercase_names(adapter, replay_ltspice):
    """LTspice ölçüm adlarını küçük harfle yazar (log: 'Measurement: v_out'); spec 'V_OUT' aynı ölçümdür."""
    res = _mc_replay(adapter, replay_ltspice, {"V_OUT": {"min": 4.75, "max": 5.25}})
    assert res["yield_analysis"]["V_OUT"]["passed_runs"] == 5


# ------------------------------------------- create_or_replace_schematic (gerçek LTspice kayıtları yeniden oynatılır)
# examples/14 ``schematic``: şema girdisi (.json), LTspice -netlist'in ham çıktısı (.net), bağlı şemanın .op koşumu.
CONNECTED_RUN = {".log": DATA / "ref26_sch_connected.log.txt", ".raw": DATA / "ref26_sch_connected.raw"}


def _v_r_schematic(ground_r1):
    """V1 (5 V) + -> R1; R1'in 2. pini (316, 296) topraklı ya da boşta. Kaydın kendi girdisi okunur."""
    name = "connected" if ground_r1 else "open_pin"
    return json.loads((DATA / f"ref26_sch_{name}.json").read_text(encoding="utf-8"))


def _replay_ltspice_cli(monkeypatch, netlist, outputs=None):
    """
    Gerçek sınırlar: ``LTspice.exe -netlist`` (subprocess.run) ``netlist`` kaydının ham baytlarını şemanın yanına yazar;
    simülasyon (subprocess.Popen, ``-b``) başlatılırsa komutu ve LTspice'a giden dosyanın baytlarını saklar, ``outputs``
    kayıtlarını yanına yazar. Başlatılan simülasyonların [(komut, baytlar)] listesini döner.
    """
    from types import SimpleNamespace

    import ltspice_llm.adapter as adapter_mod

    launched = []

    def fake_run(cmd, **kwargs):
        Path(cmd[-1]).with_suffix(".net").write_bytes(netlist.read_bytes())
        return SimpleNamespace(returncode=0, stdout="")

    class FakeSimulation:
        def __init__(self, cmd, **kwargs):
            target = Path(cmd[-1])
            launched.append(([str(c) for c in cmd], target.read_bytes()))
            for suffix, source in (outputs or {}).items():
                target.with_suffix(suffix).write_bytes(source.read_bytes())
            self.pid, self.returncode = 999, 0

        def communicate(self, timeout=None):
            return "", ""

    monkeypatch.setattr(adapter_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", FakeSimulation)
    return launched


def test_schematic_tool_does_not_simulate_a_schematic_with_an_open_pin(adapter, monkeypatch):
    """Netlist: R1'in 2. pini boş şemanın LTspice 26.1.1 çıktısı ('R1 N001 NC_01 1k').
    Eski hata: kopuk pinli şema SUCCESS ile kaydedilip simüle ediliyordu."""
    launched = _replay_ltspice_cli(monkeypatch, DATA / "ref26_sch_open_pin.net", CONNECTED_RUN)
    res = handle_tool_call(
        "create_or_replace_schematic", {"circuit_name": "sch_open", "schematic_data": _v_r_schematic(False)},
        adapter=adapter,
    )
    assert res["status"] == "WARNING"
    assert [p["element"] for p in res["inspection"]["unconnected_pins"]] == ["R1"]
    assert launched == []  # simülasyon başlatılmadı


def test_schematic_tool_simulates_a_connected_schematic_once(adapter, monkeypatch):
    """Netlist 'R1 N001 0 1k'; simülasyon kaydı aynı şemanın .op koşumu. Elle: |I(R1)| = 5 V / 1k = 5 mA."""
    launched = _replay_ltspice_cli(monkeypatch, DATA / "ref26_sch_connected.net", CONNECTED_RUN)
    res = handle_tool_call(
        "create_or_replace_schematic", {"circuit_name": "sch_ok", "schematic_data": _v_r_schematic(True)},
        adapter=adapter,
    )
    assert res["status"] == "SUCCESS", res.get("errors") or res.get("message")
    assert abs(res["measurements"]["ir"]) == pytest.approx(5e-3, rel=1e-6)
    assert res["inspection"]["unconnected_pins"] == [] and res["inspection"]["dangling_nets"] == []
    assert [cmd[1] for cmd, _ in launched] == ["-b"]  # tek simülasyon


def test_schematic_tool_does_not_simulate_when_only_inspection_is_asked(adapter, monkeypatch):
    for extra in ({"inspect_only": True}, {"simulate": False}):
        launched = _replay_ltspice_cli(monkeypatch, DATA / "ref26_sch_connected.net", CONNECTED_RUN)
        res = handle_tool_call(
            "create_or_replace_schematic", {"circuit_name": "sch_i", "schematic_data": _v_r_schematic(True), **extra},
            adapter=adapter,
        )
        assert res["status"] == "SUCCESS", extra
        assert res["inspection"]["unconnected_pins"] == [], extra
        assert launched == [], extra


def test_schematic_tool_applies_parameters_to_the_simulated_schematic(adapter, monkeypatch):
    """``parameters`` LTspice'a giden şemaya yazılır (R1 1k -> 2k); kaydın değeri burada önemsiz, yalnızca giden dosya."""
    launched = _replay_ltspice_cli(monkeypatch, DATA / "ref26_sch_connected.net", CONNECTED_RUN)
    handle_tool_call(
        "create_or_replace_schematic",
        {"circuit_name": "sch_p", "schematic_data": _v_r_schematic(True), "parameters": {"R1": "2k"}},
        adapter=adapter,
    )
    ((_, sent),) = launched
    assert b"SYMATTR InstName R1\nSYMATTR Value 2k" in sent.replace(b"\r\n", b"\n")


