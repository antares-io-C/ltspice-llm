"""
LTspice - LLM Python Adapter
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Bir LLM (Büyük Dil Modeli) ile LTspice analog devre simülatörü arasında
çift yönlü köprü sağlayan Python kütüphanesi.
"""

from .netlist import measured_signals, validated_save_signal
from .adapter import LTSpiceAdapter, SimulationResult, apply_parameters, strip_auto_directives
from .cpu_guard import CpuLease, CpuSlotPool, CpuSlotTimeout, available_cpus
from .parser import (
    LogParser,
    RawParser,
    WaveformSummary,
    ParsedLog,
    parse_convergence_report,
    parse_fourier_blocks,
    read_measure_db,
)
from .reference import ReferenceDocs, find_model_definitions, ltspice_data_dir, ltspice_user_dir, reference_dir
from .report import SimulationReport
from .visualizer import WaveformVisualizer
from .tools import convert_tool_schemas, get_ltspice_tool_schemas, handle_tool_call
from .agent import CircuitOptimizer, OptimizationResult
from .circuit_agent import AutonomousCircuitAgent
from .system_prompts import CIRCUIT_DESIGNER_SYSTEM_PROMPT
from .quantizer import (
    quantize_circuit_parameters,
    snap_to_e_series,
    parse_spice_value,
    format_spice_value,
)
from .diagnostics import SPICEDiagnostics, DiagnosisReport, SPICEIssueType
from .schematic import (
    AscSchematic,
    SchematicSymbol,
    SchematicWire,
    SchematicFlag,
    SchematicText,
    decode_asc_bytes,
    encode_asc_text,
    is_asc_content,
    read_asc_file,
    write_asc_file,
)
from .symbols import SymbolDefinition, SymbolPin, find_symbol, load_symbol, read_symbol
from .parasitics import (
    CableParasitics,
    HotplugAnalysis,
    MlccDeratingResult,
    TraceParasitics,
    analyze_hotplug_transient,
    hotplug_step_peak,
    calculate_trace_parasitics,
    get_cable_parasitics,
    inject_trace_parasitics_into_netlist,
    mlcc_dc_bias_derating,
)

__version__ = "1.4.0"
__all__ = [
    "LTSpiceAdapter",
    "SimulationResult",
    "apply_parameters",
    "measured_signals",
    "validated_save_signal",
    "strip_auto_directives",
    "CpuSlotPool",
    "CpuLease",
    "CpuSlotTimeout",
    "available_cpus",
    "convert_tool_schemas",
    "OptimizationResult",
    "LogParser",
    "RawParser",
    "ParsedLog",
    "WaveformSummary",
    "parse_convergence_report",
    "parse_fourier_blocks",
    "read_measure_db",
    "ReferenceDocs",
    "find_model_definitions",
    "ltspice_data_dir",
    "ltspice_user_dir",
    "reference_dir",
    "SimulationReport",
    "WaveformVisualizer",
    "get_ltspice_tool_schemas",
    "handle_tool_call",
    "CircuitOptimizer",
    "AutonomousCircuitAgent",
    "CIRCUIT_DESIGNER_SYSTEM_PROMPT",
    "quantize_circuit_parameters",
    "snap_to_e_series",
    "parse_spice_value",
    "format_spice_value",
    "SPICEDiagnostics",
    "DiagnosisReport",
    "SPICEIssueType",
    "AscSchematic",
    "SchematicSymbol",
    "SchematicWire",
    "SchematicFlag",
    "SchematicText",
    "is_asc_content",
    "decode_asc_bytes",
    "encode_asc_text",
    "read_asc_file",
    "write_asc_file",
    "SymbolDefinition",
    "SymbolPin",
    "find_symbol",
    "load_symbol",
    "read_symbol",
    "TraceParasitics",
    "MlccDeratingResult",
    "CableParasitics",
    "HotplugAnalysis",
    "calculate_trace_parasitics",
    "mlcc_dc_bias_derating",
    "get_cable_parasitics",
    "analyze_hotplug_transient",
    "hotplug_step_peak",
    "inject_trace_parasitics_into_netlist",
]

