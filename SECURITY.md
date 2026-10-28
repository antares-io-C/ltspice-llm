# Security Policy

## Reporting a vulnerability

Report security problems privately: on GitHub, open the repository's **Security** tab and choose **Report a
vulnerability**. Please don't open a public issue for them. English or Turkish is fine.

Include the ltspice-llm version (`ltspice_llm.__version__`), the LTspice version, and the smallest netlist or call
sequence that shows the problem. Only the latest release gets fixes.

## Scope

ltspice-llm runs LTspice on your machine with netlists and schematics that a language model writes. In scope:

- **File access by untrusted netlists.** Before LTspice sees a netlist or `.asc` from the model, the tool layer checks
  it with `ltspice_llm.netlist.file_access_problems`. Nothing may write a file. Nothing may read a file outside the
  workspace or the LTspice libraries. A netlist that gets past this check is a vulnerability.
- **Paths chosen by the model.** Circuit names become file names inside the adapter's `workspace_dir` and must not
  leave it.
- **HTML reports.** `SimulationReport` pages must not load anything from the network.

Out of scope:

- LTspice itself; report those to Analog Devices.
- What your LLM provider receives: the netlists, logs and measurements you pass to a model go to that provider.
- Input you mark as trusted with `handle_tool_call(..., allow_file_access=True)`.
