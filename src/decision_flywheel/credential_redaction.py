"""Find environment credential values for existing local trace redactors."""
from collections.abc import Mapping


def credential_values(environment: Mapping[str, str]) -> tuple[str, ...]:
    suffixes = ('_API_KEY', '_TOKEN', '_SECRET_KEY', '_SECRET_ACCESS_KEY', '_ACCESS_KEY_ID')
    return tuple(dict.fromkeys(value for key, value in environment.items()
                              if value and key.upper().endswith(suffixes)))
