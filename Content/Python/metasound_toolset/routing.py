"""Sound routing setup: attenuation, sound class and submix on SoundBase.

MetaSoundSource (like every SoundBase) carries ``attenuation_settings``,
``sound_class_object`` and ``sound_submix_object`` properties (live-probed
2026-10-23); setting them is how a designed sound gets its spatialization
and mix routing. ``MetaSoundEditorSubsystem.build_to_asset`` additionally
takes a ``template_sound_wave`` whose attenuation/modulation/sound-class
settings are copied onto the built asset - the one-call path when a
reference wave already carries the mix setup.

House laws hold: validate before any engine write, raise exceptions,
silent saves only.
"""

import unreal

from . import _core

#: kind name -> (asset class, class path for registry queries)
ROUTING_KINDS = {
    'Attenuation': ('SoundAttenuation',
                    ('/Script/Engine', 'SoundAttenuation')),
    'SoundClass': ('SoundClass', ('/Script/Engine', 'SoundClass')),
    'Submix': ('SoundSubmix', ('/Script/Engine', 'SoundSubmix')),
}


def load_sound_base(asset_path: str) -> unreal.SoundBase:
    asset = _core.load_asset_checked(asset_path)
    if not isinstance(asset, unreal.SoundBase):
        raise TypeError(
            f"Asset '{asset_path}' is a {type(asset).__name__}, not a "
            'SoundBase (MetaSound source, SoundWave, SoundCue, ...).')
    return asset


def _object_path(obj) -> str:
    return obj.get_path_name().split('.')[0] if obj is not None else ''


def read_routing(sound: unreal.SoundBase) -> dict:
    return {
        'attenuation_path': _object_path(
            sound.get_editor_property('attenuation_settings')),
        'sound_class_path': _object_path(
            sound.get_editor_property('sound_class_object')),
        'submix_path': _object_path(
            sound.get_editor_property('sound_submix_object')),
        'priority': float(sound.get_editor_property('priority')),
    }


def apply_routing(sound: unreal.SoundBase,
                  attenuation_path: str | None,
                  sound_class_path: str | None,
                  submix_path: str | None) -> None:
    """Set the provided routing properties (None = leave, '' = clear).

    All referenced assets are loaded and type-checked BEFORE the first
    property write, so a bad path cannot leave the sound half-routed.
    """
    writes = []
    for value, prop, cls in (
            (attenuation_path, 'attenuation_settings',
             unreal.SoundAttenuation),
            (sound_class_path, 'sound_class_object', unreal.SoundClass),
            (submix_path, 'sound_submix_object', unreal.SoundSubmixBase)):
        if value is None:
            continue
        asset = _core.load_asset_checked(value, cls) if value else None
        writes.append((prop, asset))
    if not writes:
        raise ValueError(
            'Provide at least one of attenuation_path / sound_class_path / '
            "submix_path (empty string '' clears a slot).")
    for prop, asset in writes:
        sound.set_editor_property(prop, asset)


def save_silently(asset_path: str) -> None:
    if not unreal.EditorAssetLibrary.save_asset(
            asset_path.split('.')[0], only_if_is_dirty=False):
        raise RuntimeError(
            f"Silent save of '{asset_path}' failed; the change is in memory "
            'and unsaved.')


def list_routing_assets(kind: str, path_filter: str) -> list[str]:
    if kind not in ROUTING_KINDS:
        raise ValueError(
            f"kind '{kind}' must be one of {sorted(ROUTING_KINDS)}.")
    if not path_filter.startswith('/'):
        raise ValueError(
            f"path_filter '{path_filter}' must be a rooted content path "
            "such as '/Game/Audio'.")
    _, (module, class_name) = ROUTING_KINDS[kind]
    registry = unreal.AssetRegistryHelpers.get_asset_registry()
    assets = registry.get_assets_by_class(
        unreal.TopLevelAssetPath(module, class_name), True)
    found = []
    for asset_data in assets or []:
        package = str(asset_data.package_name)
        if _core.package_under_path(package, path_filter):
            found.append(package)
    return sorted(found)


def create_attenuation(asset_name: str, package_path: str,
                       inner_radius: float, falloff_distance: float,
                       spatialize: bool, attenuate_with_lpf: bool,
                       enable_occlusion: bool) -> str:
    """Create a spherical SoundAttenuation asset; returns its object path."""
    _core.validate_asset_name(asset_name)
    _core.validate_content_path(package_path)
    _core.ensure_not_engine_content(
        f'{package_path.rstrip("/")}/{asset_name}')
    if inner_radius < 0.0:
        raise ValueError('inner_radius must be >= 0 (Unreal units).')
    if falloff_distance <= 0.0:
        raise ValueError('falloff_distance must be > 0 (Unreal units).')
    target_path = f'{package_path.rstrip("/")}/{asset_name}'
    # Registry + on-disk existence (review F4): an unindexed package file
    # would walk create_asset into its existing-object confirmation path.
    if _core.asset_exists_checked(target_path):
        raise FileExistsError(
            f"Asset '{target_path}' already exists; pick another name "
            '(attenuation assets are small - make a new one).')

    tools = unreal.AssetToolsHelpers.get_asset_tools()
    asset = tools.create_asset(asset_name, package_path.rstrip('/'),
                               unreal.SoundAttenuation,
                               unreal.SoundAttenuationFactory())
    if asset is None:
        raise RuntimeError(
            f"Could not create SoundAttenuation at '{target_path}'.")

    settings = asset.get_editor_property('attenuation')
    settings.set_editor_property('attenuate', True)
    settings.set_editor_property('spatialize', spatialize)
    settings.set_editor_property('attenuate_with_lpf', attenuate_with_lpf)
    settings.set_editor_property('enable_occlusion', enable_occlusion)
    settings.set_editor_property(
        'attenuation_shape_extents',
        unreal.Vector(inner_radius, 0.0, 0.0))
    settings.set_editor_property('falloff_distance', falloff_distance)
    asset.set_editor_property('attenuation', settings)
    save_silently(target_path)
    return target_path


def read_attenuation(asset_path: str) -> dict:
    asset = _core.load_asset_checked(asset_path, unreal.SoundAttenuation)
    settings = asset.get_editor_property('attenuation')
    extents = settings.get_editor_property('attenuation_shape_extents')
    return {
        'asset_path': asset_path.split('.')[0],
        'inner_radius': float(extents.x),
        'falloff_distance': float(
            settings.get_editor_property('falloff_distance')),
        'spatialize': bool(settings.get_editor_property('spatialize')),
        'attenuate': bool(settings.get_editor_property('attenuate')),
        'attenuate_with_lpf': bool(
            settings.get_editor_property('attenuate_with_lpf')),
        'enable_occlusion': bool(
            settings.get_editor_property('enable_occlusion')),
        'shape': str(settings.get_editor_property('attenuation_shape')),
    }
