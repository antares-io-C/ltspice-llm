"""
Use case akış doğrulaması: gdb tracer'ının Python karşılığı (bkz. GEMINI.md "Test kuralları").

gdb ve gcov Python koduna bakamaz: gdb CPython yorumlayıcısının C tarafını görür (Windows'taki MSVC derlemesinde
hiç), gcov derleme enstrümantasyonu ister. Karşılıkları:

- gdb'nin durmayan breakpoint'leri -> `CallTracer` (``sys.setprofile`` + ``threading.setprofile``): test
  koşarken ``ltspice_llm`` fonksiyon çağrılarını durdurmadan kaydeder.
- gcov ``--gcov-check`` -> ``scripts/flows.py`` (coverage.py dinamik bağlamları, test başına).
- etkileşimli gdb -> ``pytest --pdb`` / ``--trace`` / ``breakpoint()``.

``tests/usecases/*.json`` her use case'in eylemlerini (``actions``) ve senaryolarını (``scenarios``) tutar.
Senaryonun ``test``'i koşarken conftest kaydı `check` ile karşılaştırır; tutmazsa test kırılır. Beklenen dizi
belgeden (``source``) yazılır, tracer çıktısından değil: kayda bakıp dizi yazmak testi koda yansıtır.

Kanıt türleri (``proof``): ``trace`` (tracer görür), ``boundary`` (testin gerçek sınırdaki assert'i: sahte
LTspice süreci, betikli LLM), ``ltspice`` (yalnızca gerçek LTspice çıktısında görünür), ``none`` (kapsam dışı).
``boundary``/``ltspice`` eylemleri senaryonun ``evidence``'ında testin kaynağından birebir alıntılanan assert'le
kanıtlanır; `validate` alıntının testte durduğunu denetler.
"""

import ast
import functools
import json
import os
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "ltspice_llm"
USECASES = Path(__file__).resolve().parent / "usecases"
FLOWS = {"Basic", "Alternative", "Error"}
MODES = {"subsequence", "strict"}
PROOFS = {"trace", "boundary", "ltspice", "none"}
_GENERATOR_FLAGS = 0x20 | 0x80 | 0x100 | 0x200  # CO_GENERATOR, CO_COROUTINE, CO_ITERABLE_COROUTINE, CO_ASYNC_GENERATOR


def _norm(path):
    # realpath: Windows'ta aynı dosya kısa (8.3, ör. KULLAN~1) ve uzun adla gelebilir; abspath bunu eşitlemez
    return os.path.normcase(os.path.realpath(path))


def _walk(body, prefix):
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = f"{prefix}.{node.name}"
            yield name, node
            yield from _walk(node.body, f"{name}.<locals>")
        elif isinstance(node, ast.ClassDef):
            yield from _walk(node.body, f"{prefix}.{node.name}")


def _first_body_line(node):
    """İlk bayt kodu üreten satır: docstring, ``global`` ve ``nonlocal`` coverage.py'de hiç çalışmış görünmez."""
    for index, stmt in enumerate(node.body):
        if isinstance(stmt, (ast.Global, ast.Nonlocal)):
            continue
        if index == 0 and isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant) \
                and isinstance(stmt.value.value, str):
            continue
        return stmt.lineno
    return node.body[0].lineno


@functools.lru_cache(maxsize=None)
def symbols():
    """
    ``ltspice_llm`` fonksiyonları: ``{(dosya, satır): sembol}`` ve ``{sembol: (dosya, ilk gövde satırı)}``.

    Sembol ``modül.Sınıf.fonksiyon`` biçimindedir (``adapter.LTSpiceAdapter.run_netlist``). Satır anahtarı hem
    ``def`` hem ilk dekoratör satırıdır: dekoratörlü fonksiyonda ``co_firstlineno`` dekoratörü gösterir.
    """
    by_line, body = {}, {}
    for path in sorted(PACKAGE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        file = _norm(path)
        for name, node in _walk(tree.body, path.stem):
            for line in [node.lineno] + [d.lineno for d in node.decorator_list]:
                by_line[(file, line)] = name
            body[name] = (file, _first_body_line(node))
    return by_line, body


class CallTracer:
    """Bağlam boyunca ``ltspice_llm`` fonksiyon çağrılarını ``(iş parçacığı, sembol)`` olarak kaydeder."""

    def __init__(self):
        self.events = []
        self._by_line = symbols()[0]
        self._files = {}
        self._resumed = set()
        self._lock = threading.Lock()

    def _profile(self, frame, event, arg):
        if event != "call":
            return
        code = frame.f_code
        file = self._files.get(code.co_filename)
        if file is None:
            file = self._files[code.co_filename] = _norm(code.co_filename)
        name = self._by_line.get((file, code.co_firstlineno))
        if name is None:
            return
        if code.co_flags & _GENERATOR_FLAGS:  # üreteç her next()'te yeniden "call" olayı üretir
            if id(frame) in self._resumed:
                return
            self._resumed.add(id(frame))
        with self._lock:
            self.events.append((threading.get_ident(), name))

    def __enter__(self):
        self._previous = (sys.getprofile(), threading.getprofile())
        threading.setprofile(self._profile)
        sys.setprofile(self._profile)
        return self

    def __exit__(self, *exc):
        sys.setprofile(self._previous[0])
        threading.setprofile(self._previous[1])


def _short(items, limit=40):
    items = list(items)
    return items if len(items) <= limit else items[:limit] + [f"... (+{len(items) - limit})"]


def _sequence_problem(sequence, recorded, mode):
    wanted = set(sequence)
    relevant = [s for s in recorded if s in wanted]
    if mode == "strict":
        if relevant != list(sequence):
            return f"birebir dizi tutmadı: beklenen {list(sequence)}, kaydedilen {_short(relevant)}"
        return None
    remaining = iter(relevant)
    for index, symbol in enumerate(sequence):
        if symbol not in remaining:
            return (f"sıralı alt dizi tutmadı: {index + 1}. adım {symbol} bulunamadı; "
                    f"kaydedilen {_short(relevant)}")
    return None


def check(scenario, events):
    """Kaydedilen çağrıları senaryoyla karşılaştırır; sorunları (boş liste: tuttu) döner."""
    problems = []
    names = [name for _, name in events]
    sequence = scenario.get("sequence") or []
    mode = scenario.get("mode", "subsequence")
    if sequence:
        if scenario.get("per_thread"):
            threads = {}
            for ident, name in events:
                threads.setdefault(ident, []).append(name)
            groups = [names_ for names_ in threads.values() if sequence[0] in names_]
            if not groups:
                problems.append(f"{sequence[0]} hiçbir iş parçacığında çağrılmadı")
        else:
            groups = [names]
        problems += [p for p in (_sequence_problem(sequence, g, mode) for g in groups) if p]
    for symbol in scenario.get("forbidden") or []:
        if symbol in names:
            problems.append(f"{symbol} çağrılmamalıydı, {names.count(symbol)} kez çağrıldı")
    for symbol, want in (scenario.get("counts") or {}).items():
        if names.count(symbol) != want:
            problems.append(f"{symbol}: {want} çağrı bekleniyordu, {names.count(symbol)} oldu")
    return problems


def load_usecases(directory=USECASES):
    cases = []
    for path in sorted(Path(directory).glob("*.json")):
        case = json.loads(path.read_text(encoding="utf-8"))
        case["_file"] = path.name
        cases.append(case)
    return cases


@functools.lru_cache(maxsize=None)
def _index():
    return {sc["test"]: (uc, sc) for uc in load_usecases() for sc in uc.get("scenarios", [])}


def scenario_for(nodeid):
    """pytest düğüm kimliğine bağlı ``(use case, senaryo)``; parametreler (``[..]``) aynı senaryoyu paylaşır."""
    return _index().get(nodeid.split("[", 1)[0])


def _test_source(test, root=ROOT):
    """``tests/x.py::test_y`` ya da ``tests/x.py::Sınıf::test_y`` için fonksiyonun kaynağı (yoksa None)."""
    file, _, qual = test.partition("::")
    path = root / file
    if not path.is_file() or not qual:
        return None
    text = path.read_text(encoding="utf-8")
    body = ast.parse(text).body
    *classes, func = qual.split("::")
    for cls in classes:
        body = next((n.body for n in body if isinstance(n, ast.ClassDef) and n.name == cls), [])
    node = next((n for n in body if isinstance(n, ast.FunctionDef) and n.name == func), None)
    return ast.get_source_segment(text, node) if node else None


def validate(cases, known=None, root=ROOT):
    """Manifest tutarlılığı: yazım (sembol var mı), testler var mı, her eylem kanıtlanıyor mu."""
    known = set(symbols()[1]) if known is None else set(known)
    problems, bound = [], {}
    for uc in cases:
        where = uc.get("_file", "?")
        for key in ("use_case", "source", "actions", "scenarios"):
            if not uc.get(key):
                problems.append(f"{where}: '{key}' eksik")
        actions = {}
        for action in uc.get("actions", []):
            aid = action.get("id")
            if aid in actions:
                problems.append(f"{where}: {aid} iki kez tanımlı")
            actions[aid] = action
            if action.get("proof") not in PROOFS:
                problems.append(f"{where}: {aid} proof {action.get('proof')!r} geçersiz ({sorted(PROOFS)})")
            if not action.get("source"):
                problems.append(f"{where}: {aid} için 'source' (belge) yok")
            if action.get("proof") == "trace" and action.get("symbol") not in known:
                problems.append(f"{where}: {aid} sembolü {action.get('symbol')!r} ltspice_llm'de yok")
        covered = set()
        for sc in uc.get("scenarios", []):
            test = sc.get("test", "")
            label = f"{where}: {test}"
            if test in bound:
                problems.append(f"{label} iki senaryoya bağlı ({bound[test]})")
            bound[test] = where
            source = _test_source(test, root)
            if source is None:
                problems.append(f"{label} test fonksiyonu bulunamadı")
            if sc.get("flow") not in FLOWS:
                problems.append(f"{label}: flow {sc.get('flow')!r} geçersiz ({sorted(FLOWS)})")
            if sc.get("mode", "subsequence") not in MODES:
                problems.append(f"{label}: mode {sc.get('mode')!r} geçersiz")
            named = list(sc.get("sequence") or []) + list(sc.get("forbidden") or []) + list(sc.get("counts") or {})
            if not named:
                problems.append(f"{label}: sequence/forbidden/counts boş, tracer hiçbir şey denetlemez")
            problems += [f"{label}: sembol {s!r} ltspice_llm'de yok" for s in named if s not in known]
            evidence = sc.get("evidence") or {}
            for aid in sc.get("actions", []):
                action = actions.get(aid)
                if action is None:
                    problems.append(f"{label}: eylem {aid} tanımlı değil")
                    continue
                covered.add(aid)
                if action.get("proof") == "trace" and action.get("symbol") not in named:
                    problems.append(f"{label}: {aid} 'trace' ama {action.get('symbol')} senaryoda denetlenmiyor")
                if action.get("proof") in ("boundary", "ltspice"):
                    quote = evidence.get(aid)
                    if not quote:
                        problems.append(f"{label}: {aid} için evidence (testteki assert alıntısı) yok")
                    elif source is not None and quote not in source:
                        problems.append(f"{label}: {aid} evidence testte yok: {quote!r}")
            problems += [f"{label}: evidence {aid} senaryonun actions listesinde değil"
                         for aid in evidence if aid not in sc.get("actions", [])]
        problems += [f"{where}: {aid} hiçbir senaryoda kanıtlanmıyor" for aid, a in actions.items()
                     if aid not in covered and a.get("proof") != "none"]
    return problems
