"""Item crud client."""
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Type, Union
from urllib.parse import urlencode, urljoin
from fastapi.applications import FastAPI
from starlette.requests import Request

import sqlalchemy as sa
from sqlalchemy import func
from sqlalchemy.orm import sessionmaker

import geoalchemy2 as ga
from sqlakeyset import get_page
from sqlalchemy.engine import create_engine
from stac_api import errors
from stac_api.api.extensions import ContextExtension, FieldsExtension
from stac_api.clients.base import BaseCoreClient
from stac_api.clients.postgres.config import PostgresSettings
from stac_api.clients.postgres.base import PostgresClient, READER, WRITER
from stac_api.clients.postgres.tokens import PaginationTokenClient
from stac_api.errors import DatabaseError
from stac_api.models import database, schemas
from stac_api.models.links import CollectionLinks
from stac_pydantic import ItemCollection
from stac_pydantic.api import ConformanceClasses, LandingPage
from stac_pydantic.api.collections import Collections
from stac_pydantic.api.extensions.paging import PaginationLink
from stac_pydantic.shared import Link, MimeTypes, Relations

logger = logging.getLogger(__name__)

NumType = Union[float, int]


@dataclass
class CoreCrudClient(PostgresClient, BaseCoreClient):
    """Client for core endpoints defined by stac"""
    settings: PostgresSettings = PostgresSettings()

    landing_page_id: str = "stac-api"
    title: str = "Arturo STAC API"
    description: str = "Arturo raster datastore"
    pool_size: int = 5
    pagination_client: Optional[PaginationTokenClient] = None
    table: Type[database.Item] = database.Item
    collection_table: Type[database.Collection] = database.Collection

    def register(self, app: FastAPI) -> None:
        """Register client with the application"""
        @app.on_event("startup")
        async def on_startup():
            """Create database engines and sessions on startup"""
            app.state.ENGINE_READER = create_engine(
                self.settings.reader_connection_string, echo=app.debug, pool_size=self.pool_size
            )
            app.state.ENGINE_WRITER = create_engine(
                self.settings.writer_connection_string, echo=app.debug, pool_size=self.pool_size
            )
            app.state.DB_READER = sessionmaker(
                autocommit=False, autoflush=False, bind=app.state.ENGINE_READER
            )
            app.state.DB_WRITER = sessionmaker(
                autocommit=False, autoflush=False, bind=app.state.ENGINE_WRITER
            )

        @app.on_event("shutdown")
        async def on_shutdown():
            """Dispose of database engines and sessions on app shutdown"""
            app.state.ENGINE_READER.dispose()
            app.state.ENGINE_WRITER.dispose()

        @app.middleware("http")
        async def create_db_connection(request: Request, call_next):
            """Create a new database connection for each request"""
            if "titiler" in str(request.url):
                return await call_next(request)
            reader = request.app.state.DB_READER()
            writer = request.app.state.DB_WRITER()
            READER.set(reader)
            WRITER.set(writer)
            try:
                resp = await call_next(request)
            finally:
                reader.close()
                writer.close()
            return resp

    def landing_page(self, **kwargs) -> LandingPage:
        """landing page"""
        landing_page = LandingPage(
            id=self.landing_page_id,
            title=self.title,
            description=self.description,
            links=[
                Link(
                    rel=Relations.self,
                    type=MimeTypes.json,
                    href=str(kwargs["request"].base_url),
                ),
                Link(
                    rel=Relations.docs,
                    type=MimeTypes.html,
                    title="OpenAPI docs",
                    href=urljoin(str(kwargs["request"].base_url), "docs"),
                ),
                Link(
                    rel=Relations.conformance,
                    type=MimeTypes.json,
                    title="STAC/WFS3 conformance classes implemented by this server",
                    href=urljoin(str(kwargs["request"].base_url), "conformance"),
                ),
                Link(
                    rel=Relations.search,
                    type=MimeTypes.geojson,
                    title="STAC search",
                    href=urljoin(str(kwargs["request"].base_url), "search"),
                ),
            ],
        )
        col_result = self.collections(request=kwargs["request"])
        for coll in col_result.collections:
            coll_link = CollectionLinks(
                collection_id=coll.id, base_url=str(kwargs["request"].base_url)
            ).self()
            coll_link.rel = Relations.child
            coll_link.title = coll.title
            landing_page.links.append(coll_link)
        return landing_page

    def conformance(self, **kwargs) -> ConformanceClasses:
        """conformance classes"""
        return ConformanceClasses(
            conformsTo=[
                "https://stacspec.org/STAC-api.html",
                "http://docs.opengeospatial.org/is/17-069r3/17-069r3.html#ats_geojson",
            ]
        )

    def collections(self, **kwargs) -> Collections:
        """Read collections from the database"""
        try:
            collections = self.reader_session.query(self.collection_table).all()
        except Exception as e:
            logger.error(e, exc_info=True)
            raise errors.DatabaseError(
                "Unhandled database error when getting item collection"
            )

        response_collections = []
        for collection in collections:
            collection.base_url = str(kwargs["request"].base_url)
            response_collections.append(schemas.Collection.from_orm(collection))
        return Collections(
            collections=response_collections,
            links=[Link(
                    rel=Relations.self,
                    type=MimeTypes.json,
                    href=urljoin(str(kwargs["request"].base_url), "collections"),
                ),]
        )

    def get_collection(self, id: str, **kwargs) -> schemas.Collection:
        """Get collection by id"""
        collection = self.lookup_id(id, table=self.collection_table).first()
        collection.base_url = str(kwargs["request"].base_url)
        return schemas.Collection.from_orm(collection)

    def item_collection(
        self, id: str, limit: int = 10, token: str = None, **kwargs
    ) -> ItemCollection:
        """Read an item collection from the database"""
        try:
            collection_children = (
                self.reader_session.query(self.table)
                .join(self.collection_table)
                .filter(self.collection_table.id == id)
                .order_by(self.table.datetime.desc(), self.table.id)
            )
            count = None
            if self.extension_is_enabled(ContextExtension):
                count_query = collection_children.statement.with_only_columns(
                    [func.count()]
                ).order_by(None)
                count = collection_children.session.execute(count_query).scalar()
            token = self.pagination_client.get(token) if token else token
            page = get_page(collection_children, per_page=limit, page=(token or False))
            # Create dynamic attributes for each page
            page.next = (
                self.pagination_client.insert(keyset=page.paging.bookmark_next)
                if page.paging.has_next
                else None
            )
            page.previous = (
                self.pagination_client.insert(keyset=page.paging.bookmark_previous)
                if page.paging.has_previous
                else None
            )
        except errors.NotFoundError:
            raise
        except Exception as e:
            logger.error(e, exc_info=True)
            raise errors.DatabaseError(
                "Unhandled database error when getting collection children"
            )

        links = []
        if page.next:
            links.append(
                PaginationLink(
                    rel=Relations.next,
                    type="application/geo+json",
                    href=f"{kwargs['request'].base_url}collections/{id}/items?token={page.next}&limit={limit}",
                    method="GET",
                )
            )
        if page.previous:
            links.append(
                PaginationLink(
                    rel=Relations.previous,
                    type="application/geo+json",
                    href=f"{kwargs['request'].base_url}collections/{id}/items?token={page.previous}&limit={limit}",
                    method="GET",
                )
            )

        response_features = []
        for item in page:
            item.base_url = str(kwargs["request"].base_url)
            response_features.append(schemas.Item.from_orm(item))

        context_obj = None
        if self.extension_is_enabled(ContextExtension):
            context_obj = {"returned": len(page), "limit": limit, "matched": count}

        return ItemCollection(
            type="FeatureCollection",
            context=context_obj,
            features=response_features,
            links=links,
        )

    def get_item(self, id: str, collection_id: str, **kwargs) -> schemas.Item:
        """Get item by id"""
        obj = self.lookup_id(id).first()
        obj.base_url = str(kwargs["request"].base_url)
        return schemas.Item.from_orm(obj)

    def get_search(
        self,
        collections: Optional[List[str]] = None,
        ids: Optional[List[str]] = None,
        bbox: Optional[List[NumType]] = None,
        datetime: Optional[Union[str, datetime]] = None,
        limit: Optional[int] = 10,
        query: Optional[str] = None,
        token: Optional[str] = None,
        fields: Optional[List[str]] = None,
        sortby: Optional[str] = None,
        **kwargs,
    ) -> Dict[str, Any]:
        """GET search catalog"""
        # Parse request parameters
        base_args = {
            "collections": collections,
            "ids": ids,
            "bbox": bbox,
            "limit": limit,
            "token": token,
            "query": json.loads(query) if query else query,
        }
        if datetime:
            base_args["datetime"] = datetime
        if sortby:
            # https://github.com/radiantearth/stac-spec/tree/master/api-spec/extensions/sort#http-get-or-post-form
            sort_param = []
            for sort in sortby:
                sort_param.append(
                    {
                        "field": sort[1:],
                        "direction": "asc" if sort[0] == "+" else "desc",
                    }
                )
            base_args["sortby"] = sort_param

        if fields:
            includes = set()
            excludes = set()
            for field in fields:
                if field[0] == "-":
                    excludes.add(field[1:])
                elif field[0] == "+":
                    includes.add(field[1:])
                else:
                    includes.add(field)
            base_args["fields"] = {"include": includes, "exclude": excludes}

        # Do the request
        search_request = schemas.STACSearch(**base_args)
        resp = self.post_search(search_request, request=kwargs["request"])

        # Pagination
        page_links = []
        for link in resp["links"]:
            if link.rel == Relations.next or link.rel == Relations.previous:
                query_params = dict(kwargs["request"].query_params)
                if link.body and link.merge:
                    query_params.update(link.body)
                link.method = "GET"
                link.href = f"{link.href}?{urlencode(query_params)}"
                link.body = None
                link.merge = False
                page_links.append(link)
            else:
                page_links.append(link)
        resp["links"] = page_links
        return resp

    def post_search(
        self, search_request: schemas.STACSearch, **kwargs
    ) -> Dict[str, Any]:
        """POST search catalog"""
        token = (
            self.pagination_client.get(search_request.token)
            if search_request.token
            else False
        )
        query = self.reader_session.query(self.table)

        # Filter by collection
        count = None
        if search_request.collections:
            query = query.join(self.collection_table).filter(
                sa.or_(
                    *[
                        self.collection_table.id == col_id
                        for col_id in search_request.collections
                    ]
                )
            )

        # Sort
        if search_request.sortby:
            sort_fields = [
                getattr(self.table.get_field(sort.field), sort.direction.value)()
                for sort in search_request.sortby
            ]
            sort_fields.append(self.table.id)
            query = query.order_by(*sort_fields)
        else:
            # Default sort is date
            query = query.order_by(self.table.datetime.desc(), self.table.id)

        # Ignore other parameters if ID is present
        if search_request.ids:
            id_filter = sa.or_(*[self.table.id == i for i in search_request.ids])
            try:
                items = query.filter(id_filter).order_by(self.table.id)
                page = get_page(items, per_page=search_request.limit, page=token)
                if self.extension_is_enabled(ContextExtension):
                    count = len(search_request.ids)
                page.next = (
                    self.pagination_client.insert(keyset=page.paging.bookmark_next)
                    if page.paging.has_next
                    else None
                )
                page.previous = (
                    self.pagination_client.insert(keyset=page.paging.bookmark_previous)
                    if page.paging.has_previous
                    else None
                )
            except Exception as e:
                logger.error(e, exc_info=True)
                raise DatabaseError(
                    "Unhandled database error when searching for items by id"
                )
        else:
            # Spatial query
            poly = search_request.polygon()
            if poly:
                filter_geom = ga.shape.from_shape(poly, srid=4326)
                query = query.filter(
                    ga.func.ST_Intersects(self.table.geometry, filter_geom)
                )

            # Temporal query
            if search_request.datetime:
                # Two tailed query (between)
                if ".." not in search_request.datetime:
                    query = query.filter(
                        self.table.datetime.between(*search_request.datetime)
                    )
                # All items after the start date
                if search_request.datetime[0] != "..":
                    query = query.filter(
                        self.table.datetime >= search_request.datetime[0]
                    )
                # All items before the end date
                if search_request.datetime[1] != "..":
                    query = query.filter(
                        self.table.datetime <= search_request.datetime[1]
                    )

            # Query fields
            if search_request.query:
                for (field_name, expr) in search_request.query.items():
                    field = self.table.get_field(field_name)
                    for (op, value) in expr.items():
                        query = query.filter(op.operator(field, value))

            try:
                if self.extension_is_enabled(ContextExtension):
                    count_query = query.statement.with_only_columns(
                        [func.count()]
                    ).order_by(None)
                    count = query.session.execute(count_query).scalar()
                page = get_page(query, per_page=search_request.limit, page=token)
                # Create dynamic attributes for each page
                page.next = (
                    self.pagination_client.insert(keyset=page.paging.bookmark_next)
                    if page.paging.has_next
                    else None
                )
                page.previous = (
                    self.pagination_client.insert(keyset=page.paging.bookmark_previous)
                    if page.paging.has_previous
                    else None
                )
            except Exception as e:
                logger.error(e, exc_info=True)
                raise DatabaseError(
                    "Unhandled database error during spatial/temporal query"
                )
        links = []
        if page.next:
            links.append(
                PaginationLink(
                    rel=Relations.next,
                    type="application/geo+json",
                    href=f"{kwargs['request'].base_url}search",
                    method="POST",
                    body={"token": page.next},
                    merge=True,
                )
            )
        if page.previous:
            links.append(
                PaginationLink(
                    rel=Relations.previous,
                    type="application/geo+json",
                    href=f"{kwargs['request'].base_url}search",
                    method="POST",
                    body={"token": page.previous},
                    merge=True,
                )
            )

        response_features = []
        filter_kwargs = {}
        if self.extension_is_enabled(FieldsExtension):
            filter_kwargs = search_request.field.filter_fields

        xvals = []
        yvals = []
        for item in page:
            item.base_url = str(kwargs["request"].base_url)
            item_model = schemas.Item.from_orm(item)
            xvals += [item_model.bbox[0], item_model.bbox[2]]
            yvals += [item_model.bbox[1], item_model.bbox[3]]
            response_features.append(item_model.to_dict(**filter_kwargs))

        try:
            bbox = (min(xvals), min(yvals), max(xvals), max(yvals))
        except ValueError:
            bbox = None

        context_obj = None
        if self.extension_is_enabled(ContextExtension):
            context_obj = {
                "returned": len(page),
                "limit": search_request.limit,
                "matched": count,
            }

        return {
            "type": "FeatureCollection",
            "context": context_obj,
            "features": response_features,
            "links": links,
            "bbox": bbox,
        }
