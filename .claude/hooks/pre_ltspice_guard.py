"""
PreToolUse (Bash/PowerShell, ``python``/``pytest`` komutları): koordinasyonsuz eşzamanlı LTspice'ı engeller.

``tests/conftest.py`` her pytest oturumuna ayrı ``cpu_guard`` kilit dizini verir; pytest'in LTspice koşumları
başka süreçlerin iş parçacığı bütçesini görmez, onlar da pytest'inkini. İkisi aynı anda koşarsa çözücü
iş parçacıkları çekirdek sayısını aşar ve LTspice livelock'a girebilir (docs/LESSONS_LEARNED_PERFORMANCE.md).

- pytest başlatılırken penceresiz (batch) LTspice çalışıyorsa: engelle.
- pytest'e ait batch LTspice çalışırken LTspice kullanabilecek bir betik başlatılıyorsa: engelle.
- ``cpu_guard``'lı betikler birbirini zaten bekler; onlara dokunulmaz. LTspice GUI sayılmaz.

pytest koşumu komut metninde ``pytest`` sözcüğü aranarak değil, komutun bölümlerinde (``&& || ; | &``, satır sonu)
gerçekten çalıştırılan programa bakılarak bulunur: ``pytest``, ``python -m pytest`` ya da ``--full`` ile
``check.py``/``flows.py``/``isolation.py``. Eskiden ``pip install pytest``, ``python -c "print('pytest')"`` ya da
argümanında ``pytest`` geçen bir betik de engelleniyordu. Betiğin içinden (``subprocess``) başlatılan pytest
yakalanmaz. LTspice'sız koşum: o bölümde ``LTSPICE_EXE=<yol>`` (ya da önceki ``export``/``$env:``) var olmayan bir
dosyayı gösterir; ``LTSPICE_EXE`` sözcüğünün geçmesi yetmez.

``LTSPICE_LLM_GUARD=off`` içeren komut serbesttir (yalnızca kullanıcı onaylarsa kullanılır).
"""

import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ltspice_procs import batch_processes, describe, owned_by_pytest  # noqa: E402

BYPASS = re.compile(r"LTSPICE_LLM_GUARD['\"]?\s*=\s*['\"]?off", re.I)
# Testleri --full ile LTspice'la koşan betikler (pytest gibi); --full olmadan LTSPICE_EXE'yi kendileri kapatır
FULL_GATES = {"check.py", "flows.py", "isolation.py"}
PYTHON = re.compile(r"^(?:pythonw?(?:\d+(?:\.\d+)*)?|py)$", re.I)
WRAPPERS = {"env", "time", "command", "exec", "nohup", "timeout"}
# Komutu kendi kabuğunda koşan programlar: bash -c '...', pwsh -Command '...', cmd /c ...
SHELLS = {"bash": "Bash", "sh": "Bash", "zsh": "Bash", "pwsh": "PowerShell", "powershell": "PowerShell", "cmd": "cmd"}
SHELL_FLAGS = {"-c", "-command", "/c", "/k"}
ASSIGNMENT = re.compile(r"^(\$env:)?([A-Za-z_]\w*)=(.*)$", re.S | re.I)
# Bash heredoc'undaki Python kodu ayrı satırlarda durur: LTspice kullanabilecek betik bütün komut metninde aranır
USES_LTSPICE = re.compile(r"examples[/\\]|\.scratch[/\\]|ltspice_llm|run_(netlist|sweep|batch)|interactive_designer", re.I)


def segments(cmd):
    """Komutun bölümleri (``&& || ; | &`` ve satır sonu); her bölüm tırnakları atılmış sözcüklerin listesidir."""
    parts, words, word, in_word, quote = [], [], [], False, None
    for char in cmd + "\n":
        if quote:
            if char == quote:
                quote = None
            else:
                word.append(char)
        elif char in "'\"":
            quote, in_word = char, True
        elif char in " \t;|&\r\n":
            if in_word:
                words.append("".join(word))
                word, in_word = [], False
            if char not in " \t" and words:
                parts.append(words)
                words = []
        else:
            word.append(char)
            in_word = True
    return parts


def _program(word):
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _runs_python(args, env):
    """python argümanları pytest'i (``-m pytest``, ``-mpytest``, ``-m coverage run -m pytest``) ya da ``--full``
    kapısını mı koşuyor?"""
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "-":
        if args[i].startswith("-m"):
            module, rest = (args[i][2:], args[i + 1:]) if len(args[i]) > 2 else (" ".join(args[i + 1:i + 2]), args[i + 2:])
            if module == "coverage":
                return rest[:1] == ["run"] and _runs_python(rest[1:], env)
            return module in ("pytest", "py.test")
        if args[i] == "-c":
            return False
        i += 2 if args[i] in ("-X", "-W") else 1
    return i < len(args) and _program(args[i]) in FULL_GATES and "--full" in args[i + 1:]


def _runs_tests(command, env):
    """Komut (sözcükler) pytest'i ya da testleri ``--full`` ile koşan bir betiği mi çalıştırıyor?"""
    program, args = _program(command[0]), command[1:]
    if program in ("pytest", "py.test"):
        return True
    if program == "coverage":
        return args[:1] == ["run"] and _runs_python(args[1:], env)
    if program in SHELLS:  # iç komut kendi kabuğunun kurallarıyla, dışarıdaki ortamla okunur
        flag = next((i for i, arg in enumerate(args) if arg.lower() in SHELL_FLAGS), None)
        if flag is None:
            return False
        inner = args[flag + 1:] if program == "cmd" else args[flag + 1:flag + 2]
        return runs_ltspice_tests(" ".join(inner), SHELLS[program], env)
    return bool(PYTHON.match(program)) and _runs_python(args, env)


def runs_ltspice_tests(cmd, shell="Bash", env=None):
    """
    Komut LTspice'la test koşuyor mu: bir bölümü pytest (ya da ``--full`` kapısı) ve ``LTSPICE_EXE`` var olan bir
    dosyayı gösteriyor ya da hiç verilmemiş. Atamalar kabuğun kuralıyla okunur: Bash'te ``X=değer komut`` yalnız o
    komutta, ``export X=değer`` sonrakilerde de; PowerShell'de yalnız ``$env:X = 'değer'``; ``set`` ortam değişkeni
    yalnız ``cmd``'de. Başka kabuğun sözdizimi (Bash'te ``$env:``, PowerShell'de ``export``) atama değildir.
    """
    env = dict(env or {})
    for words in segments(cmd):
        exported = (shell == "Bash" and words[0] == "export") or (shell == "cmd" and words[0].lower() == "set")
        words = words[1:] if exported else words
        local, i = dict(env), 0
        while i < len(words):
            match = ASSIGNMENT.match(words[i])
            if match and bool(match.group(1)) == (shell == "PowerShell"):
                local[match.group(2).upper()] = match.group(3)
                if exported or match.group(1):
                    env[match.group(2).upper()] = match.group(3)
            elif shell == "PowerShell" or words[i].lower() not in WRAPPERS:
                break
            elif words[i].lower() == "timeout":  # timeout [seçenek] SÜRE komut
                while i + 1 < len(words) and words[i + 1].startswith("-"):
                    i += 1
                i += 1
            i += 1
        if i >= len(words):
            continue
        if shell == "PowerShell" and words[i].lower().startswith("$env:") and words[i + 1:i + 2] == ["="]:
            env[words[i][5:].upper()] = words[i + 2] if i + 2 < len(words) else ""
        elif _runs_tests(words[i:], local):
            exe = local.get("LTSPICE_EXE")
            if exe is None or os.path.isfile(exe):
                return True
    return False


def deny(reason):
    json.dump({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }}, sys.stdout, ensure_ascii=False)
    return 0


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    cmd = str((data.get("tool_input") or {}).get("command", ""))
    if BYPASS.search(cmd):
        return 0
    is_pytest = runs_ltspice_tests(cmd, data.get("tool_name") or "Bash")
    if not is_pytest and not USES_LTSPICE.search(cmd):
        return 0
    batch = batch_processes()
    if not batch:
        return 0
    pytest_runs = [p for p in batch if owned_by_pytest(p)]
    tail = (" Arka plan işinin bitmesini bekle (Monitor/görev bildirimi). Süreç takılmışsa (.log/.raw büyümüyorsa) "
            "logunu oku ve kullanıcıya sor. Kullanıcı açıkça onaylarsa komutun başına `LTSPICE_LLM_GUARD=off ` ekle.")
    if is_pytest:
        return deny(f"Çalışan batch LTspice var: {describe(batch)}. pytest cpu_guard bütçesini paylaşmaz "
                    f"(tests/conftest.py); eşzamanlı koşum livelock'a girebilir." + tail)
    if pytest_runs:
        return deny(f"pytest'e ait batch LTspice çalışıyor: {describe(pytest_runs)}. Bu komut LTspice "
                    f"başlatabilir ve pytest'in iş parçacığı bütçesini görmez; livelock'a girebilir." + tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
