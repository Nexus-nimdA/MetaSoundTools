"""Agent Skills shipped with the MetaSound Toolset.

Skills register themselves on import (the ``@agent_skill`` decorator);
``init_unreal.py`` imports this package after the toolset registers.
"""

from . import metasound_graph_craft  # noqa: F401
