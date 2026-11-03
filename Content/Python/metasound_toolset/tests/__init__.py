"""In-editor tests for the MetaSound toolset.

Discovered by the PythonTestRunner registered in init_unreal.py; run them
through the AutomationTestToolset (DiscoverTests -> RunTests ->
GetTestResults) or headless:

    UnrealEditor-Cmd <project> -ExecCmds="Automation RunTests AI.MetaSoundToolset;quit" -Unattended -NullRHI
"""

# Set by init_unreal.py; keeps the runner alive for the editor session.
_metasound_toolset_test_runner = None
