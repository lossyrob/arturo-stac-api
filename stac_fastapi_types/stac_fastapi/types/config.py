from typing import Set

from pydantic import BaseSettings

class ApiSettings(BaseSettings):
    """ApiSettings.
    Defines api configuration, potentially through environment variables.
    See https://pydantic-docs.helpmanual.io/usage/settings/.
    Attributes:
        environment: name of the environment (ex. dev/prod).
        debug: toggles debug mode.
        forbidden_fields: set of fields defined by STAC but not included in the database.
        indexed_fields:
            set of fields which are usually in `item.properties` but are indexed as distinct columns in
            the database.
    """

    # TODO: Remove `default_includes` attribute so we can use `pydantic.BaseSettings` instead
    default_includes: Set[str] = None

    class Config:
        """model config (https://pydantic-docs.helpmanual.io/usage/model_config/)."""

        extra = "allow"
        env_file = ".env"
