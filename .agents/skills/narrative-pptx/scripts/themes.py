"""
Sunum temaları: renkler, yazı tipleri ve anlam kodlu vurgu rolleri
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Kapak ve kapanış koyu, içerik slaytları açık zeminli. Yazı tipi adları .pptx'e yazılır; makinede yoksa PowerPoint
benzeriyle değiştirir.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Dict, Tuple


def hex_to_rgb(hex_color: str) -> Tuple[int, int, int]:
    """`#RRGGBB` dizgesini `(R, G, B)` tamsayı üçlüsüne çevirir."""
    clean = hex_color.strip().lstrip("#")
    if len(clean) != 6:
        raise ValueError(f"Geçersiz renk kodu: {hex_color!r}")
    return int(clean[0:2], 16), int(clean[2:4], 16), int(clean[4:6], 16)


@dataclass(frozen=True)
class AccentRole:
    """Anlam kodlu vurgu rengi (ana renk, açık zemin tonu, kenarlık tonu, koyu zemin parlak tonu)."""
    primary: str
    soft_bg: str
    soft_border: str
    on_dark: str


@dataclass(frozen=True)
class DeckTheme:
    """Bir sunumun tüm renk ve yazı tipi kimliği."""
    name: str
    font_heading: str
    font_body: str
    font_mono: str
    # Koyu (kapak / kapanış) tuval renkleri
    dark_bg: str
    dark_surface: str
    dark_border: str
    dark_text: str
    dark_muted: str
    # Açık (içerik / kanıt) tuval renkleri
    light_bg: str
    card_bg: str
    card_alt_bg: str
    card_border: str
    ink: str
    ink_secondary: str
    ink_muted: str
    bridge_bg: str
    # Anlam kodlu kimlik renkleri
    accents: Dict[str, AccentRole]
    # Metin dili (büyük harfe çevirmede kullanılır): "tr" ya da "en"
    language: str = "en"

    def accent(self, key: str = "blue") -> AccentRole:
        return self.accents.get(key, self.accents["blue"])

    def caps(self, text: str) -> str:
        """Büyük harfe çevirir; Türkçede i -> İ ve ı -> I (``str.upper`` "Geçiş"i "GEÇIŞ" yapar)."""
        if self.language == "tr":
            text = text.replace("i", "İ").replace("ı", "I")
        return text.upper()


MIDNIGHT_EDITORIAL = DeckTheme(
    name="midnight_editorial",
    font_heading="Segoe UI",
    font_body="Segoe UI",
    font_mono="Consolas",
    dark_bg="#0B1120",
    dark_surface="#131C31",
    dark_border="#23324E",
    dark_text="#F8FAFC",
    dark_muted="#94A3B8",
    light_bg="#F8FAFC",
    card_bg="#FFFFFF",
    card_alt_bg="#F1F5F9",
    card_border="#E2E8F0",
    ink="#0F172A",
    ink_secondary="#334155",
    ink_muted="#64748B",
    bridge_bg="#EEF2F7",
    accents={
        "blue": AccentRole("#2563EB", "#EFF6FF", "#BFDBFE", "#60A5FA"),
        "amber": AccentRole("#D97706", "#FFFBEB", "#FDE68A", "#FBBF24"),
        "emerald": AccentRole("#059669", "#ECFDF5", "#A7F3D0", "#34D399"),
        "rose": AccentRole("#E11D48", "#FFF1F2", "#FECDD3", "#FB7185"),
        "violet": AccentRole("#7C3AED", "#F5F3FF", "#DDD6FE", "#A78BFA"),
        "slate": AccentRole("#475569", "#F1F5F9", "#CBD5E1", "#94A3B8"),
    },
)

NORDIC_LIGHT = DeckTheme(
    name="nordic_light",
    font_heading="Segoe UI",
    font_body="Segoe UI",
    font_mono="Consolas",
    dark_bg="#182232",
    dark_surface="#223046",
    dark_border="#334663",
    dark_text="#F9FAFB",
    dark_muted="#CBD5E1",
    light_bg="#F4F6F9",
    card_bg="#FFFFFF",
    card_alt_bg="#EBF0F5",
    card_border="#DCE3EC",
    ink="#111827",
    ink_secondary="#374151",
    ink_muted="#6B7280",
    bridge_bg="#E6ECF2",
    accents=MIDNIGHT_EDITORIAL.accents,
)

OBSIDIAN_PRO = DeckTheme(
    name="obsidian_pro",
    font_heading="Calibri",
    font_body="Calibri",
    font_mono="Consolas",
    dark_bg="#090D16",
    dark_surface="#111827",
    dark_border="#1F2937",
    dark_text="#F9FAFB",
    dark_muted="#9CA3AF",
    light_bg="#F9FAFB",
    card_bg="#FFFFFF",
    card_alt_bg="#F3F4F6",
    card_border="#E5E7EB",
    ink="#111827",
    ink_secondary="#374151",
    ink_muted="#6B7280",
    bridge_bg="#F3F4F6",
    accents=MIDNIGHT_EDITORIAL.accents,
)

THEMES: Dict[str, DeckTheme] = {
    "midnight_editorial": MIDNIGHT_EDITORIAL,
    "nordic_light": NORDIC_LIGHT,
    "obsidian_pro": OBSIDIAN_PRO,
}


def get_theme(name: str | None = None, language: str = "en") -> DeckTheme:
    """Adı verilen tema (varsayılan ``midnight_editorial``), ``language`` ile."""
    theme = THEMES.get(name or "midnight_editorial")
    if theme is None:
        raise ValueError(f"Bilinmeyen tema {name!r}; seçenekler: {sorted(THEMES)}")
    if language not in ("tr", "en"):
        raise ValueError(f"language 'tr' ya da 'en' olmalı: {language!r}")
    return replace(theme, language=language)
