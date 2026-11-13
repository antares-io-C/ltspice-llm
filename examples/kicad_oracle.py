"""
KiCad tarafı: sentetik test kartlarını kurar ve KiCad'in kendi hesabını (tanık) JSON olarak kaydeder.

Bu betik KiCad'in kendi Python'uyla çalışır (``pcbnew`` yalnız orada var) ve ``ltspice_llm``'i içe aktarmaz. Doğrudan
çağırmayın: ``examples/20_kicad_board_checks.py`` KiCad'in Python'unu bulur, kartı geçici klasöre kopyalar ve bu betiği
orada koşturur.

    <KiCad>/bin/python.exe kicad_oracle.py build paths <cikti.kicad_pcb>
    <KiCad>/bin/python.exe kicad_oracle.py dump <kart.kicad_pcb> <cikti.json>

Ölçülen tuzaklar (KiCad 10.0.3, 2026-11-12):

- ``pcbnew.LoadBoard`` kartın yanına ``.kicad_pro`` / ``.kicad_prl`` yazar. Kullanıcının proje klasöründe çalıştırmayın;
  kartı geçici klasöre kopyalayın.
- Katman vermeden ``PCB_VIA.GetWidth()`` gibi eski çağrılar "wxWidgets Debug Alert" penceresi açar ve cevap bekler
  (koşum asılı kalır). ``wx.DisableAsserts()`` yalnız assert pencerelerini kapatır.
- wxLog bilgi/uyarı iletileri de mesaj kutusu olarak açılır ve kullanıcı "Tamam"a basana kadar süreç bekler: demo
  ``RoyalBlue54L-Feather`` kartını yüklemek "Font 'Ubuntu Sans' not found; substituting 'Verdana Bold'." kutusunu açtı
  (2026-11-12). ``wx.Log.SetActiveTarget(wx.LogStderr())`` iletiyi stderr'e yazar; hedef nesnesinin referansı modül
  düzeyinde tutulur.
- ``ZONE_FILLER.Fill`` yeni kurulmuş (``CreateEmptyBoard``) kartta pad ya da via varsa süreci çökertir (rc 139). Kart
  kaydedilip ``LoadBoard`` ile yeniden yüklenince çalışır.
- ``BOARD_STACKUP`` SWIG'de sarılı değil (``GetList`` yok). Yüklenmiş kartta ``m_HasStackup = True`` KiCad'in varsayılan
  katman yapısını dosyaya yazdırır.
- ``kicad-cli pcb export stats`` net başına uzunluk vermez ve JSON anahtarlarını KiCad'in diline çevirir.
"""

import hashlib
import json
import math
import os
import sys

import pcbnew
import wx

wx.DisableAsserts()  # assert penceresi kullanıcının ekranında açılıp koşumu bekletmesin
_LOG = wx.LogStderr()          # KiCad'in bilgi iletileri (ör. eksik yazı tipi) pencere yerine stderr'e
wx.Log.SetActiveTarget(_LOG)   # referansı modül düzeyinde tut

F, B = pcbnew.F_Cu, pcbnew.B_Cu


def mm(x, y):
    return pcbnew.VECTOR2I(pcbnew.FromMM(x), pcbnew.FromMM(y))


def xy(v):
    return [round(pcbnew.ToMM(v.x), 6), round(pcbnew.ToMM(v.y), 6)]


def layer_name(layer_id):
    """Dosyadaki (kanonik) katman adı; ``board.GetLayerName`` kullanıcının verdiği adı döndürür."""
    return pcbnew.BOARD.GetStandardLayerName(layer_id)


# --------------------------------------------------------------------------------------------------- kart kurucu
class Builder:
    """Sentetik kart: her şey programla kurulur, kütüphane footprint'i ya da kullanıcı tasarımı içermez."""

    def __init__(self, copper_layers=2):
        self.board = pcbnew.CreateEmptyBoard()
        if copper_layers != 2:
            self.board.SetCopperLayerCount(copper_layers)
        self.nets = {}
        #: Kaydedilen metne KiCad yüklemeden önce eklenecek satırlar (API'de karşılığı olmayan alanlar için): KiCad
        #: dosyayı yükleyip kendisi yeniden yazar, yani son dosyadaki sözdizimi KiCad'indir.
        self.patches = []

    def net(self, name):
        if name not in self.nets:
            item = pcbnew.NETINFO_ITEM(self.board, name)
            self.board.Add(item)
            self.nets[name] = item
        return self.nets[name]

    def footprint(self, ref, x, y, pads, angle=0.0, flip=False, jumpers=False, jumper_groups=()):
        """
        ``pads``: [(numara, dx, dy, net ya da None, biçim sözlüğü)]; dx/dy footprint merkezine göre (döndürmeden önce).
        ``jumpers``: aynı numaralı padler içten bağlı (``duplicate_pad_numbers_are_jumpers``); ``jumper_groups``: içten
        bağlı pad numarası grupları (``jumper_pad_groups``; SWIG'de ayarlanamıyor, metinle eklenir).
        """
        fp = pcbnew.FOOTPRINT(self.board)
        if jumpers:
            fp.SetDuplicatePadNumbersAreJumpers(True)
        if jumper_groups:
            groups = " ".join("(" + " ".join('"%s"' % n for n in group) + ")" for group in jumper_groups)
            self.patches.append(('(property "Reference" "%s"' % ref,
                                 '(jumper_pad_groups %s) (property "Reference" "%s"' % (groups, ref)))
        fp.SetReference(ref)
        fp.SetValue(ref)
        fp.SetPosition(mm(x, y))
        for number, dx, dy, net, style in pads:
            pad = pcbnew.PAD(fp)
            pad.SetNumber(number)
            kind = style.get("kind", "smd")
            shape = {"rect": pcbnew.PAD_SHAPE_RECT, "roundrect": pcbnew.PAD_SHAPE_ROUNDRECT,
                     "circle": pcbnew.PAD_SHAPE_CIRCLE, "oval": pcbnew.PAD_SHAPE_OVAL}[style.get("shape", "roundrect")]
            if kind == "smd":
                pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
                pad.SetLayerSet(pad.SMDMask())
            else:
                pad.SetAttribute(pcbnew.PAD_ATTRIB_PTH)
                pad.SetLayerSet(pad.PTHMask())
                drill = style.get("drill", 0.8)
                pad.SetDrillSize(mm(drill, drill))
            pad.SetShape(F, shape)
            pad.SetSize(F, mm(*style.get("size", (1.0, 1.2))))
            if shape == pcbnew.PAD_SHAPE_ROUNDRECT:
                pad.SetRoundRectRadiusRatio(F, style.get("rratio", 0.25))
            pad.SetPosition(mm(x + dx, y + dy))
            if net:
                pad.SetNet(self.net(net))
            fp.Add(pad)
        self.board.Add(fp)
        if angle:
            fp.SetOrientationDegrees(angle)
        if flip:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        return fp

    def tp(self, ref, x, y, net, **style):
        """Tek padli test noktası (pad "1")."""
        return self.footprint(ref, x, y, [("1", 0.0, 0.0, net, style)])

    def seg(self, net, x1, y1, x2, y2, width, layer=F):
        t = pcbnew.PCB_TRACK(self.board)
        t.SetStart(mm(x1, y1))
        t.SetEnd(mm(x2, y2))
        t.SetWidth(pcbnew.FromMM(width))
        t.SetLayer(layer)
        t.SetNet(self.net(net))
        self.board.Add(t)

    def arc(self, net, start, mid, end, width, layer=F):
        a = pcbnew.PCB_ARC(self.board)
        a.SetStart(mm(*start))
        a.SetMid(mm(*mid))
        a.SetEnd(mm(*end))
        a.SetWidth(pcbnew.FromMM(width))
        a.SetLayer(layer)
        a.SetNet(self.net(net))
        self.board.Add(a)

    def via(self, net, x, y, diameter=0.6, drill=0.3, layers=(F, B), via_type=None):
        v = pcbnew.PCB_VIA(self.board)
        v.SetPosition(mm(x, y))
        v.SetViaType(pcbnew.VIATYPE_THROUGH if via_type is None else via_type)
        v.SetLayerPair(*layers)
        v.SetWidth(F, pcbnew.FromMM(diameter))
        v.SetDrill(pcbnew.FromMM(drill))
        v.SetNet(self.net(net))
        self.board.Add(v)

    def zone(self, net, layer, corners):
        z = pcbnew.ZONE(self.board)
        z.SetLayer(layer)
        z.SetNet(self.net(net))
        outline = z.Outline()
        outline.NewOutline()
        for x, y in corners:
            outline.Append(pcbnew.FromMM(x), pcbnew.FromMM(y))
        self.board.Add(z)

    def outline(self, x1, y1, x2, y2):
        shape = pcbnew.PCB_SHAPE(self.board)
        shape.SetShape(pcbnew.SHAPE_T_RECT)
        shape.SetStart(mm(x1, y1))
        shape.SetEnd(mm(x2, y2))
        shape.SetLayer(pcbnew.Edge_Cuts)
        shape.SetWidth(pcbnew.FromMM(0.1))
        self.board.Add(shape)

    def save(self, path, title):
        """Kaydet, yeniden yükle (dökümü doldurmak ve katman yapısını yazdırmak için), yeniden kaydet."""
        tmp = path + ".tmp.kicad_pcb"
        if not pcbnew.SaveBoard(tmp, self.board):
            raise SystemExit("kart kaydedilemedi: " + tmp)
        if self.patches:
            with open(tmp, encoding="utf-8") as fh:
                text = fh.read()
            for old, new in self.patches:
                if text.count(old) != 1:
                    raise SystemExit("eklenecek yer bulunamadı: " + old)
                text = text.replace(old, new)
            with open(tmp, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
        board = pcbnew.LoadBoard(tmp)
        board.GetDesignSettings().m_HasStackup = True
        block = board.GetTitleBlock()
        block.SetTitle(title)
        block.SetRevision("A")
        block.SetDate("2026-11-12")
        board.BuildConnectivity()
        if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
            raise SystemExit("dökümler doldurulamadı")
        if not pcbnew.SaveBoard(path, board):
            raise SystemExit("kart kaydedilemedi: " + path)
        for leftover in (tmp, tmp[:-len(".kicad_pcb")] + ".kicad_pro", tmp[:-len(".kicad_pcb")] + ".kicad_prl"):
            if os.path.exists(leftover):
                os.remove(leftover)


def build_paths(out):
    """
    İki katmanlı yol kartı. Her net elle hesaplanabilen tek bir durumu taşır (uzunluklar mm):

    - /CHAIN  J1.1 -> TP1.1: 5 (0.25, F) + 6 (0.5, F) + via + 8 (0.5, B) + via + 6 (0.25, F) = 25
    - /TEE    bölünmemiş T: ana iz TP2-TP3 20, dal ortadan TP4'e 15 -> TP2-TP4 = 10 + 15 = 25
    - /ARC    TP5 -> TP6: 5 + çeyrek daire (r = 5: 2.5 pi) + 5
    - /CROSS  ucu ötekinin içinde olmayan iki çapraz iz (X); kesişim (66, 30)
    - /WIDE   2 mm'lik izin içinde, eksenden 0.8 mm kaçık başlayan ince dal
    - /LOOP   iki paralel yol: 12 ve 4 + 12 + 4 = 20
    - /POUR   yalnız bakır dökümle bağlı iki pad (iz yok)
    - /STUB   padin üstünden geçip 5 mm devam eden iz: TP13-TP14 = 10
    - /SHLD   aynı numaralı ("S") iki pad; iz yalnız birinden çıkar: 6
    - /OFFC   2 x 1.2 mm padin içinde, merkezden 0.7 mm kaçık biten iz: 9.3
    - /GAP    yönlendirilmemiş: iki iz ucu arasında 2 mm boşluk
    - /ROT    45 derece dönük R1'den alt yüze çevrilmiş C1'e: 18 (F) + via + 4 (B) = 22
    - /XSLOT  alt yüzdeki /CHAIN izinin GND dökümünde açtığı yarığın üstünden geçen 10 mm'lik iz
    - /JMP    JP1'in iki "1" padi içten bağlı (duplicate_pad_numbers_are_jumpers): TP27 -> JP1.1 6, öteki JP1.1 -> TP28 6;
              KiCad'e göre tek ada, ama TP27 -> TP28 iz yolu yok (bağlantı bileşenin içinden)
    - /JGRP   JP2'nin "1" ve "2" padleri içten bağlı (jumper_pad_groups): TP29 -> JP2.1 6, JP2.2 -> TP30 6
    - /TOUCH  iz yok; iki padin bakırı örtüşür (1 x 1.2 mm padler, merkezler arası 0.8 mm)
    - /GND    alt yüzün tamamı döküm (dönüş düzlemi), J1.2 ona bağlı
    """
    b = Builder()
    b.outline(0, 0, 90, 70)
    th = {"kind": "th", "shape": "circle", "size": (1.6, 1.6), "drill": 0.8}
    oval = {"kind": "th", "shape": "oval", "size": (1.6, 2.4), "drill": 0.8}

    b.footprint("J1", 10, 10, [("1", 0, 0, "/CHAIN", th), ("2", 0, 2.54, "/GND", th)])
    b.seg("/CHAIN", 10, 10, 13, 14, 0.25)
    b.seg("/CHAIN", 13, 14, 13, 20, 0.5)
    b.via("/CHAIN", 13, 20)
    b.seg("/CHAIN", 13, 20, 21, 20, 0.5, B)
    b.via("/CHAIN", 21, 20)
    b.seg("/CHAIN", 21, 20, 21, 26, 0.25)
    b.tp("TP1", 21, 26, "/CHAIN")

    b.tp("TP11", 17, 15, "/XSLOT")
    b.seg("/XSLOT", 17, 15, 17, 25, 0.25)
    b.tp("TP12", 17, 25, "/XSLOT")

    b.tp("TP2", 30, 10, "/TEE")
    b.tp("TP3", 50, 10, "/TEE")
    b.tp("TP4", 40, 25, "/TEE")
    b.seg("/TEE", 30, 10, 50, 10, 0.3)
    b.seg("/TEE", 40, 10, 40, 25, 0.3)

    b.tp("TP5", 60, 10, "/ARC")
    b.seg("/ARC", 60, 10, 60, 15, 0.25)
    half = 5 * math.sqrt(0.5)
    b.arc("/ARC", (60, 15), (65 - half, 15 + half), (65, 20), 0.25)
    b.seg("/ARC", 65, 20, 70, 20, 0.25)
    b.tp("TP6", 70, 20, "/ARC")

    for ref, x, y in (("TP7", 62, 26), ("TP8", 70, 34), ("TP9", 62, 34), ("TP10", 70, 26)):
        b.tp(ref, x, y, "/CROSS")
    b.seg("/CROSS", 62, 26, 70, 34, 0.25)
    b.seg("/CROSS", 62, 34, 70, 26, 0.25)

    b.tp("TP15", 30, 30, "/WIDE")
    b.tp("TP16", 50, 30, "/WIDE")
    b.tp("TP17", 40, 35, "/WIDE")
    b.seg("/WIDE", 30, 30, 50, 30, 2.0)
    b.seg("/WIDE", 40, 30.8, 40, 35, 0.25)

    b.tp("TP18", 10, 38, "/LOOP")
    b.tp("TP19", 22, 38, "/LOOP")
    b.seg("/LOOP", 10, 38, 22, 38, 0.25)
    b.seg("/LOOP", 10, 38, 10, 42, 0.25)
    b.seg("/LOOP", 10, 42, 22, 42, 0.25)
    b.seg("/LOOP", 22, 42, 22, 38, 0.25)

    b.tp("TP20", 31, 46, "/POUR")
    b.tp("TP21", 41, 46, "/POUR")
    b.zone("/POUR", F, [(27, 42), (45, 42), (45, 50), (27, 50)])

    b.tp("TP13", 50, 42, "/STUB")
    b.tp("TP14", 60, 42, "/STUB")
    b.seg("/STUB", 50, 42, 65, 42, 0.25)

    b.footprint("J2", 12, 52, [("S", -2, 0, "/SHLD", oval), ("S", 2, 0, "/SHLD", oval)])
    b.seg("/SHLD", 14, 52, 20, 52, 0.25)
    b.tp("TP22", 20, 52, "/SHLD")

    b.tp("TP23", 30, 56, "/OFFC", shape="rect", size=(2.0, 1.2))
    b.seg("/OFFC", 30.7, 56, 40, 56, 0.25)
    b.tp("TP24", 40, 56, "/OFFC")

    b.tp("TP25", 50, 54, "/GAP")
    b.seg("/GAP", 50, 54, 54, 54, 0.25)
    b.seg("/GAP", 56, 54, 60, 54, 0.25)
    b.tp("TP26", 60, 54, "/GAP")

    rect = {"shape": "rect"}
    r1 = b.footprint("R1", 70, 42, [("1", -1, 0, None, rect), ("2", 1, 0, "/ROT", rect)], angle=45)
    c1 = b.footprint("C1", 71, 54, [("1", -1, 0, None, {}), ("2", 1, 0, None, {})], flip=True)
    start = next(p for p in r1.Pads() if p.GetNumber() == "2").GetPosition()
    target = max(c1.Pads(), key=lambda p: p.GetPosition().x)   # via'ya (x = 76) yakın olan pad
    target.SetNet(b.net("/ROT"))
    sx, sy = pcbnew.ToMM(start.x), pcbnew.ToMM(start.y)
    tx, ty = pcbnew.ToMM(target.GetPosition().x), pcbnew.ToMM(target.GetPosition().y)
    b.seg("/ROT", sx, sy, 76, sy, 0.25)
    b.seg("/ROT", 76, sy, 76, 54, 0.25)
    b.via("/ROT", 76, 54)
    b.seg("/ROT", 76, 54, tx, ty, 0.25, B)

    b.tp("TP27", 22, 63, "/JMP")
    b.footprint("JP1", 30, 63, [("1", -2, 0, "/JMP", {}), ("1", 2, 0, "/JMP", {})], jumpers=True)
    b.tp("TP28", 38, 63, "/JMP")
    b.seg("/JMP", 22, 63, 28, 63, 0.25)
    b.seg("/JMP", 32, 63, 38, 63, 0.25)

    b.tp("TP29", 52, 63, "/JGRP")
    b.footprint("JP2", 60, 63, [("1", -2, 0, "/JGRP", {}), ("2", 2, 0, "/JGRP", {})], jumper_groups=[("1", "2")])
    b.tp("TP30", 68, 63, "/JGRP")
    b.seg("/JGRP", 52, 63, 58, 63, 0.25)
    b.seg("/JGRP", 62, 63, 68, 63, 0.25)

    b.tp("TP31", 78, 63, "/TOUCH")
    b.tp("TP32", 78.8, 63, "/TOUCH")

    b.zone("/GND", B, [(2, 2), (88, 2), (88, 68), (2, 68)])
    b.save(out, "ltspice-llm test karti: yollar")


def build_4layer(out):
    """
    Dört katmanlı kart: KiCad'in varsayılan 4 katman yapısı (prepreg / çekirdek / prepreg).

    - /OUTER  F.Cu'da 20 mm (referans düzlemi In1.Cu)
    - /INNER  In1.Cu'da 20 mm, iki delikli pad arasında (stripline: microstrip denklemi uygulanmaz)
    - /BLIND  F.Cu 10 + kör via (F.Cu-In1.Cu) + In1.Cu 10; aynı noktada biten B.Cu izi via'ya bağlı DEĞİL
    - /GND    In2.Cu'nun tamamı döküm
    """
    b = Builder(copper_layers=4)
    b.outline(0, 0, 40, 50)
    th = {"kind": "th", "shape": "circle", "size": (1.6, 1.6), "drill": 0.8}
    in1, in2 = pcbnew.In1_Cu, pcbnew.In2_Cu

    b.tp("TP1", 10, 10, "/OUTER")
    b.seg("/OUTER", 10, 10, 30, 10, 0.2)
    b.tp("TP2", 30, 10, "/OUTER")

    b.footprint("J1", 10, 20, [("1", 0, 0, "/INNER", th)])
    b.seg("/INNER", 10, 20, 30, 20, 0.2, in1)
    b.footprint("J2", 30, 20, [("1", 0, 0, "/INNER", th)])

    blind = getattr(pcbnew, "VIATYPE_BLIND", None)
    if blind is None:
        blind = pcbnew.VIATYPE_BLIND_BURIED
    b.tp("TP3", 10, 30, "/BLIND")
    b.seg("/BLIND", 10, 30, 20, 30, 0.2)
    b.via("/BLIND", 20, 30, layers=(F, in1), via_type=blind)
    b.seg("/BLIND", 20, 30, 30, 30, 0.2, in1)
    b.footprint("J3", 30, 30, [("1", 0, 0, "/BLIND", th)])
    b.seg("/BLIND", 20, 30, 20, 40, 0.2, B)
    bottom = b.footprint("TP4", 20, 40, [("1", 0, 0, "/BLIND", {})], flip=True)
    assert bottom.IsFlipped()

    b.zone("/GND", in2, [(2, 2), (38, 2), (38, 48), (2, 48)])
    b.save(out, "ltspice-llm test karti: dort katman")


# --------------------------------------------------------------------------------------------------- tanık kaydı
def pad_label(pad):
    return "%s.%s" % (pad.GetParentFootprint().GetReference(), pad.GetNumber())


#: "İzin altında bakır var mı" örneklemesinin adımı (mm). Okuyucu (ltspice_llm.board) aynı kuralı kullanır:
#: n = max(1, ceil(uzunluk / adım)) nokta, t = (i + 0.5) / n.
COVERAGE_STEP_MM = 0.1


def copper_layers_of(item, enabled):
    """Delikli padin katman kümesi 32 bakır katmanın hepsini taşır; kartta açık olanlarla sınırlanır."""
    return [layer_name(layer) for layer in item.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer) and layer in enabled]


def plane_under(track, zones_by_layer):
    """Düz izin orta çizgisindeki örnek noktalardan, öteki bakır katmanlardaki dolu dökümün içinde kalanların oranı."""
    start, end = track.GetStart(), track.GetEnd()
    count = max(1, math.ceil(pcbnew.ToMM(track.GetLength()) / COVERAGE_STEP_MM))
    result = {}
    for layer, zones in zones_by_layer.items():
        if layer == track.GetLayer():
            continue
        inside = 0
        for i in range(count):
            t = (i + 0.5) / count
            point = pcbnew.VECTOR2I(int(round(start.x + (end.x - start.x) * t)), int(round(start.y + (end.y - start.y) * t)))
            if any(zone.HitTestFilledArea(layer, point, 0) for zone in zones):
                inside += 1
        result[layer_name(layer)] = round(inside / count, 6)
    return result


def dump(board_path, out_path, fill_samples=True):
    """KiCad'in hesabı: iz uzunlukları, pad konumları, izle bağlı pad adaları, döküm alanı ve içinde/dışında noktalar."""
    with open(board_path, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    board = pcbnew.LoadBoard(board_path)
    board.BuildConnectivity()
    conn = board.GetConnectivity()
    enabled = {layer for layer in board.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(layer)}
    zones_by_layer = {}
    for zone in board.Zones():
        if zone.GetNetname() and zone.IsFilled():
            for layer in zone.GetLayerSet().Seq():
                if layer in enabled:
                    zones_by_layer.setdefault(layer, []).append(zone)

    tracks, vias, nets = [], [], {}
    for t in board.GetTracks():
        net = t.GetNetname()
        stat = nets.setdefault(net, {"track_length_mm": 0.0, "tracks": 0, "vias": 0})
        if t.GetClass() == "PCB_VIA":
            stat["vias"] += 1
            vias.append({"net": net, "at": xy(t.GetPosition()), "diameter_mm": pcbnew.ToMM(t.GetWidth(t.TopLayer())),
                         "drill_mm": pcbnew.ToMM(t.GetDrillValue()), "top": layer_name(t.TopLayer()),
                         "bottom": layer_name(t.BottomLayer())})
            continue
        length = pcbnew.ToMM(t.GetLength())
        stat["track_length_mm"] += length
        stat["tracks"] += 1
        record = {"type": "arc" if t.GetClass() == "PCB_ARC" else "segment", "net": net, "layer": layer_name(t.GetLayer()),
                  "width_mm": pcbnew.ToMM(t.GetWidth()), "start": xy(t.GetStart()), "end": xy(t.GetEnd()),
                  "length_mm": round(length, 6)}
        if record["type"] == "arc":
            record["mid"] = xy(t.GetMid())
        elif fill_samples:
            record["plane_under"] = plane_under(t, zones_by_layer)
        tracks.append(record)
    for stat in nets.values():
        stat["track_length_mm"] = round(stat["track_length_mm"], 6)

    pads, by_uuid = [], {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            by_uuid[pad.m_Uuid.AsString()] = pad
            layers = copper_layers_of(pad, enabled)
            first = next((layer for layer in pad.GetLayerSet().Seq() if pcbnew.IsCopperLayer(layer)), F)
            box = pad.GetBoundingBox()
            pads.append({"ref": fp.GetReference(), "number": pad.GetNumber(), "net": pad.GetNetname(),
                         "at": xy(pad.GetPosition()), "size_mm": xy(pad.GetSize(first)),
                         "shape": pad.ShowPadShape(first), "angle_deg": round(pad.GetOrientationDegrees(), 6),
                         "layers": layers, "footprint_layer": layer_name(fp.GetLayer()),
                         "bbox_mm": [round(pcbnew.ToMM(v), 6) for v in (box.GetX(), box.GetY(), box.GetWidth(),
                                                                         box.GetHeight())]})

    islands, seen = {}, set()
    for uuid, pad in by_uuid.items():
        if uuid in seen or not pad.GetNetname():
            continue
        group = {uuid}
        for item in conn.GetConnectedItems(pad, pcbnew.EXCLUDE_ZONES):
            other = item.m_Uuid.AsString()
            if other in by_uuid:
                group.add(other)
        seen |= group
        islands.setdefault(pad.GetNetname(), []).append(sorted(pad_label(by_uuid[u]) for u in group))
    for groups in islands.values():
        groups.sort()

    zones = []
    for zone in board.Zones():
        if not zone.GetNetname():
            continue   # kural alanı (keepout): bakır değil
        zone.CalculateFilledArea()
        entry = {"net": zone.GetNetname(), "layers": copper_layers_of(zone, enabled), "is_filled": bool(zone.IsFilled()),
                 "filled_area_mm2": round(zone.GetFilledArea() / 1e12, 4), "samples": []}
        box = zone.GetBoundingBox()
        if fill_samples and zone.IsFilled():
            # Kenardan uzak noktalar: 0 ve 20 µm toleransla aynı cevabı verenler kaydedilir
            x0, y0, w, h = (pcbnew.ToMM(v) for v in (box.GetX(), box.GetY(), box.GetWidth(), box.GetHeight()))
            step = max(w, h) / 12.0
            for layer in zone.GetLayerSet().Seq():
                if layer not in enabled:
                    continue
                for i in range(13):
                    for j in range(13):
                        px, py = x0 + 0.137 + i * step, y0 + 0.093 + j * step
                        if px > x0 + w or py > y0 + h:
                            continue
                        exact = zone.HitTestFilledArea(layer, mm(px, py), 0)
                        loose = zone.HitTestFilledArea(layer, mm(px, py), pcbnew.FromMM(0.02))
                        if exact == loose:
                            entry["samples"].append([layer_name(layer), round(px, 6), round(py, 6), bool(exact)])
        zones.append(entry)

    result = {
        "kicad_version": pcbnew.Version(),
        "board_file": os.path.basename(board_path),
        "sha256": digest,
        "coverage_step_mm": COVERAGE_STEP_MM,
        "copper_layers": [layer_name(layer) for layer in board.GetEnabledLayers().Seq() if pcbnew.IsCopperLayer(layer)],
        "nets": dict(sorted(nets.items())),
        "islands": dict(sorted(islands.items())),
        "tracks": tracks,
        "vias": vias,
        "pads": pads,
        "zones": zones,
    }
    with open(out_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(result, fh, ensure_ascii=True, indent=1, sort_keys=True)
        fh.write("\n")


def main(argv):
    if len(argv) == 4 and argv[1] == "build" and argv[2] in ("paths", "4layer"):
        {"paths": build_paths, "4layer": build_4layer}[argv[2]](argv[3])
    elif len(argv) in (4, 5) and argv[1] == "dump":
        dump(argv[2], argv[3], fill_samples="--no-samples" not in argv)
    else:
        raise SystemExit(__doc__)
    print("OK", flush=True)


if __name__ == "__main__":
    main(sys.argv)
