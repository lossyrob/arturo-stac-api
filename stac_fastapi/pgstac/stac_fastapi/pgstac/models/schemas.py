"""API pydantic models."""

from datetime import datetime
from typing import List, Optional, Union

from geojson_pydantic.geometries import Polygon
from pydantic import BaseModel
from stac_pydantic import Collection as CollectionBase
from stac_pydantic import Item as ItemBase
from stac_pydantic.shared import DATETIME_RFC339
from stac_pydantic.links import Link

# Be careful: https://github.com/samuelcolvin/pydantic/issues/1423#issuecomment-642797287
NumType = Union[float, int]


class Collection(CollectionBase):
    """Collection model."""

    links: Optional[List[Link]]

    class Config:
        """Model config."""

        use_enum_values = True


class Item(ItemBase):
    """Item model."""

    geometry: Polygon
    links: Optional[List[Link]]

    class Config:
        """Model config."""

        json_encoders = {datetime: lambda v: v.strftime(DATETIME_RFC339)}
        use_enum_values = True


class Items(BaseModel):
    """Items model."""

    items: List[Item]
