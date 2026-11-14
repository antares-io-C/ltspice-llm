# User Guide

Turkish original: [KILAVUZ.md](KILAVUZ.md).

Detailed reference for ltspice-llm: features, API examples, performance measurements, livelock protection, tests and known limitations. For a short introduction see the [README](../README.md).

---

## 🚀 Features

1. **Headless Execution:**
   - Runs LTspice in the background without the graphical interface (`-b`). One simulation takes about **0.7 s** on the measured machine (LTspice 26.1.1), including the LTspice process start-up time.
   - Looks for LTspice in this order: the `executable_path` argument, the `LTSPICE_EXE` environment variable, `PATH` and known install locations (Windows, macOS).
   - On timeout it terminates only the LTspice process it started itself (and its children / process group); it does not touch other open LTspice windows.
   - The `.raw` format is **binary** by default (`raw_format="auto"`). On the measured 500 kHz synchronous buck circuit, the binary format speeds up the simulation **3-4x** and parsing **18x** compared with ASCII, and makes the `.raw` file **6x** smaller (39.2 MB → 6.8 MB); measurement values agree to a relative difference of ~1e-5. ASCII (`raw_format="ascii"`) is only needed to inspect the `.raw` by hand and does not work with `.step` (spicelib cannot read stepped ASCII).

2. **Advanced Output Parsing (Parsers):**
   - **LogParser:** With multi-encoding support (UTF-8, UTF-16, CP1252), it captures `.meas` measurements, `.op` operating points and compiler/syntax errors. Measurements that could not produce a result in any step (`Measurement "x" FAIL'ed`) are reported in the `failed_measurements` field and in `warnings`; they do not disappear silently.
   - **RawParser (Optimized for LLMs):** Compresses the data points in `.raw` files into min, max, RMS, peak-to-peak and a 15-point trend preview. In stepped files the first 3 steps are summarized separately (`"V(out) [step 2]"`).

3. **Visualization (Bode & Time Domain & Multimodal):**
   - Uses Matplotlib to produce time-domain waveforms and **dual-panel (Gain dB + Phase) Bode diagrams** as PNG or **Base64** (`plot_to_base64`, `plot_bode_to_base64`; `include_image_base64=true` in the tools). In stepped simulations the steps are overlaid on the same plot.
   - Bode works only for `.ac` analyses, and the waveform tool only for `.tran`/sweep analyses; a descriptive error is returned for the wrong analysis type.

4. **LLM Function Calling / Tool Support (10 tools):**
   - `create_or_replace_circuit`, `modify_parameters`, `plot_transient_waveforms`, `plot_bode_diagram`, `get_circuit_netlist`, `list_circuits`, `quantize_parameters`, `diagnose_circuit_issue`, `optimize_parameter`, `search_ltspice_reference`.
   - Schemas are obtained in `generic`, `openai`, `anthropic` and `gemini` formats with `get_ltspice_tool_schemas(format=...)`. The formats are produced according to the provider documentation and are not tested automatically against the live API.
   - Tool arguments can be a dictionary or JSON text (OpenAI sends arguments as text).
   - The extended set (`include_extended=True`) adds, besides the design tools, `compare_circuits` (netlist and `.meas` difference of two circuits), `export_waveforms_csv` (writes waveforms to CSV), `read_board_trace` (trace path and parasitic model from a KiCad board) `analyze_loop_stability` (phase and gain margin of a feedback loop from an `.ac` run) and `verify_specs` (PASS/FAIL/UNVERIFIED for `.meas` results against limits); details are in "LLM Agent Integration" below.

5. **Standard Component Quantization (E-Series Snapping):**
   - Rounds theoretical values (e.g.: $15.915\text{ k}\Omega, 1.414\text{ nF}$) to the **E6, E12, E24, E96** series (nearest, floor, ceil) and provides deviation analysis.

6. **SPICE Convergence Error Diagnosis Engine:**
   - On *Singular Matrix*, *Time step too small*, *Parallel Voltage Sources* and *Iteration limit reached* errors, it inspects the log and the netlist, finds the problem nodes, and offers `.options` suggestions and an automatic patch. The patch is added before the `.end ; comment` line; an option the user has already set is not overwritten.
   - Options come in the order of the ADI TROUBLESHOOTING-GUIDE. In a transient run, the first patch is the triple from the ADI example (`method=gear itl4=50 cshunt=1f`); each following diagnosis adds the next single option that is not set in the netlist (`abstol` → `gshunt` → `reltol` → `solver=alt`; `maxstep` is given explicitly according to the circuit). At the DC operating point, one at a time, from the least aggressive: `solver=alt` → `abstol` → `reltol` → `gmin` → `gshunt`. No `.options` is added for a singular matrix: according to ADI it is a topology problem, and the patch only adds a resistor to the floating node.
   - The log parser recognizes a measurement line before it searches for error words: measurement names such as `gain_error` and `temp_warning`, the `Circuit:` line and the netlist path in the "Files loaded:" list are not counted as an error or warning.

7. **Parametric & Temperature Sweep (.step):**
   - Parses real LTspice `.step` tables; computes `min`, `max`, `mean`, `std`, `range` and the number of failed steps, and adds the step parameter (`rv`, `temp`...) to each row. In `measurements`, measurements that depend on `.step` are the **mean over the steps**; per-step values are in `stepped_measurements`.

8. **Updating Parameters and Component Values:**
   - For each name, `run_netlist(parameters=...)` and `modify_parameters` do the following in order: update an existing `.param` definition; add a `.param` if the name is used as `{name}`; change the value of an **R/C/L (and plain V/I) element** with the same name. A name that matches none of these returns an error in `modify_parameters` (a change with no effect is not silently counted as `SUCCESS`).

9. **Visual Schematic (.asc) Support and the LTspice Graphical Interface (UI):**
   - Simulates real visual schematics (`.asc`) instead of text netlists, updates their parameters and generates them (`AscSchematic`). `.asc` files are read and written with LTspice's encoding (cp1252 / UTF-16LE); UTF-8 is not written (LTspice reads UTF-8 `1µ` as `1Âµ` = 1 F).
   - `adapter.inspect_schematic(path)` converts the schematic with LTspice's own netlister (`-netlist`) and reports unconnected pins (`NC_xx`) and open-ended nets. Use it to verify schematics generated by coordinate calculation. `run_sweep` converts an `.asc` schematic to a sweepable netlist the same way.
   - A circuit given by file path (`run_netlist(path)`, `run_sweep(path, ...)`, the path in `run_batch`) runs **in its own folder**, as in the LTspice interface: LTspice resolves relative `.include`/`.lib` paths from the netlist's folder, and symbols and hierarchical blocks from the schematic's folder (ADI SIMULATION-COMMANDS-REFERENCE ".INCLUDE", ".LIB" Search order; SCHEMATIC-REFERENCE "Symbol and Library Search Paths"). During the run, a temporary copy named `_ltspice_llm_<name>_<pid>_<thread>` sits in the folder; when the run ends, the copy and its outputs (`.log`, `.raw`, `.op.raw`, `.db`, the `.net` produced from the schematic) are moved to the workspace as `<circuit_name>.*`, and the source file is not touched. Previously the copy ran in the workspace: the designer's model file gave "File not found", a symbol next to the schematic was dropped from the netlist without a warning, and the run looked successful with the wrong circuit (LTspice 26.1.1; `tests/test_integration_ltspice.py`). If the folder cannot be written, the run is done in the workspace and a warning is added. The netlist text of a model (tool layer) always runs in the workspace.
   - With a single function (`adapter.open_in_gui(path)`), it opens the circuit in the LTspice graphical interface (GUI) so that you can visually inspect component symbols, wires, labels and probe measurements.

10. **ADI LTspice AI Reference Integration:**
    - The official ADI reference documents installed by LTspice 26 (`%LOCALAPPDATA%\LTspice\reference`) are read from the installation and are not copied into the repository. `ReferenceDocs().search("...")` and the `search_ltspice_reference` tool return the relevant sections. The location can be changed with `LTSPICE_REFERENCE_DIR`.
    - `find_model_definitions("LT1001")` finds the file of a model/subcircuit in the LTspice library (`.lib LTC.lib`). The missing-model diagnosis uses it to suggest a `.lib` patch.
    - The claims in the documents were verified by measurement in LTspice 26.1.1. The results and the bugs fixed along the way (a failed DC operating point looking like SUCCESS, `.asc` µ loss, the result of `WHEN …=1/sqrt(2)` being read as 1.0, etc.) are in `docs/LESSONS_LEARNED_LTSPICE_REFERENCE.md`. To reproduce: `python examples/12_ltspice_reference_checks.py` (~15 s).
    - `.four` results (`fourier`: THD %, harmonics), `convreport` difficulty scores (`convergence`) and LTspice's `.meas` SQLite database (`db_path`, `read_measure_db`) were added to the results.

11. **Simulation Report (`SimulationReport`):**
    - Produces a single-file HTML report from simulation results. The report contains the question and method of each test, waveforms (inline SVG, value readout with the cursor), measurement tables, component effects (before → after, from single-variable variant pairs), new effects, run reliability (thread count and method in the log, warnings, Gear cross-check) and assumptions. Every number in the report comes from `.meas`/`.raw`; it is readable in light and dark themes and does not need LTspice.
    - Example: `python examples/15_hotplug_damping_validation.py` → `reports/15_hotplug_damping_validation.html`. By project rule, every simulation process ends with such a report (GEMINI.md).

---

## 📁 Project Structure

```text
ltspice-llm/
├── ltspice_llm/                  # Core Python package
│   ├── __init__.py               # Exported modules and __version__
│   ├── adapter.py                # LTspice execution engine, .asc/.net support, open_in_gui
│   ├── cpu_guard.py              # Machine-wide solver-thread budget for concurrent LTspice instances
│   ├── schematic.py              # LTspice .asc schematic builder (AscSchematic) and pin transforms
│   ├── symbols.py                # .asy symbol pins (get_pin for parts in the ADI library)
│   ├── netlist.py                # Netlist text helpers that do not need LTspice (.end, .step, analysis regexes)
│   ├── parser.py                 # .log (including .step tables) and .raw parsers
│   ├── visualizer.py             # Matplotlib waveform & Bode plotter (PNG / Base64)
│   ├── tools.py                  # LLM Tool / Function Calling interfaces and schema formats
│   ├── quantizer.py              # E-series component quantization engine
│   ├── parasitics.py             # PCB trace, MLCC DC-bias, cable inductance and hot-plug analysis (sourced constants)
│   ├── board.py                  # KiCad board reader: trace path between two pads and stackup (read-only)
│   ├── diagnostics.py            # SPICE error and convergence diagnosis advisor (ADI troubleshooting order)
│   ├── reference.py              # ADI LTspice AI Reference search and model library index
│   ├── report.py                 # Simulation report (HTML): questions, waveforms, component effects, reliability
│   ├── agent.py                  # Closed-loop optimization (CircuitOptimizer)
│   ├── compare.py                # Netlist and .meas difference of two circuits (single-variable check)
│   ├── export.py                 # Writes .raw waveforms to CSV
│   ├── providers.py              # Anthropic / OpenAI / Gemini message format converters
│   ├── circuit_agent.py          # LLM tool loop, conversation history and tool log
│   └── system_prompts.py         # System prompt of the AI Hardware Engineer
├── examples/                     # Example scenarios (01-19)
│   ├── 01_basic_simulation.py             # basic simulation and .meas measurements
│   ├── 02_waveform_plotting.py            # waveform plot (PNG / multimodal LLM image)
│   ├── 03_llm_function_calling_demo.py    # LLM tool call (tool / function calling)
│   ├── 04_closed_loop_tuning.py           # closed-loop parameter optimization
│   ├── 05_filter_design_walkthrough.py    # filter design with tool calls (calls are fixed in the script)
│   ├── 07_quantization_sweep_and_diagnosis.py  # E-series quantization, temperature sweep, diagnosis
│   ├── 08_agent_loop_with_scripted_llm.py # provider-independent tool-calling loop
│   ├── 10_performance_fast_paths.py       # signal pruning, run_sweep, run_batch measurements
│   ├── 12_ltspice_reference_checks.py     # verifying ADI reference claims and fixes by measurement
│   ├── 14_record_flow_fixtures.py         # real LTspice recordings that the use case tests replay
│   ├── 15_hotplug_damping_validation.py   # hot-plug tool claims measured with LTspice + report
│   ├── 16_process_and_memory_hardening.py # Crash, orphan process, disk, .raw memory, .asy pins: measurement + report
│   ├── 17_gemini_review_3.py              # claims of the third Gemini review, measured + report
│   ├── 18_gemini_review_4.py              # claims of the fourth Gemini review, measured + report
│   ├── 19_simulation_to_slides.py         # RC step response -> report, GIF and .pptx (agent skills)
│   ├── 20_kicad_board_checks.py           # compares the board reader with KiCad's own computation (needs KiCad)
│   ├── 21_board_trace_to_simulation.py    # board trace path in LTspice: DC drop, load step + report
│   ├── kicad_oracle.py                    # runs in KiCad's Python: builds the test boards, records KiCad's computation
│   └── study_helpers.py                   # report path and registration
├── docs/
│   ├── KILAVUZ.md                         # this document: detailed user guide
│   ├── LESSONS_LEARNED_PERFORMANCE.md     # lessons and checklist from the performance/livelock work
│   ├── LESSONS_LEARNED_LTSPICE_REFERENCE.md  # ADI LTspice AI Reference: confirmed/refuted claims, bugs found
│   ├── LESSONS_LEARNED_KICAD.md           # comparison of the board reader with KiCad, pitfalls of KiCad's Python
│   ├── MISSION_BRIEF_TEMPLATE.md # Template for a design/verification job: .meas thresholds, autonomy envelope, evidence
│   └── references/               # App note / datasheet catalog and downloader (PDFs are not committed to git)
├── scripts/
│   ├── check.py                  # Single quality gate (human, CI, Claude Code Stop hook)
│   ├── flows.py                  # Coverage witness for use case scenarios
│   ├── isolation.py              # Runs each test alone and finds tests that depend on each other
│   └── mutants.py                # Measures test strength with hand-picked mutants
├── tests/                        # Tests, Hypothesis property tests, real LTspice log samples (tests/data/)
├── .github/workflows/tests.yml   # CI: scripts/check.py (tests that do not need LTspice)
├── interactive_designer.py       # Interactive terminal interface (CLI, open the GUI with [G])
├── pyproject.toml                # Package configuration (version: ltspice_llm.__version__)
├── CHANGELOG.md
└── README.md
```

---

## 🛠️ Quick Start

### 1. Installation

```bash
pip install -e ".[dev]"
```

The editable install points to **the folder you are in**; if you move the project, run it again (otherwise the package cannot be loaded with `import` from another folder). If LTspice is somewhere else, use the `LTSPICE_EXE` environment variable or the `LTSpiceAdapter(executable_path=...)` argument.

### 2. Starting the Interactive Designer (CLI)

```bash
python interactive_designer.py     # or after installation: ltspice-designer
```

### 3. Basic Simulation Example

```python
from ltspice_llm import LTSpiceAdapter

adapter = LTSpiceAdapter()

netlist = """* RC Low-Pass Filter
V1 in 0 PULSE(0 5 0 1n 1n 1m 2m)
R1 in out 1k
C1 out 0 1u
.tran 5m
.meas tran v_max MAX V(out)
.meas tran v_tau FIND V(out) WHEN time=1m
.end
"""

result = adapter.run_netlist(netlist, circuit_name="rc_filter")

print(f"Simülasyon Başarılı: {result.success}")
print("Ölçümler (.meas):", result.measurements)
# Output: {'v_max': 3.646, 'v_tau': 3.1606, 'v_tau_at': 0.001}

# JSON that can be given directly to the LLM:
print(result.to_llm_dict())
```

`status` values: `SUCCESS` (the operation succeeded), `FAILED` (the simulation ran but failed; details are in `errors` and `diagnostics`), `NOT_CONVERGED` (the optimization could not reach the target), `ERROR` (the tool call could not be executed: missing argument, circuit not found, etc.). The LLM must also read the `warnings`, `failed_measurements` and `parameter_changes` fields in the response sent to it.

---

## 🤖 LLM Agent Integration (Function Calling)

```python
from ltspice_llm import get_ltspice_tool_schemas, handle_tool_call

tools = get_ltspice_tool_schemas("openai")      # "generic" | "openai" | "anthropic" | "gemini"

# When the LLM produces a tool call, pass it to the adapter (arguments can be a dictionary or JSON text):
response = handle_tool_call(
    "create_or_replace_circuit",
    {"circuit_name": "voltage_divider", "netlist_code": "...", "parameters": {"R1_VAL": "10k"}},
)
# The returned JSON is passed directly to the LLM as a ToolMessage.
```

### Provider-independent tool loop

`AutonomousCircuitAgent` does not connect to any LLM provider itself and does not read an API key. You supply the model call as a function; the agent executes the tool calls, appends the results to the history, and loops until the model stops calling tools:

```python
from ltspice_llm import AutonomousCircuitAgent

def llm(messages, tools):
    ...  # call your own client; return {"content": str|None, "tool_calls": [{"id", "name", "arguments"}]}

agent = AutonomousCircuitAgent()
outcome = agent.run("10 kHz alçak geçiren filtre tasarla", llm, max_steps=10)
# {"finished": True, "steps": 4, "final_message": "..."}
agent.tool_log          # name, arguments, status and duration of every tool executed
agent.get_history()     # provider-independent message history (add_user_message / add_assistant_message / add_tool_result)
```

For a working example see [examples/08_agent_loop_with_scripted_llm.py](../examples/08_agent_loop_with_scripted_llm.py).

To convert the history to a provider format, use `ltspice_llm.providers`: `to_anthropic` (`system`, `messages`), `to_openai` (`messages`), `to_gemini` (`system_instruction`, `contents`); to convert the response to the agent format, use `from_anthropic`, `from_openai`, `from_gemini` (dictionary or SDK object). The `image_base64` of the plot tools does not enter the history as text; it stays in the `images` field of the message, and the converters turn it into the provider's image block (Anthropic `image`, OpenAI `image_url` after the tool messages, Gemini `inlineData`). The formats follow the provider documentation; they are not tested automatically with the live API.

For a complete loop with Claude (model ID, `effort`, `fallbacks` for a refused request) see [README.md, "Using it with an AI model"](../README.md#using-it-with-an-ai-model). Points to watch:

- On Claude Opus 5.5 and Fable 5.1, thinking is always on and thinking blocks are tied to the conversation ("preserved thinking"): every request must carry the earlier turns unchanged; a request with an edited history gets a 400 on accounts opened after 2026-08-31 (Anthropic Claude API documentation). `from_anthropic` stores a turn that has blocks other than text and tool calls (`thinking`, `fallback` ...) as they are in the `anthropic_content` field, the agent keeps this field in the history, and `to_anthropic` sends the turn back as it is. Previously the thinking blocks were dropped on every turn: the request is not rejected, but the model cannot see the reasoning of the earlier turns (the documentation treats this as a one-time recovery, not a lasting pattern).
- With `fallbacks`, if Claude refuses the request, the API retries it on another Claude model; the response has a `fallback` block. In a streamed response, if the model changes in the middle of the output, `from_anthropic` does not send back the thinking, redacted_thinking and tool_use blocks before the last `fallback` block, and does not give the agent the tool call before the boundary; the text and everything after the boundary go back as they are (Anthropic documentation, "Echoing fallback turns back"). In a non-streamed response the refused partial output never arrives.
- Build the `tools` list once: a tool list that changes in the middle of a conversation also invalidates the earlier thinking blocks.
- The agent's default system prompt is Turkish; `AutonomousCircuitAgent(system_prompt=CIRCUIT_DESIGNER_SYSTEM_PROMPT_EN)` gives its English translation. Tool descriptions and the messages in tool results are Turkish regardless of the prompt.
- For the extended tools, create the agent with `include_extended=True` and give the API `get_ltspice_tool_schemas("anthropic", include_extended=True)`.

Extended tools (`include_extended=True`): schematic drawing, trace parasitics, hot-plug, SOA/derating, Monte Carlo, `optimize_parameters`, which tunes several parameters toward several `.meas` targets with Nelder-Mead, `compare_circuits`, which compares two circuits, `export_waveforms_csv`, which writes waveforms to a file, `read_board_trace`, which reads a trace path from a KiCad board, `analyze_loop_stability`, which gives the phase and gain margin of a feedback loop, and `verify_specs`, which checks `.meas` results against limits.

### Comparing two circuits

A measurement difference can be attributed to a variable only if that variable is the single thing that changed between the two circuits. The `compare_circuits` tool gives the netlist difference of two simulated circuits in the workspace and the difference of their common `.meas` results. From Python, `compare_netlists` and `compare_measurements` do the same.

```python
from ltspice_llm import handle_tool_call

handle_tool_call("create_or_replace_circuit", {"circuit_name": "before", "netlist_code": netlist})
handle_tool_call("create_or_replace_circuit",
                 {"circuit_name": "after", "netlist_code": netlist.replace("R1 in out 1k", "R1 in out 2k")})
result = handle_tool_call("compare_circuits", {"circuit_a": "before", "circuit_b": "after"})
# result["changes"]: [{"kind": "element", "name": "R1", "a": "R1 in out 1k", "b": "R1 in out 2k"}]
# result["single_variable"]: True
# result["measurements"]: a, b, delta, relative_change_percent and same_definition for each common measurement
```

- `changes`: the variables that changed (`element`, `param`, `option`, `analysis`, `model`, `directive`). The title, comments, whitespace, line order, letter case, anything after `.end`, `.save` and the `.options threads` line added by the adapter are not differences. R, C and L values and `.param` and `.options` values are compared as numbers (`1k` = `1000`; `1M` is milli, `1Meg` is mega); the other fields are compared as text. `GND` is the same node as `0` and `µ` the same suffix as `u`. Unnamed directives (`.ic`, `.lib`, ...) are compared line by line: their order is not a difference, but a line that appears twice counts twice.
- `single_variable`: `true` if exactly one variable changed. If several variables changed, or the netlists are the same, `warnings` says so.
- `measurements`: the common measurements. `delta = b - a`, `relative_change_percent = (b - a) / |a| * 100`; the percentage is `null` when `a` is zero. Values are not rounded. If `same_definition` is `false`, the `.meas` definition of the measurement differs between the two circuits and the two values do not measure the same thing. The lists keep the order of the logs: `measurements` and `only_in_a` that of circuit a, `only_in_b` that of circuit b.
- A stepped (`.step`) run is not compared: its measurements are the mean over the steps. Run each case to be compared under its own `circuit_name`.

### Exporting waveforms to CSV

```python
from ltspice_llm import LTSpiceAdapter, export_waveforms_csv

run = LTSpiceAdapter().run_netlist(netlist, circuit_name="rc", treat_as_text=True)
info = export_waveforms_csv(run.raw_path, "rc.csv", ["V(out)"])
# info: {"csv_path": "rc.csv", "rows": ..., "columns": ["time", "V(out)"], "steps": 1, "analysis": "tran"}
export_waveforms_csv(run.raw_path, "rc_comma.csv", delimiter=";", decimal=",")  # locale that uses a decimal comma
```

- The first column is the X axis (`time`, `frequency` or the name of the `.dc` source); an `.op` result has no axis column. In a `.tran` with Tstart greater than zero the time is absolute.
- Values are not rounded: each cell is the shortest text that reads back as the same number in the type it was stored in (in LTspice's default `.raw`, traces are float32 and time is float64).
- `.ac` traces are written to the columns `re(V(out))` and `im(V(out))`. In a stepped run the first column is `step` (it starts at 1, as in the LTspice log).
- The `export_waveforms_csv` tool writes the file into the workspace as `<circuit_name>.csv` and returns only the file path, the row count and the column names (the data does not go to the model); it also accepts `"tab"` for `delimiter`. `remove_circuit` and `cleanup_workspace` delete this file too.

### Phase and gain margin of a feedback loop

Break the loop with an AC source in series (Middlebrook voltage injection): in `Vinj x y AC 1`, `x` is the node the source drives into the loop and `y` is the node where the loop comes back. Put the source between a low-impedance output and a high-impedance input; otherwise the measured ratio is not the loop gain. Run an `.ac` analysis that saves both nodes.

```python
from ltspice_llm import LTSpiceAdapter, raw_loop_margins, stability_margins

run = LTSpiceAdapter().run_netlist(netlist, circuit_name="loop", treat_as_text=True)
info = raw_loop_margins(run.raw_path, loop_input="x", loop_output="y")
# info: {"loop_gain": "-V(y)/V(x)", "steps": [{"step": 1, "crossover_hz": ..., "phase_margin_deg": ...,
#        "phase_crossover_hz": ..., "gain_margin_db": ..., "gain_crossovers": [...], "phase_crossovers": [...],
#        "warnings": [...]}]}
margins = stability_margins(freq_hz, loop_gain)  # the same calculation on your own complex T(jω) samples
```

- The loop gain is `T = -V(y)/V(x)`. The gain crossover is where `|T| = 1`, and the phase margin is `180° + ∠T` there, in (-180°, 180°]. The phase crossover is where `T` crosses the negative real axis, and the gain margin is `-20·log10|T|` there. dB and phase are interpolated linearly in log f.
- Every crossing is listed. The headline values are the pessimistic ones: the smallest phase margin and the gain margin closest to 0 dB. If `|T|` crosses 1 more than once, `warnings` says that the margins alone do not show stability (look at the Nyquist plot). If there is no crossing in the swept range, the value is `null` and `warnings` says why.
- A stepped run gives one entry per step (starting at 1, as in the CSV export); `params` holds that step's parameter values from the `.log`, in the order LTspice ran them (for example `{"kloop": 4.0}`).
- The `analyze_loop_stability` tool does the same for a model (extended set). The tests check it against an LTspice run of a three-pole loop whose margins are known in closed form.
- End-to-end example: [examples/22_loop_stability_capacitive_load.py](../examples/22_loop_stability_capacitive_load.py) measures how a capacitive load lowers the phase margin of an op-amp follower and how an isolation resistor restores it, and checks the result against the overshoot of the step response.

### Checking results against specs

Whether a circuit works is decided by limits written down in advance, not by reading the numbers. `check_specs` compares `.meas` results with `min`/`max` limits; the `verify_specs` tool does the same for a model from a simulated circuit's log (extended set).

```python
from ltspice_llm import check_specs

specs = {"v_out": {"min": 3.2, "max": 3.4, "uncertainty": 0.01, "source": "<requirement or datasheet page>"},
         "t_settle": {"max": "1m", "source": "<requirement or datasheet page>"}}
report = check_specs([run.measurements], specs)
# report: {"verdict": ..., "passed": ..., "failed": ..., "unverified": ..., "steps": 1, "warnings": [],
#          "results": [{"name": "v_out", "status": ..., "min": 3.2, "max": 3.4, "source": "...", "worst_value": ...,
#          "worst_step": 1, "margin": ..., "uncertainty": 0.01, "failed_steps": []}, ...]}
```

- The limits are inclusive. `margin` is the distance to the nearest limit: zero or more inside, negative by the size of the violation outside.
- In a stepped run every step is checked and the worst step is reported: the run's `measurements` are the mean over the steps, which would hide a step that breaks a limit.
- A measurement that is not in the log, or that LTspice could not compute (`FAIL'ed`), is `UNVERIFIED`, and then the verdict cannot be `PASS`. The verdict is `FAIL` if any spec failed, otherwise `UNVERIFIED` if any spec could not be checked.
- `uncertainty` (in the unit of the measurement) is a guard band on both sides of each limit, following JCGM 106:2012 (8.3.2 guarded acceptance, 8.3.3 guarded rejection; local copy in `docs/references`, `jcgm-106-2012`): a result closer to the limit than the uncertainty is neither accepted nor rejected but `UNVERIFIED`. Without an uncertainty the decision is the plain comparison.
- `source` says where the limit comes from (requirement, datasheet page); a spec without one is listed in `warnings`.
- With `numerical_check` the tool runs the circuit twice more, with `reltol=1e-4` and with Gear, compression off (`plotwinsize=0`), and adds the largest change of each measurement to its guard band. `reltol` is not an accuracy guarantee, so the effect of the solver settings is measured, not assumed. If a rerun fails, or Gear does not show in its log, the spec cannot be verified.

### Reading a trace path from a KiCad board

The length of the copper trace between two parts is not typed in by hand; it is read from the KiCad board file. `read_kicad_board` only reads the file and does not need KiCad installed; `trace_path` finds the shortest trace path between two pads; `parasitics` turns each piece into R/L/C with the dielectric thickness, relative permittivity and copper thickness from the board's stackup. The `read_board_trace` tool lets a model do the same (extended set).

```python
from ltspice_llm import inject_board_path_into_netlist, read_kicad_board

board = read_kicad_board("kart.kicad_pcb")
path = board.trace_path("U3.2", "C5.1")  # two pads on the same net, as "REF.PAD"
model = path.parasitics()                # H, er and copper thickness from the board's stackup
netlist = inject_board_path_into_netlist(netlist, "vreg", "vdd", model)
# path.to_dict(): per piece layer, width, length, coverage by the return plane under the trace, layer changes
# board.summary(): file name, SHA-256, revision, stackup (goes into the report)
```

- The path is the shortest trace path between the two pads, not the total track length of the net.
- If the connection runs only through a copper pour, the path is on an inner layer (stripline) or the connection is not routed, `BoardPathError` is raised: no length is made up.
- The microstrip equation assumes an unbroken return plane under the trace. Each piece's `plane_coverage` says how much of the piece the filled pour on the neighbouring copper layer covers.
- Vias are counted but not modelled (ideal short); their values can be given as inputs with `parasitics(via_inductance_h=..., via_resistance_ohm=...)`.
- The tool looks for the board in the workspace; an absolute path or `..` is read only with `allow_file_access=True`. The contents of a file that cannot be read are not echoed in the error message.
- The reader was compared with KiCad 10.0.3's own computation (`examples/20_kicad_board_checks.py`): track lengths, pad positions and connected pad groups came out the same. Measurements and the pitfalls of KiCad's Python: `docs/LESSONS_LEARNED_KICAD.md`.
- End-to-end example: `examples/21_board_trace_to_simulation.py` adds the path to a supply circuit, measures in LTspice the DC drop and the dip at a load step against an ideal wire, and writes a report; `--kart` and `--yol` run it on your own board.

---

## 📈 Autonomous Closed-Loop Optimization

```python
from ltspice_llm import CircuitOptimizer

res = CircuitOptimizer().optimize_single_parameter(
    base_netlist=netlist,
    circuit_name="rc_opt",
    param_name="R_VAL",          # .param name or R/C/L element name
    target_meas_name="v_target",
    target_value=3.50,
    min_param=100.0,
    max_param=5000.0,
    tolerance=0.005,             # 0.5% error tolerance
)

print(res.converged, res.best_parameters, res.best_value)
print(res.message)               # the reason if it did not converge (endpoints have the same value, target outside the range, simulation failed...)
# Example 04: R_VAL = 827.34 Ω, Measured: 3.5071 V
```

The same function is offered to the LLM as the `optimize_parameter` tool. The measurement is assumed to change **monotonically** with the parameter; a single parameter is optimized. The temporary files produced per step (`.net/.log/.raw/.db`) are deleted by default when the job ends (kept with `cleanup_intermediate=False`) and every run uses unique names.

---

## 🧹 Workspace and Concurrency

By default, files are written under `%TEMP%/ltspice_llm_runs` with fixed names derived from `circuit_name` (LTspice 26 also leaves one `.db` cache file per circuit). To clean up:

```python
adapter.remove_circuit("rc_filter")            # all files of a single circuit
adapter.cleanup_workspace(max_age_seconds=86400)   # outputs older than 1 day (0: all), including run_batch's _batch_* directories
adapter.cleanup_workspace(max_total_mb=500)        # shrink to 500 MB, starting from the oldest runs
LTSpiceAdapter(workspace_quota_mb=500)             # automatically after every run (off by default)
```

The quota does not delete the files of the `SimulationResult`s you are still holding. It is off by default because the plot tools later read the `.raw` by `circuit_name`. Measurement: the default workspace without a quota grew to 3.16 GB in 9 days (99% of it `.raw`).

On Windows, LTspice processes are in a job object tied to the Python process: however Python ends, LTspice ends too, a crashing LTspice does not wait for Windows Error Reporting, and the crash code is reported as an error
(see [docs/LESSONS_LEARNED_PERFORMANCE.md §10](LESSONS_LEARNED_PERFORMANCE.md)).

Two calls that use the same `circuit_name` at the same time in the same `workspace_dir` overwrite each other's files; for parallel workflows give each its own `workspace_dir`. `adapter.run_batch(...)` does this itself: it opens a separate subdirectory for each job.

Running LTspice several times at the same time (from threads, with `run_batch`, from several agents or separate Python processes) is **safe**: the livelock protection is on by default and limits the solver threads of all runs to the core count machine-wide (see [Performance → Livelock protection](#-performance)).

---

## ⚡ Performance

> What was learned in this work, the root-cause analysis and a checklist for future performance work:
> [docs/LESSONS_LEARNED_PERFORMANCE.md](LESSONS_LEARNED_PERFORMANCE.md).

All the numbers below were **measured** with the code in this repository, on LTspice 26.1.1 and Windows 11.
Machine: Intel Core i7-1255U (2 P + 8 E cores, 12 logical processors, 15 W), "Balanced" power plan, AC power.
Reference circuit: 500 kHz synchronous buck converter, 2 ms `.tran`, 20 ns maximum step. Parallelism
gains depend heavily on the processor; higher gains are expected on desktop/workstation processors.

### What actually speeds things up

| Intervention | Measured gain | Status |
| --- | --- | --- |
| **Binary `.raw`** (default) | simulation **3.2x**, parsing **18x**, file **5.8x** smaller | Automatic |
| **Signal pruning with `.save`** | `.raw` **2.5x** smaller, parsing **2.4x** | `save_signals=[...]` |
| **`run_sweep`** (LTspice `.step`, single process) | **1.4-1.9x** on an 8-point sweep | `adapter.run_sweep(...)` |
| **`run_batch`** (solver threads limited automatically) | **2.0-2.4x** on 12 independent circuits | `adapter.run_batch(...)` |

End to end: a real optimization loop of 9 iterations went from **20.62 s / 176.9 MB** to
**7.76 s / 6.2 MB** (**2.66x faster, 28.8x less disk**); the optimum parameter found stayed the same.

```python
# Parameter sweep: LTspice's own .step runs in a single process
res = adapter.run_sweep(netlist, parameter="Lval", values=["10u", "22u", "47u"],
                        save_signals=["V(vout)"])
print(res.step_statistics["vout_pp"])   # min / max / mean / std / range

# Independent circuits: concurrent instances, solver threads limited automatically
results = adapter.run_batch([
    dict(netlist_content_or_path=net_a, circuit_name="a", treat_as_text=True),
    dict(netlist_content_or_path=net_b, circuit_name="b", treat_as_text=True),
])
```

### Why did parallel LTspice livelock?

**Root cause:** The LTspice 26 solver (`Multi-threaded, self-authoring solver`) by default opens as many threads as
**all logical cores** (log: `Maximum thread count: 12`). This number ignores the process
affinity and the other load on the machine. When several LTspice instances run at the same time and these
threads cannot find cores at the same time, the solver **spins burning CPU without making progress** (livelock): one instance
pinned to 3 cores used 176 CPU-seconds in 60 s and did not advance its log by even one line.

**Fix:** the undocumented `.options threads=N` directive limits the solver's thread count
(`.options numthreads` / `nthreads` is a syntax error; `OMP_NUM_THREADS` has no effect). `run_batch` adds
`.options threads=cores//instances` to each job. A causality test in which this directive is the only difference:

| Configuration (normally ~1 s per job) | `threads` default (12) | `threads` limited |
| --- | --- | --- |
| 8 free instances | **1/24 succeeded**, 23 killed at 40 s | **24/24**, 8 jobs ~3.6 s (`threads=1`) |
| 4 instances, each pinned to its own 3 cores | **0/12 succeeded** | **12/12** (`threads=3`) |
| `run_batch` 12 jobs, 8 instances | 5/12 succeeded, 0.27x | **36/36**, 2.1x |

**The ceiling of the gain is the hardware.** The i7-1255U is a hybrid processor: the same solver job runs
**1.95x** slower on an E-core than on a P-core, and all cores share a common 15 W budget (with 10 cores loaded, the P-core
solver time went from 0.53 → 0.73-0.85 s). Even pinning each job to a separate physical core
only raises the theoretical ceiling to ~3.3x; the 2.0-2.4x of `run_batch` is close to that.

| 12-job load | 4 instances × 3 threads | 6 × 2 | 10 × 1 | 12 × 1 |
| --- | --- | --- | --- | --- |
| Speedup (worst repeat) | **2.39x** | 2.38x | 1.79x | 1.93x |

> For a parameter sweep of a single circuit, use **`run_sweep`**, not `run_batch`: LTspice's own
> `.step` runs in a single process.

### Livelock protection

This livelock occurs not only in `run_batch` but in **every case** where LTspice runs more than once at the same time:
calling `run_netlist` from your own threads, several agents, separate Python processes. So the protection is in the adapter itself and is on by default (`cpu_guard=True`):

1. **Machine-wide core budget.** Before each run starts, it leases cores from the budget and
   `.options threads=<leased>` is given to LTspice. The budget is held with one operating-system file lock per core;
   so the limit also applies across different Python processes. Even if a process crashes or is killed, the locks
   are released by the operating system and the budget does not stay stuck.
2. **Sized to the machine.** The budget is the number of cores the process can actually use (the affinity mask is
   read; LTspice does not do this itself). With a value such as `LTSPICE_LLM_CPU_BUDGET=6`, the total number of cores this software
   allocates to LTspice can be reduced.
3. **Fair sharing.** A run that is alone gets all the cores (same speed as the LTspice default). A run that has to
   wait its turn asks for a `cores/4` share instead of grabbing the machine; so under contention the runs proceed in parallel
   safely. If the budget is full, a new run waits instead of oversubscribing
   (`slot_wait_timeout`, default `max(60, 2 × timeout)` s; when the time runs out, it returns with a clear error).
4. **Stall watchdog.** For loads the protection cannot see (a simulation open in the LTspice interface, other
   tools): if the solver's `.log`/`.raw` files make no progress at all for `stall_timeout` (default 20 s), the run is
   stopped and run once more with `threads=1`. In normal runs these files grow at sub-second
   intervals; in a livelock they never change.

If the user gives `.options threads=N` in the netlist, this value is not overridden; the run waits until N cores (at most as many as the process can use)
are free. LTspice clips N to the machine's logical core count; `SimulationResult.solver_threads`
reports the number LTspice used.
The line added by the adapter is marked with `; ltspice_llm:auto-threads`, is updated in the next run, and
is not shown in the `get_circuit_netlist` output.

Measurement with real LTspice (2 ms buck, normally ~1 s per job, timeout 40 s):

| Scenario | Protection off | Protection on (default) |
| --- | --- | --- |
| 8 threads, each running `run_netlist` with its own adapter | **1/8 succeeded**, 43 s | **8/8**, 3.9-6.1 s (sequential: 6.7 s) |
| 4 separate Python processes × 3 concurrent runs | **0/12 succeeded**, 45 s | **12/12**, ~10 s |
| Watchdog only (protection off, `stall_timeout=5`) | — | **8/8**; 8 stalls caught and recovered with `threads=1` |
| Single-run extra cost | 1.101 s | 1.080 s |

```python
LTSpiceAdapter()                                  # protection + watchdog on (recommended)
LTSpiceAdapter(stall_timeout=120)                 # very large circuits with a long operating point
LTSpiceAdapter(cpu_guard=False, stall_timeout=None)   # LTspice default behavior (not recommended)
```

### `.options` settings that do not help

Some commonly recommended SPICE "speed-up" options gave no gain when measured on this circuit:

| Option | Claim | Measured |
| --- | --- | --- |
| `.options method=gear` | 2-3x speed | **1.07x** (noise level); slightly damps the resonance amplitude at a coarse step |
| `.options plotwinsize=0` | Turns off compression and speeds things up | **1.02x**, `.raw` size did not change |
| `.options reltol=0.005 trtol=7` | 2x "without hurting accuracy" | **1.10x**, and it also changes the measurement values |

`method=gear` is still valuable for **convergence** problems (the diagnosis engine suggests it for this purpose) — but it is not a speed
setting.

### Why can't the optimization loop be parallelized?

`CircuitOptimizer.optimize_single_parameter` does a **binary search**: which point each step will try depends on the
result of the previous step. So the iterations cannot be distributed to a process pool. Instead, the loop
prunes everything except the `.meas` signals with `.save` in each iteration (`prune_signals=True`); if you need to try independent
points at the same time, do a coarse sweep with `run_sweep` and narrow the range.

---

## 🧪 Running the Tests

```bash
python -m pip install -e ".[dev]"          # pytest, hypothesis, pytest-timeout, ruff
python scripts/check.py                    # quality gate: .asc integrity + ruff + tests without LTspice (CI runs this too)
python scripts/check.py --full             # same gate, tests with LTspice (examples included)
python -m pytest                           # all tests; those that depend on LTspice are skipped if it is not installed
python -m pytest -m "not examples"         # tests with LTspice, without running the examples end to end
python scripts/mutants.py                  # test strength: all hand-picked mutants must die
python scripts/flows.py [--full]           # use case scenarios: coverage witness (equivalent of gcov --gcov-check)
python scripts/flows.py --uncovered        # functions that no test enters
python scripts/isolation.py [hedef ...]    # each test alone: finds tests that depend on the state of a previous test
```

Warnings count as errors and a single test is cut off at 120 s (`pyproject.toml`). `tests/test_properties.py` contains the Hypothesis
property tests; with `HYPOTHESIS_PROFILE=ci` it tries 1000 examples.

| File | Does it need LTspice? |
| --- | --- |
| `test_integration_ltspice.py`, `test_integration_reference.py`, `test_adapter.py` | Yes (skipped if it is not installed) |
| `test_examples.py` | Partly: import and report path work without LTspice; running the examples end to end (the `examples` marker) needs LTspice |
| Others (`test_real_logs.py`, `test_parameters.py`, `test_tools_and_agent.py`, `test_visualizer_steps.py`, `test_adapter_platform.py`, `test_interactive_designer.py`, `test_regressions.py`, ...) | No |

**Use case flow.** `tests/usecases/*.json` derives the actions of netlist simulation, sweep/batch runs and the agent loop
from the documents (this guide, GEMINI.md) and ties each scenario to a test. While the test runs, `tests/flowtrace.py`
(`sys.setprofile`, the equivalent of gdb's non-stopping breakpoints) records the `ltspice_llm` calls and checks the sequence,
the forbidden calls and the call counts. For debugging, use `pytest --pdb`, `--trace` or
`breakpoint()` instead of gdb.

The logs under `tests/data/` are real LTspice 26.1.1 outputs; they test the parser against real output, not synthetic formats.

---

## ⚠️ Known Limitations

- `optimize_single_parameter` is for a single parameter and a monotonic measurement; since binary search is sequential, its iterations cannot be parallelized. For several parameters and targets use `optimize_multi_parameters` (Nelder-Mead).
- The parallelism measurements were made on a single machine (i7-1255U, hybrid 2P+8E); the gain depends heavily on the processor. `.options threads=N` is an undocumented directive in LTspice; it was verified on LTspice 26.1.1, and its behavior may change in other versions.
- The livelock protection coordinates only the runs started through this library (same user, same `LTSPICE_LLM_SLOT_DIR`). Simulations started by hand in the LTspice interface, or LTspice runs of other tools, are not included in the budget; only the stall watchdog is active against them. The thread count of an LTspice window opened with `open_in_gui` also cannot be controlled.
- The stall watchdog senses progress from file growth. A very large circuit whose operating point calculation takes longer than `stall_timeout` may be wrongly considered stalled and re-run with `threads=1` (the result is still correct, it is only slower); for such circuits, increase `stall_timeout`.
- When pruning with `save_signals`, signals that appear in `.meas` lines are kept automatically, but if you later want to plot a signal outside `.meas`, you have to re-run the simulation.
- The unit of AC `.meas` results depends on `.options meascplxfmt`: in the default (bode) format `mag()` returns dB, in polar/cartesian it is linear. For the exact value independent of the format, use `read_measure_db(result.db_path)`.
- `inspect_schematic`/`schematic_to_netlist` write a temporary copy of the schematic into its own folder to be able to find local symbols, and delete it immediately (the `-I<path>` switch does not work in LTspice 26.1.1). If the folder cannot be written, the copy is converted in the workspace and only standard symbols are resolved.
- In a headless run (`-b`), LTspice 26.1.1 skips a symbol it cannot find without a warning: that element does not enter the netlist, there is no trace in the log, and the run looks successful. For this reason a schematic given by file path runs in its own folder; if the folder cannot be written and the run falls back to the workspace, or the symbol really does not exist, the result belongs to the wrong circuit. If in doubt, use `inspect_schematic`: in the same situation it does not produce a netlist and gives an error.
- When `.op` and `.step` are used together, LTspice writes `.meas` once on the step axis, not per step (26.1.1 log: `vout: V(out)=6.66666650772 at 1000`); use `.tran` or `.dc` for per-step measurements.
- The AC `.raw` summary gives only the magnitude. The stepped `.raw` summary is limited to the first 3 steps, and the plots to the first 8 steps.
- macOS (`LTspice.app`) and Linux/Wine support with `LTSPICE_EXE` has been added to the code but has not been tried in these environments; the tests were run only on Windows + LTspice 26.1.1.
- Before simulation, the tool layer checks the LLM's netlist with `netlist.file_access_problems` (file accesses in the ADI documents): `.wave`, `.savebias`, `.savestate` that write files and `.tran ... savestate` are rejected; `.include`/`.lib`/`.loadstate`, `file=` and `wavefile=` are rejected only if they point to an absolute path, `~`, `..` or an environment variable (`%VAR%`; LTspice 26.1.1 expands it in an `.include` path, `examples/12` S3). The text after the parameter values are applied is checked. If the input is trusted, use `handle_tool_call(..., allow_file_access=True)` or `AutonomousCircuitAgent(allow_file_access=True)`. Code that calls `LTSpiceAdapter.run_netlist` directly is not checked.
- The OpenAI/Anthropic/Gemini formats of the tool schemas are produced according to the provider documentation; they are not tested automatically with the live APIs.
