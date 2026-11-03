"""The tool surface pin: a hash of the schema these tools advertise.

Tool descriptions are read by the model, so a changed description changes
behaviour as surely as changed code. This module hashes the toolset schema
the MCP server hands to clients, byte for byte the describe_toolset text
(live-verified 2026-11-02), and data/tool_surface_pin.json records the value
for this release. On the pinned engine version the automation suite fails
when the two disagree, and tools/verify_tool_surface.py lets a client compare
a running editor against a hash it recorded.

The hash is a tripwire for interface changes. It says nothing about tool
bodies, catalog text or result text, and a project's ToolsetBlockedNames or
ToolsetAllowedNames setting changes what clients receive without changing
what is hashed here. Skill text has its own hash, read by the suite only.

The registry reports version "1.0" for every Python toolset: the version is
a C++ virtual with no Python override, so the schema's own version field
cannot carry this. The hash needs no field of its own.

After a deliberate surface change, regenerate the pin from a headless editor
by running a one-line script through -ExecutePythonScript:

    from metasound_toolset import surface; surface.write_pin()
"""

import hashlib
import inspect
import json
from pathlib import Path

import unreal

from .skills import metasound_graph_craft

PIN_PATH = Path(__file__).parent / 'data' / 'tool_surface_pin.json'


def surface_sha256(schema_text: str) -> str:
    """Hash a toolset schema independently of key and tool ordering.

    Must stay identical to surface_sha256 in tools/verify_tool_surface.py.
    """
    schema = json.loads(schema_text)
    schema['tools'] = sorted(schema['tools'], key=lambda tool: tool['name'])
    canonical = json.dumps(schema, sort_keys=True, separators=(',', ':'),
                           ensure_ascii=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def skill_text() -> str:
    """What the skill tells a model: its listing line and its full body.

    Read through the module so a reload sees the current text.
    """
    skill = getattr(metasound_graph_craft, 'MetaSoundGraphCraftSkill', None)
    description = inspect.cleandoc(skill.__doc__ or '') if skill else ''
    return f'{description}\n{metasound_graph_craft._INSTRUCTIONS}'


def engine_version() -> str:
    """The running engine as 'major.minor.patch'."""
    return str(unreal.SystemLibrary.get_engine_version()).split('-')[0]


def current_surface() -> dict:
    """The surface of the code running now, in the pin file's shape."""
    from .toolset import MetaSoundsToolset

    schema_text = unreal.ToolsetRegistry.get_toolset_json_schema(
        MetaSoundsToolset)
    return {
        'engine': engine_version(),
        'tool_count': len(json.loads(schema_text)['tools']),
        'toolset_sha256': surface_sha256(schema_text),
        'skill_sha256': hashlib.sha256(
            skill_text().encode('utf-8')).hexdigest(),
    }


def read_pin() -> dict:
    """The pin committed with this release."""
    return json.loads(PIN_PATH.read_text(encoding='utf-8'))


def write_pin() -> dict:
    """Record the running surface as the pin; returns what was written."""
    surface = current_surface()
    PIN_PATH.write_text(json.dumps(surface, indent=2) + '\n', encoding='utf-8')
    return surface
