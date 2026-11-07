---
name: explainer-gif
description: >-
  Kullanıcı bir dalga formunu, zaman izini ya da parametre taramasını hareketli GIF olarak göstermek istediğinde
  kullan: "GIF", "hareketli grafik", "dalga formunu canlandır", "taramayı canlandır", "animated GIF",
  "animate waveform", "animate sweep", HTML/WebGL (Three.js) sahnesini GIF'e çevirme. Bütün bir sunum isteniyorsa
  narrative-pptx'i kullan; PowerPoint geçiş animasyonları bu skill'in işi değildir.
compatibility: Python 3.10+, numpy, matplotlib, Pillow (pip install -e ".[media]"); koyu tema için scipy; HTML/WebGL sahnesi için isteğe bağlı Playwright.
---

Bu skill'in tek kaynağı `.agents/skills/explainer-gif/SKILL.md` (Antigravity ile ortak). Önce o dosyayı oku ve
oradaki adımları izle. Oradaki göreli yollar (`scripts/`, `references/`, `assets/`) `.agents/skills/explainer-gif/`
klasörüne göredir, bu klasöre değil. Bu dosyaya yönerge yazma: önbilgi bloğu kanonik dosyanınkiyle birebir
aynı kalır (`tests/test_skills.py` denetler).
