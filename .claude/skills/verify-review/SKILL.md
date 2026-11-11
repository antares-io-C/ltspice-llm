---
name: verify-review
compatibility: Bu depo (ltspice_llm) ve Windows'ta LTspice 26. test-reviewer alt ajanı (.agents/agents/test-reviewer.md ve .claude/agents/test-reviewer.md) Antigravity ve Claude Code'da çalışır; başka ajanda testleri kendin incele.
description: Antigravity'nin (ya da başka bir yapay zekânın) verdiği inceleme, performans önerisi, donanım veya simülasyon iddialarını uygulamadan önce gerçek LTspice ölçümüyle doğrula, yalnızca tutanları uygula ve dersleri kaydet. "antigravity şunları verdi", "bu öneriler doğru mu", "incelemeyi uygula", "lesson learned olarak kaydet" gibi isteklerde kullan.
---

Bu skill'in tek kaynağı `.agents/skills/verify-review/SKILL.md` (Antigravity ile ortak). Önce o dosyayı oku ve
oradaki adımları izle. Oradaki göreli yollar (`scripts/`, `references/`, `assets/`) `.agents/skills/verify-review/`
klasörüne göredir, bu klasöre değil. Bu dosyaya yönerge yazma: önbilgi bloğu kanonik dosyanınkiyle birebir
aynı kalır (`tests/test_skills.py` denetler).
