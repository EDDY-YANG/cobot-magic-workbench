"""Explicit capture scope; device registration and hardware checks stay intact.

PC: python -X utf8 -B -m unittest discover -s tests -p test_2026_10_16_capture_profile.py
Importing this module opens no device. Only named profiles can select channels.
"""
import copy

SCHEMA = 'tiger-capture-contract-v1'
ALL = 'all_configured_v1'
RIGHT = 'right_only_v1'
PROFILES = frozenset((ALL, RIGHT))


def profile_name(cfg):
    name = cfg.get('capture_profile', ALL)
    if not isinstance(name, str) or name not in PROFILES:
        raise ValueError('Unknown capture profile')
    return name


def contract(cfg, name=None):
    name = profile_name(cfg) if name is None else name
    if not isinstance(name, str) or name not in PROFILES:
        raise ValueError('Unknown capture profile')
    registered_arms, registered_cameras = cfg.get('can', {}), cfg.get('cameras', {})
    if not isinstance(registered_arms, dict) or not isinstance(registered_cameras, dict):
        raise ValueError('Capture profile requires registered devices')
    arms = list(registered_arms) if name == ALL else ['right']
    cameras = list(registered_cameras) if name == ALL else ['front', 'right']
    if any(role not in registered_arms for role in arms) or any(role not in registered_cameras for role in cameras):
        raise ValueError('Capture profile requires registered devices')
    return copy.deepcopy({'schema': SCHEMA, 'profile': name,
        'required_arm_roles': arms, 'required_camera_roles': cameras,
        'excluded_arm_roles': [role for role in registered_arms if role not in arms],
        'excluded_camera_roles': [role for role in registered_cameras if role not in cameras],
        'arm_adapter_serials': {role: registered_arms[role].get('expected_serial') for role in arms},
        'camera_serials': {role: registered_cameras[role] for role in cameras}})


def receipt_contract(cfg, report):
    """Review a frozen episode against its batch assignment, not today's scope.

    Legacy episodes without the new field retain all registered-channel checks.
    A new scoped batch cannot lose its contract and become a legacy episode.
    """
    value = report.get('capture_contract')
    metadata = report.get('collection_v1')
    if 'collection_v1' in report and not isinstance(metadata, dict):
        raise ValueError('Invalid collection metadata fields')
    expected = (metadata.get('capture_profile', ALL) if metadata is not None else
                report.get('capture_profile', value.get('profile', ALL) if isinstance(value, dict) else ALL))
    if 'capture_profile' in report and report['capture_profile'] != expected:
        raise ValueError('Recording profile differs from frozen batch profile')
    if value is None:
        if (expected != ALL or 'capture_profile' in report
                or metadata is not None and 'capture_profile' in metadata
                or 'required_arm_roles' in report or 'required_camera_roles' in report):
            raise ValueError('Scoped recording lost its frozen capture contract')
        return contract(cfg, ALL)
    if not isinstance(value, dict) or value != contract(cfg, expected):
        raise ValueError('Capture contract differs from registered devices or frozen batch profile')
    for field in ('required_arm_roles', 'required_camera_roles'):
        if field in report and report[field] != value[field]:
            raise ValueError('Recording channel roles differ from frozen capture contract')
    return copy.deepcopy(value)
