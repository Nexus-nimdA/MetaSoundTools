"""MetaSound Toolset — sound-designer-grade MetaSounds tools for Unreal's MCP.

Registers Python ToolsetDefinition classes with the engine's Toolset
Registry. The Toolset Registry is the dependency; MCP is one transport
reading that registry.

Call :func:`register` from ``init_unreal.py``; :func:`unregister` is the
counterpart for reload loops (``toolset_registry.reload_module`` handles
both automatically when hot-reloading).
"""

import unreal

_REGISTERED = False


def register() -> bool:
    """Register the MetaSound toolset classes with the Toolset Registry.

    Returns:
        True if registration happened, False if the registry is unavailable
        (e.g. the ToolsetRegistry plugin is disabled) or already registered.
    """
    global _REGISTERED
    if _REGISTERED:
        return False
    if not hasattr(unreal, 'ToolsetRegistry'):
        unreal.log_warning(
            'MetaSoundToolset: unreal.ToolsetRegistry not available; '
            'is the ToolsetRegistry plugin enabled?')
        return False

    # Import deferred so a broken tool module fails loudly here, at startup,
    # with a clear log line - not silently at first tool call.
    from .toolset import MetaSoundsToolset

    unreal.ToolsetRegistry.register_toolset_class(MetaSoundsToolset)
    _REGISTERED = True
    unreal.log('MetaSoundToolset: registered MetaSoundsToolset.')
    _log_tool_surface()
    return True


def _log_tool_surface() -> None:
    """Log the tool surface hash; warn when it differs from the release pin.

    Never fails registration: the pin is a check on the toolset, not a
    condition of running it. A different engine build can change the schema
    text by itself, so a mismatch only warns on the pinned engine version.
    """
    try:
        from . import surface

        live = surface.current_surface()
        unreal.log(
            f"MetaSoundToolset: tool surface {live['toolset_sha256']} "
            f"({live['tool_count']} tools).")
        pin = surface.read_pin()
        if (live['engine'] == pin['engine']
                and live['toolset_sha256'] != pin['toolset_sha256']):
            unreal.log_warning(
                'MetaSoundToolset: the tool surface does not match the pin '
                'shipped with this release. These tools have been changed '
                'since it was taken.')
    except Exception as exc:
        unreal.log_warning(
            f'MetaSoundToolset: could not check the tool surface pin: {exc}')


def unregister() -> None:
    """Unregister the MetaSound toolset classes from the Toolset Registry."""
    global _REGISTERED
    if not _REGISTERED or not hasattr(unreal, 'ToolsetRegistry'):
        return

    from .toolset import MetaSoundsToolset

    unreal.ToolsetRegistry.unregister_toolset_class(MetaSoundsToolset)
    _REGISTERED = False
    unreal.log('MetaSoundToolset: unregistered MetaSoundsToolset.')
