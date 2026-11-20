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
- **The live monitor.** `python -m ltspice_llm.monitor` listens on 127.0.0.1 only. A web page you visit must not be able
  to read what it shows or change the folders it watches. To prevent that, it:
  - refuses a `Host` that is not a local address (DNS rebinding);
  - serves its pages with a Content-Security-Policy;
  - changes its watched folders only on a POST that carries its own origin and the `X-LTspice-Monitor` header.

  It shows only `.log` files named in run events, and `.asc` schematics (drawn with the symbols they use) from watched
  folders, LTspice's recent files or run events. A way around any of this is a vulnerability.

Out of scope:

- Other user accounts on the same computer. The monitor has no login: anyone who can connect to 127.0.0.1 can read
  what it shows and add folders for it to watch. On Linux the default event folder is under the shared `/tmp`; set
  `LTSPICE_LLM_EVENT_DIR` to a folder only you can write. Run the monitor only on a computer you do not share.
- LTspice itself; report those to Analog Devices.
- What your LLM provider receives: the netlists, logs and measurements you pass to a model go to that provider.
- Input you mark as trusted with `handle_tool_call(..., allow_file_access=True)`.
