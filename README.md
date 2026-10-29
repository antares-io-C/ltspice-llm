<h1 align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo-light.svg" alt="ltspice-llm" height="72">
  </picture>
</h1>

<p align="center"><a href="README.tr.md">Türkçe</a> · <b>English</b></p>

<p align="center"><b>Let your language model run LTspice.</b></p>

[LTspice](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html) is Analog Devices'
free circuit simulator; it is built for a graphical interface, and its `.log`/`.raw` output is too large and raw for a
language model to read. **ltspice-llm** runs it in the background, reads the `.meas` measurements, the operating point
and the errors, and turns each waveform into a short JSON summary (min, max, mean, RMS and a 15-point trend). With the
ready-made tool schemas a model can build a circuit, simulate it, read the numbers and change values; the library
itself does not contact any model provider.

```text
LLM or Python ──(circuit)──► ltspice-llm ──► LTspice (in the background)
        ▲                                          │
        └──────── short summary (JSON) ◄───────────┘
```

## What you can do

- **Simulate a netlist and get the numbers back.** LTspice runs headless; `.meas` results, the operating point and
  errors come back as a Python dict. [examples/01_basic_simulation.py](examples/01_basic_simulation.py)
- **Plot waveforms.** Save `.tran` traces as PNG; the plotting tools can also return the image as Base64 for
  multimodal models. [examples/02_waveform_plotting.py](examples/02_waveform_plotting.py)
- **Give a model LTspice as tools.** 10 tool schemas in OpenAI, Anthropic, Gemini or generic format;
  `handle_tool_call` runs the model's tool calls. [examples/03_llm_function_calling_demo.py](examples/03_llm_function_calling_demo.py)
- **Run the tool loop without writing it.** `AutonomousCircuitAgent` calls your model function, runs the tools it asks
  for and stops when the model stops calling tools. The example plays the model from a script, so it needs no API key.
  [examples/08_agent_loop_with_scripted_llm.py](examples/08_agent_loop_with_scripted_llm.py)
- **Tune a value to a target.** Bisection on one parameter until a `.meas` result is within tolerance of the target.
  [examples/04_closed_loop_tuning.py](examples/04_closed_loop_tuning.py)
- **Follow a filter design through the tools.** A 10 kHz Sallen-Key low-pass: AC analysis with the −3 dB point, Bode
  plot, step response. The tool calls are scripted.
  [examples/05_filter_design_walkthrough.py](examples/05_filter_design_walkthrough.py)
- **Round to standard values, sweep temperature, diagnose a failure.** E24 rounding, a `.step temp` sweep with
  min/max/mean/std per measurement, and a diagnosis of a deliberately broken circuit.
  [examples/07_quantization_sweep_and_diagnosis.py](examples/07_quantization_sweep_and_diagnosis.py)
- **Sweep and run in parallel.** `run_sweep` runs a parameter sweep as a single LTspice `.step` run; `run_batch` runs
  independent circuits at the same time and caps the solver threads so the instances do not livelock.
  [examples/10_performance_fast_paths.py](examples/10_performance_fast_paths.py)
- **Check a formula against LTspice and write a report.** A single-file HTML report with waveforms, measurement tables
  and reliability checks. [examples/15_hotplug_damping_validation.py](examples/15_hotplug_damping_validation.py)

Also included: reading, editing and generating `.asc` schematics and finding unconnected pins
(see [docs/KILAVUZ.md](docs/KILAVUZ.md), Turkish).

## ltspice-llm or LTspice's own MCP server?

Since version 26.1.0 (August 2026) LTspice ships its own MCP server, `ltspice-mcp-bridge.exe`. It connects an AI
assistant such as Claude Code, Claude Desktop, Cursor or GitHub Copilot to the schematic you have open in LTspice: the
assistant reads and edits it, runs the simulation and reads the results while you watch, and on Windows LTspice can
set these tools up for you (*Tools > Configure Third-Party AI Assistants*; LTspice Help, "Third Party AI Tools"). If
you want to talk to an assistant about the design on your screen, use that.

ltspice-llm is for programs that drive LTspice without a window:

- Python scripts and CI jobs: parameter sweeps and parallel runs that cap the solver threads so instances do not
  livelock, closed-loop tuning, E-series rounding, convergence diagnosis and HTML reports.
- Your own agent on a model API (Anthropic, OpenAI, Gemini, or a local model), with short JSON summaries of each run
  instead of raw `.log` and `.raw` files.

## Quick start

You need Windows, Python 3.10 or newer, and LTspice installed separately
([download from Analog Devices](https://www.analog.com/en/resources/design-tools-and-calculators/ltspice-simulator.html)).

```bash
git clone https://github.com/antares-io-C/ltspice-llm.git
cd ltspice-llm
pip install -e .
```

Simulate an RC low-pass filter and read its measurements:

```python
from ltspice_llm import LTSpiceAdapter

netlist = """* RC low-pass filter
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 5m
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""

result = LTSpiceAdapter().run_netlist(netlist, circuit_name="rc_filter")
print(result.success)        # True
print(result.measurements)   # {'v_max': 3.64623451233, 'v_tau': 3.16060549511, 'v_tau_at': 0.001}
print(result.to_llm_dict())  # the short summary you pass to a model
```

`v_tau` is the output one time constant (RC = 1 ms) after the step: 5 V × (1 − 1/e) ≈ 3.16 V.

To see a model use the tools, run the agent loop with a scripted model (no API key needed):

```bash
python examples/08_agent_loop_with_scripted_llm.py
```

LTspice is found in its usual install locations; if yours is elsewhere, set `LTSPICE_EXE` to its path. The examples
print their progress in Turkish.

### Your own schematic or netlist

```python
result = LTSpiceAdapter().run_netlist(r"C:\designs\buck\buck.asc", circuit_name="buck")
```

A circuit given as a file path runs in its own folder, as it does in the LTspice GUI, so the model files next to it
(relative `.include` and `.lib`) and your own symbols are found. During the run a temporary
`_ltspice_llm_*` copy sits in that folder; afterwards the copy and the results move to the adapter's workspace, and
your file is not changed.

## Using it with an AI model

```python
from ltspice_llm import get_ltspice_tool_schemas, handle_tool_call

tools = get_ltspice_tool_schemas("anthropic")   # "openai" | "anthropic" | "gemini" | "generic"

# Give the tools to the model. When the model calls a tool, pass on its name and arguments:
result = handle_tool_call("create_or_replace_circuit",
                          {"circuit_name": "rc_filter", "netlist_code": netlist})
# result is a dict; serialize it to JSON and return it to the model as the tool result.
```

If you would rather not write the loop yourself, `AutonomousCircuitAgent` calls your model, runs the tools it asks
for and keeps going until the model is done. It does not connect to any provider and reads no API keys; you pass the
model in as a function. A complete loop with Claude (`pip install anthropic`, then set `ANTHROPIC_API_KEY`):

```python
import anthropic

from ltspice_llm import CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN, AutonomousCircuitAgent, get_ltspice_tool_schemas
from ltspice_llm.providers import from_anthropic, to_anthropic

client = anthropic.Anthropic()
tools = get_ltspice_tool_schemas("anthropic")  # built once: the tool list must not change during a conversation


def llm(history, _tools):
    system, messages = to_anthropic(history)
    response = client.beta.messages.create(
        model="claude-opus-5-5",
        max_tokens=16000,
        output_config={"effort": "high"},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",  # if Claude declines the request, the API retries it on another Claude model
        system=system,
        tools=tools,
        messages=messages,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined the request: {response.stop_details}")
    return from_anthropic(response)


agent = AutonomousCircuitAgent(system_prompt=CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN)
outcome = agent.run("Design an RC low-pass filter with a 1 kHz cutoff and check the -3 dB point in LTspice.", llm)
print(outcome["final_message"])
```

`from_anthropic` keeps Claude's thinking blocks, and `to_anthropic` sends every earlier turn back unchanged: current
Claude models tie their thinking to the conversation, and an edited history loses it or is rejected. The same module
has converters for OpenAI (`to_openai`, `from_openai`) and Gemini (`to_gemini`, `from_gemini`). To try the loop without
an API key, run [examples/08_agent_loop_with_scripted_llm.py](examples/08_agent_loop_with_scripted_llm.py), which
plays the model from a script.

The tool descriptions and the messages in tool results are in Turkish, and so is the agent's default system prompt.
`system_prompt=CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN` gives the agent the English translation of that prompt.

## Examples

All 14 runnable examples are in [examples/](examples/). There is also an interactive terminal interface:
`python interactive_designer.py`. The code comments and the detailed documentation are in Turkish.

## Project layout

```text
ltspice_llm/   the library: LTspice adapter, output parsers, LLM tools, diagnosis, reports
examples/      runnable examples
docs/          detailed user guide and measurement-backed lessons (Turkish)
tests/         tests (most of them run without LTspice)
scripts/       quality gate and test tooling
```

## Tests

```bash
pip install -e ".[dev]"
python scripts/check.py          # lint + the tests that don't need LTspice
python scripts/check.py --full   # tests and examples against a real LTspice
```

## Limitations

- Simulations have only been run on Windows with LTspice 26.1.1. The code also looks for LTspice on macOS and accepts
  a Wine wrapper on Linux through `LTSPICE_EXE`, but neither has been tested. CI runs only the tests that do not need
  LTspice (Ubuntu and Windows; Python 3.10, 3.12 and 3.14).
- The tool layer (`handle_tool_call`, `AutonomousCircuitAgent`) rejects directives in a language model's netlist
  that write files (`.wave`, `.savebias`, `savestate`) and paths that reach outside the workspace (absolute paths,
  `..`, environment variables such as `%USERPROFILE%`); library names (`.lib standard.dio`) are allowed. If the input is trusted, pass `allow_file_access=True`.
  Code that calls `LTSpiceAdapter.run_netlist` directly is not checked. The model's netlists run in the adapter's
  workspace (by default `ltspice_llm_runs` in the temp folder); to let the model use a vendor model file, put the file
  in a folder and pass `adapter=LTSpiceAdapter(workspace_dir=that_folder)` to `handle_tool_call` or
  `AutonomousCircuitAgent`.
- The provider schema formats are generated from the providers' documentation; they are not tested automatically
  against the live APIs.

## More

- [docs/KILAVUZ.md](docs/KILAVUZ.md) (Turkish): every feature, API details, performance measurements, the livelock
  guard and the full list of known limitations
- [docs/](docs/) (Turkish): measurement-backed lessons on LTspice and performance
- [CHANGELOG.md](CHANGELOG.md): release history
- [CONTRIBUTING.md](CONTRIBUTING.md): how to contribute

## License

GPL-3.0-or-later; the full text is in [LICENSE](LICENSE). Copyright © 2026 ltspice-llm contributors.

LTspice is a registered trademark of Analog Devices, Inc. This project is not affiliated with or endorsed by Analog
Devices. You need to install LTspice yourself; this repository does not distribute it.
