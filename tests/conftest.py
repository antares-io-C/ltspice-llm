"""
Ortak test ayarları.

Kilitlenme korumasının (``cpu_guard``) makine geneli kilit dosyaları testlerde gerçek bütçeyle karışmasın:
her test oturumu kendi kilit dizinini kullanır. Böylece testler, aynı anda gerçek LTspice koşturan başka
bir süreci beklemez ve onun bütçesini de tüketmez.

Hypothesis: ``dev`` profili süre sınırı koymaz (LTspice koşarken makine yüklüyken rastgele kırılmasın);
``HYPOTHESIS_PROFILE=ci`` daha çok örnek dener.
"""

import os

import pytest
from hypothesis import settings

import flowtrace
from ltspice_llm import cpu_guard

settings.register_profile("dev", deadline=None)
settings.register_profile("ci", max_examples=1000, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    """``tests/usecases/*.json``'a bağlı testin çağrı akışını kaydedip senaryoyla karşılaştırır (bkz. flowtrace)."""
    bound = flowtrace.scenario_for(item.nodeid)
    if bound is None:
        return (yield)
    use_case, scenario = bound
    with flowtrace.CallTracer() as tracer:
        result = yield
    problems = flowtrace.check(scenario, tracer.events)
    if problems:
        pytest.fail(f"Use case akışı tutmadı ({use_case['_file']}, {scenario['flow']}):\n- " + "\n- ".join(problems),
                    pytrace=False)
    return result


@pytest.fixture(autouse=True, scope="session")
def _isolated_cpu_slot_dir(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv(cpu_guard.ENV_SLOT_DIR, str(tmp_path_factory.mktemp("cpu_slots")))
    mp.delenv(cpu_guard.ENV_CPU_BUDGET, raising=False)
    yield
    mp.undo()
