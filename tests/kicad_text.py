"""
Testlerin elle yazdığı küçük ``.kicad_pcb`` metinleri. Sözdizimi KiCad 10.0.3'ün kendi yazdığı biçimdir
(``tests/data/kicad10_paths.kicad_pcb``, ``examples/kicad_oracle.py`` kurar): ``(net "ad")``, footprint'te
``(property "Reference" ...)``, pad ``(at dx dy)`` footprint'e göre.
"""

#: KiCad 10.0.3'ün iki katmanlı kartta yazdığı varsayılan katman yapısı (tests/data/kicad10_paths.kicad_pcb, setup)
STACKUP_2L = ('(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.035)) (layer "dielectric 1" (type "core") '
              '(thickness 1.51) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02)) (layer "B.Cu" (type "copper") '
              '(thickness 0.035))))')
LAYERS_2L = '(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'


def board_text(*items, version=20260206, setup=STACKUP_2L, layers=LAYERS_2L, header=""):
    """``(kicad_pcb ...)`` metni; ``items`` footprint / segment / via / zone S-ifadeleri."""
    return (f'(kicad_pcb (version {version}) (generator "pcbnew") (generator_version "10.0") (general (thickness 1.6))\n'
            f"{layers}\n{setup}\n{header}\n" + "\n".join(items) + "\n)\n")


def footprint(ref, x, y, pads, angle=None, layer="F.Cu"):
    """``pads``: [(numara, dx, dy, net ifadesi, biçim)]; net ifadesi ``'"ad"'`` ya da KiCad 9'da ``'3 "ad"'``."""
    at = f"(at {x} {y}{'' if angle is None else f' {angle}'})"
    body = " ".join(
        f'(pad "{number}" {style.get("kind", "smd")} {style.get("shape", "circle")} (at {dx} {dy}) '
        f'(size {style.get("size", (0.5, 0.5))[0]} {style.get("size", (0.5, 0.5))[1]}) '
        f'(layers {style.get("layers", chr(34) + layer + chr(34))}) (net {net}))'
        for number, dx, dy, net, style in pads)
    return f'(footprint "" (layer "{layer}") {at} (property "Reference" "{ref}") {body})'


def point_pad(ref, x, y, net, **style):
    """Tek padli test noktası (pad "1"); ``net`` adı tırnaklanır."""
    return footprint(ref, x, y, [("1", 0, 0, f'"{net}"', style)])


def segment(net, x1, y1, x2, y2, width=0.25, layer="F.Cu"):
    return (f'(segment (start {x1!r} {y1!r}) (end {x2!r} {y2!r}) (width {width}) (layer "{layer}") '
            f'(net "{net}"))')


def via(net, x, y, size=0.6, drill=0.3, layers=("F.Cu", "B.Cu")):
    return f'(via (at {x} {y}) (size {size}) (drill {drill}) (layers "{layers[0]}" "{layers[1]}") (net "{net}"))'
