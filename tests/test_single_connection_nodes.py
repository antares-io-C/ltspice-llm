"""
Netlistte tek bağlantılı düğüm (Gemini incelemesi Madde 5, 2026-10-06).

LTspice bunu söylemiyor: examples/16 T6'da açık uçlu direnç ve boşta LT1001 girişi olan netlistler başarılı koştu,
logda uyarı yoktu (docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md §2 madde 16). Eleman başına düğüm sayıları ADI
CIRCUIT-ELEMENTS-REFERENCE'ın sözdizimi satırlarından (satır numarası yanında); toprak ve global düğümler
SPICE-SYNTAX-REFERENCE "Node `0` ... `GND` is a special synonym" ve SIMULATION-COMMANDS-REFERENCE ".GLOBAL".
LTSpice gerektirmez (run_netlist testi kayıtlı LTspice çıktısını yeniden oynatır).
"""

import sys
from collections import Counter
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

import ltspice_llm.adapter as adapter_mod
from ltspice_llm import LTSpiceAdapter
from ltspice_llm.netlist import single_connection_nodes

DATA = Path(__file__).parent / "data"


def flagged(netlist):
    return {(d["node"], d["element"]) for d in single_connection_nodes(netlist)}


# --------------------------------------------------------------------------- examples/16 T6 (LTspice sessiz)
def test_open_ended_resistor_is_flagged():
    """examples/16 T6 'Tek bağlantılı düğüm (açık uçlu direnç)': LTspice 26.1.1 koşumu başarılı, uyarı yok."""
    assert flagged("* x\nV1 in 0 1\nR1 in x 1k\nR2 in 0 1k\n.op\n.end\n") == {("x", "R1")}


def test_floating_op_amp_input_is_flagged():
    """examples/16 T6 'Boşta op-amp girişi (LT1001 In+)': OUT, In-'e bağlı (aynı satırda iki kez geçer)."""
    net = ("* o\nV1 vcc 0 5\nV2 vee 0 -5\nXU1 inp out vcc vee out LT1001\nR1 out 0 10k\n"
           ".lib LTC.lib\n.op\n.end\n")
    assert flagged(net) == {("inp", "XU1")}


def test_connected_circuit_is_not_flagged():
    """examples/14 RC kaydı (tests/data/ref26_flow_rc.net): in ve out iki pine bağlı."""
    assert flagged((DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")) == set()


# --------------------------------------------------------------------------- düğüm sayıları (ADI sözdizimi)
@pytest.mark.parametrize("line,nodes", [
    ("Q1 c b e 2N3904", ["c", "b", "e"]),                # 213: Qxxx C B E [Substrate] <model>
    ("M1 d g s IRF530", ["d", "g", "s"]),                # 257: Mxxx Nd Ng Ns <model> (VDMOS)
    ("J1 d g s 2N5484", ["d", "g", "s"]),                # 321
    ("E1 o 0 a b 10", ["o", "a", "b"]),                  # 520: Exxx n+ n- nc+ nc- <gain>
    ("E1 o 0 value={2}", ["o"]),                         # 535: Exxx n+ n- value={<expression>}
    ("G1 o 0 a b table=(0,0,1,1m)", ["o", "a", "b"]),    # 553
    ("F1 o 0 V9 2", ["o"]),                              # 579: Fxxx n+ n- <Vnam> <gain>
    ("H1 o 0 V9 1k", ["o"]),                             # 565
    ("S1 o 0 a b SW", ["o", "a", "b"]),                  # 593
    ("W1 o 0 V9 CSW", ["o"]),                            # 621
    ("T1 a 0 b 0 Zo=50 Td=1n", ["a", "b"]),              # 655
    ("B1 o 0 V=1", ["o"]),                               # 713
    ("Xu1 a b c MYSUB params: k=2", ["a", "b", "c"]),    # 821: Xxxx n1 n2 n3 ... <subckt_name> [param=val]
    ("XU2 a b MYSUB k=2", ["a", "b"]),
    ("XU3 a b MYSUB", ["a", "b"]),
    ("Z1 d g s NMF", ["d", "g", "s"]),                   # 347: Zxxx D G S <model>
    ("U1 a b 0 URC L=1m", ["a", "b"]),                   # 688: Uxxx N1 N2 Ncom <model> L=<len>
    ("O1 a 0 b 0 LTRA", ["a", "b"]),                     # 668: Oxxx L+ L- R+ R- <model>
    ("A1 a b c d e o1 o2 0 AND", ["a", "b", "c", "d", "e", "o1", "o2"]),  # 774: 5 giriş, out+ out-, com
    ("K1 L1 L2 1", []),                                  # 638: Kxxx L1 L2 [L3 ...] <coefficient>: düğüm yok
])
def test_each_node_of_an_element_is_counted_once(line, nodes):
    """Her düğüm yalnızca bu satırda: hepsi işaretlenir; model adı, kaynak adı, alt devre adı ve parametreler düğüm
    sayılmaz (sayılsaydı onlar da işaretlenirdi)."""
    element = line.split()[0]
    assert flagged(f"* t\n{line}\n.op\n.end\n") == {(n, element) for n in nodes}


def test_optional_fourth_node_counts_as_a_connection_but_is_never_flagged():
    """213/249: Q'nun substratı ve M'nin gövdesi isteğe bağlı; 4. alan model adı da olabilir. Q1'in substratı sub'a
    bağlı (R1 ile iki bağlantı); Q2'nin 4. alanı model adı, işaretlenmemeli."""
    net = "* q\nQ1 c b e sub NPN1\nR1 sub 0 1k\nQ2 c b e 2N3904\nR2 c 0 1\nR3 b 0 1\nR4 e 0 1\n.op\n.end\n"
    assert flagged(net) == set()
    assert flagged("* q\nQ1 c b e sub NPN1\nR1 c 0 1\nR2 b 0 1\nR3 e 0 1\n.op\n.end\n") == set()


def test_poly_control_nodes_count_as_connections():
    """540: Exxx n+ n- POLY(N) (n1+,n1-) ... c0 c1: denetim düğümleri parantez içinde."""
    net = "* p\nV1 a 0 1\nE1 o 0 POLY(1) (a,0) 0 2\nR1 o 0 1k\n.op\n.end\n"
    assert flagged(net) == set()  # a: V1 ve E1'in denetim girişi
    assert flagged("* p\nV1 a 0 1\nE1 o 0 1 0 2\nR1 o 0 1k\n.op\n.end\n") == {("a", "V1"), ("1", "E1")}


# --------------------------------------------------------------------------- işaretlenmeyenler
def test_ground_and_global_nodes_are_never_flagged():
    net = "* g\nR1 a 0 1k\nR2 a GND 1k\nR3 a vdd 1k\nR4 a $G_VCC 1k\n.global vdd\n.op\n.end\n"
    assert flagged(net) == set()


def test_node_read_in_an_expression_is_connected():
    """B1 x'i ifadesinde okur (V(x)); x'i yalnızca V1 sürse de boşta değildir."""
    assert flagged("* b\nV1 x 0 1\nB1 y 0 V=2*V(x)\nR1 y 0 1k\n.op\n.end\n") == set()
    assert flagged("* e\nV1 x 0 1\nE1 y 0 value={V(x,0)}\nR1 y 0 1k\n.op\n.end\n") == set()


def test_subcircuit_body_is_not_checked_but_its_instance_is():
    net = ("* s\n.subckt AMP in out\nR1 in mid 1k\nR2 mid out 1k\nC1 nc 0 1p\n.ends AMP\n"
           "V1 a 0 1\nX1 a b AMP\n.op\n.end\n")
    assert flagged(net) == {("b", "X1")}


def test_continuation_lines_comments_and_case_are_handled():
    net = "* c\nR1 a\n+ Out 1k ; yorum: R9 z 0\n* R8 y 0 1k\nC1 out 0 1n\nV1 a 0 1\n.op\n.end\n"
    assert flagged(net) == set()


def test_lines_after_end_are_not_read():
    """LTspice ilk .end'den sonrasını okumaz (netlist.ltspice_visible)."""
    assert flagged("* e\nR1 a 0 1k\nR2 a 0 1k\n.end\nR3 a z 1k\n") == set()


def test_include_may_add_elements_so_the_check_is_skipped():
    """.include eleman ekleyebilir (ADI SIMULATION-COMMANDS-REFERENCE: .lib 'ignores global-scope circuit elements',
    .include değil)."""
    assert flagged("* i\n.include board.cir\nR1 a z 1k\n.op\n.end\n") == set()
    assert flagged("* l\n.lib LTC.lib\nR1 a z 1k\nR2 a 0 1\n.op\n.end\n") == {("z", "R1")}


# --------------------------------------------------------------------------- property: iki uçlu elemanlar
names = st.sampled_from(["0", "gnd", "a", "b", "Out", "out", "n1", "vcc"])


@given(st.lists(st.tuples(st.sampled_from("RCLDVI"), names, names), min_size=1, max_size=12))
def test_two_terminal_netlists_match_a_node_count(elements):
    """Bağımsız oracle: yalnız iki uçlu elemanlardan (ADI: R, C, L, D, V, I 'n1 n2') oluşan netlistte bir düğümün
    bağlantı sayısı adının geçme sayısıdır (büyük/küçük harf duyarsız; 0 ve GND toprak)."""
    lines = [f"{kind}{i} {a} {b} 1" for i, (kind, a, b) in enumerate(elements)]
    count = Counter(n.lower() for _, a, b in elements for n in (a, b))
    expected = {n for n, c in count.items() if c == 1 and n not in ("0", "gnd")}
    assert {d["node"] for d in single_connection_nodes("* p\n" + "\n".join(lines) + "\n.op\n.end\n")} == expected


# --------------------------------------------------------------------------- run_netlist uyarısı
class _Replay:
    """Kayıtlı LTspice 26.1.1 RC koşumunu (examples/14 ``flow``) netlistin yanına yazar."""

    def __init__(self, cmd, **kwargs):
        netlist = Path(cmd[-1])
        for suffix, name in ((".log", "ref26_flow_rc.log.txt"), (".raw", "ref26_flow_rc.raw")):
            netlist.with_suffix(suffix).write_bytes((DATA / name).read_bytes())
        self.pid, self.returncode = 999, 0

    def communicate(self, timeout=None):
        return "", ""


def test_run_netlist_warns_about_a_single_connection_node(tmp_path, monkeypatch):
    """LTspice koşumu başarılı sayar (kayıt), uyarı netlistten gelir ve LLM'e giden sonuçta görünür."""
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Replay)
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1)
    rc = (DATA / "ref26_flow_rc.net").read_text(encoding="utf-8")
    clean = ad.run_netlist(rc, circuit_name="clean", treat_as_text=True)
    assert clean.success and not [w for w in clean.warnings if "tek bir pine" in w]
    typo = ad.run_netlist(rc.replace("C1 out 0", "C1 vout 0"), circuit_name="typo", treat_as_text=True)
    assert typo.success
    (warning,) = [w for w in typo.warnings if "tek bir pine" in w]
    assert "out (R1)" in warning and "vout (C1)" in warning
    assert warning in typo.to_llm_dict()["warnings"]


def test_run_netlist_does_not_check_a_schematic_as_a_netlist(tmp_path, monkeypatch):
    """Şemada bağlantıya inspect_schematic bakar (LTspice netlisterı, NC_xx). .asc satırları (SHEET, FLAG, SYMBOL)
    netlist gibi okunsaydı her şema sahte uyarı alırdı (test-reviewer: '1 (SHEET), 880 (SHEET)...')."""
    monkeypatch.setattr(adapter_mod.subprocess, "Popen", _Replay)
    ad = LTSpiceAdapter(executable_path=sys.executable, workspace_dir=tmp_path / "ws", solver_threads=1)
    asc = ("Version 4\nSHEET 1 880 680\nWIRE 112 96 32 96\nSYMBOL res 96 96 R0\nSYMATTR InstName R1\n"
           "SYMATTR Value 1k\nFLAG 112 192 0\nFLAG 32 96 in\nTEXT 0 300 Left 2 !.op\n")
    res = ad.run_netlist(asc, circuit_name="sch", treat_as_text=True)
    assert res.netlist_path.suffix == ".asc" and res.success
    assert not [w for w in res.warnings if "tek bir pine" in w]
