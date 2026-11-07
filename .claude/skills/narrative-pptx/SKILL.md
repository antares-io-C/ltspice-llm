---
name: narrative-pptx
description: >-
  Kullanıcı bir simülasyon sonucunu, devreyi ya da teknik bir konuyu PowerPoint sunumuna dönüştürmek istediğinde
  kullan: "sunum hazırla", "slaytlara dök", "bulguları sunuma dök", "pptx", "PowerPoint", "deck", "presentation",
  "slide deck"; sunuma konacak açıklamalı grafik (PNG) de bu skill'dedir. Yalnızca GIF/animasyon isteniyorsa
  explainer-gif'i, HTML simülasyon raporu için SimulationReport'u kullan.
compatibility: Python 3.10+, python-pptx ve Pillow (pip install -e ".[media]"). Gerçek slayt önizlemesi için Windows'ta PowerPoint; yoksa kaba tel kafes önizleme.
---

Bu skill'in tek kaynağı `.agents/skills/narrative-pptx/SKILL.md` (Antigravity ile ortak). Önce o dosyayı oku ve
oradaki adımları izle. Oradaki göreli yollar (`scripts/`, `references/`, `assets/`) `.agents/skills/narrative-pptx/`
klasörüne göredir, bu klasöre değil. Bu dosyaya yönerge yazma: önbilgi bloğu kanonik dosyanınkiyle birebir
aynı kalır (`tests/test_skills.py` denetler).
