"""MetaSound Toolset — editor startup registration.

Explicitly registers the toolset classes with the Toolset Registry
(registration is never automatic per Epic's create-toolset skill) and
registers the Python test runner so tests are discoverable via the
AutomationTestToolset (DiscoverTests/RunTests).
"""

import unreal

import metasound_toolset

metasound_toolset.register()

# Agent Skills register on import (toolset_registry's @agent_skill).
from metasound_toolset import skills  # noqa: E402,F401

# Test discovery: mirrors Epic's own toolset_registry/init_unreal.py pattern.
# PythonTestRunner ships with 5.8's PythonScriptPlugin; guard anyway so the
# toolset still registers on engine builds without it.
#
# The timeout is how long the automation controller polls for each test's
# result. At the 5 s default, a test that ran longer was recorded as passed
# without its result (FUntilCommand's timeout callback returns true,
# AutomationTest.h:4792-4806, 5.8.4), and the run requested for the next
# test, made while that one was still executing, never ran at all: 11 of 244
# tests on one test project never ran, yet all were reported passed (fix round 5).
# The real-tick tests await exports for seconds, so the timeout must outlast
# the slowest of them. A test that outlasts this one is still reported
# passed, so check the editor log's Python verdict count too.
TEST_TIMEOUT_SECONDS = 120.0

if hasattr(unreal, 'PythonTestRunner'):
    from metasound_toolset import tests

    tests._metasound_toolset_test_runner = unreal.PythonTestRunner.create(
        'AI.MetaSoundToolset.PythonTests',
        unreal.PythonTestRunnerSearchOptions(
            root_module=tests.__name__,
            test_name_globs=['metasound_toolset.tests.test_*']),
        TEST_TIMEOUT_SECONDS)
else:
    unreal.log_warning(
        'MetaSoundToolset: unreal.PythonTestRunner not available; '
        'Python tests will not be discoverable via automation.')
