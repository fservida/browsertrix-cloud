""" base crawl type """

import os
from datetime import timedelta
from typing import Optional, List, Union, Type, TYPE_CHECKING
from uuid import UUID
import urllib.parse
import contextlib

import asyncio
from fastapi import HTTPException, Depends

from .models import (
    CrawlFile,
    CrawlFileOut,
    BaseCrawl,
    CrawlOut,
    CrawlOutWithResources,
    UpdateCrawl,
    DeleteCrawlList,
    Organization,
    PaginatedResponse,
    User,
    StorageRef,
    RUNNING_AND_STARTING_STATES,
    SUCCESSFUL_STATES,
)
from .pagination import paginated_format, DEFAULT_PAGE_SIZE
from .utils import dt_now

if TYPE_CHECKING:
    from .crawlconfigs import CrawlConfigOps
    from .crawlmanager import CrawlManager
    from .users import UserManager
    from .orgs import OrgOps
    from .colls import CollectionOps
    from .storages import StorageOps
    from .webhooks import EventWebhookOps
    from .background_jobs import BackgroundJobOps

else:
    CrawlConfigOps = UserManager = OrgOps = CollectionOps = object
    CrawlManager = StorageOps = EventWebhookOps = BackgroundJobOps = object

# Presign duration must be less than 604800 seconds (one week),
# so set this one minute short of a week.
PRESIGN_MINUTES_MAX = 10079
PRESIGN_MINUTES_DEFAULT = PRESIGN_MINUTES_MAX


# ============================================================================
# pylint: disable=too-many-instance-attributes
class BaseCrawlOps:
    """operations that apply to all crawls"""

    # pylint: disable=duplicate-code, too-many-arguments, too-many-locals

    crawl_configs: CrawlConfigOps
    crawl_manager: CrawlManager
    user_manager: UserManager
    orgs: OrgOps
    colls: CollectionOps
    storage_ops: StorageOps
    event_webhook_ops: EventWebhookOps
    background_job_ops: BackgroundJobOps

    def __init__(
        self,
        mdb,
        users: UserManager,
        orgs: OrgOps,
        crawl_manager: CrawlManager,
        crawl_configs: CrawlConfigOps,
        colls: CollectionOps,
        storage_ops: StorageOps,
        event_webhook_ops: EventWebhookOps,
        background_job_ops: BackgroundJobOps,
    ):
        self.crawls = mdb["crawls"]
        self.crawl_manager = crawl_manager
        self.crawl_configs = crawl_configs
        self.user_manager = users
        self.orgs = orgs
        self.colls = colls
        self.storage_ops = storage_ops
        self.event_webhook_ops = event_webhook_ops
        self.background_job_ops = background_job_ops

        presign_duration_minutes = int(
            os.environ.get("PRESIGN_DURATION_MINUTES") or PRESIGN_MINUTES_DEFAULT
        )

        self.presign_duration_seconds = (
            min(presign_duration_minutes, PRESIGN_MINUTES_MAX) * 60
        )

    async def get_crawl_raw(
        self,
        crawlid: str,
        org: Optional[Organization] = None,
        type_: Optional[str] = None,
        project: Optional[dict[str, bool]] = None,
    ):
        """Get data for single crawl"""

        query: dict[str, object] = {"_id": crawlid}
        if org:
            query["oid"] = org.id

        if type_:
            query["type"] = type_

        res = await self.crawls.find_one(query, project)

        if not res:
            raise HTTPException(status_code=404, detail=f"Crawl not found: {crawlid}")

        return res

    async def _files_to_resources(self, files, org, crawlid):
        if not files:
            return []

        crawl_files = [CrawlFile(**data) for data in files]
        return await self._resolve_signed_urls(crawl_files, org, crawlid)

    async def get_crawl(
        self,
        crawlid: str,
        org: Optional[Organization] = None,
        type_: Optional[str] = None,
        cls_type: Type[Union[CrawlOut, CrawlOutWithResources]] = CrawlOutWithResources,
    ):
        """Get data for single base crawl"""
        res = await self.get_crawl_raw(crawlid, org, type_)

        if cls_type == CrawlOutWithResources:
            res["resources"] = await self._files_to_resources(
                res.get("files"), org, crawlid
            )

            if res.get("collectionIds"):
                res["collections"] = await self.colls.get_collection_names(
                    res.get("collectionIds")
                )

        res.pop("files", None)
        res.pop("errors", None)

        crawl = cls_type.from_dict(res)

        if crawl.type == "crawl":
            crawl = await self._resolve_crawl_refs(crawl, org)
            if crawl.config and crawl.config.seeds:
                crawl.config.seeds = None

        crawl.storageQuotaReached = await self.orgs.storage_quota_reached(crawl.oid)
        crawl.execMinutesQuotaReached = await self.orgs.exec_mins_quota_reached(
            crawl.oid
        )

        return crawl

    async def get_resource_resolved_raw_crawl(
        self, crawlid: str, org: Organization, type_=None
    ):
        """return single base crawl with resources resolved"""
        res = await self.get_crawl_raw(crawlid=crawlid, type_=type_, org=org)
        res["resources"] = await self._files_to_resources(
            res.get("files"), org, res["_id"]
        )
        return res

    async def _update_crawl_collections(
        self, crawl_id: str, org: Organization, collection_ids: List[UUID]
    ):
        """Update crawl collections to match updated list."""
        crawl = await self.get_crawl(crawl_id, org, cls_type=CrawlOut)

        prior_coll_ids = set(crawl.collectionIds)
        updated_coll_ids = set(collection_ids)

        # Add new collections
        added = list(updated_coll_ids.difference(prior_coll_ids))
        for coll_id in added:
            await self.colls.add_crawls_to_collection(coll_id, [crawl_id], org)

        # Remove collections crawl no longer belongs to
        removed = list(prior_coll_ids.difference(updated_coll_ids))
        for coll_id in removed:
            await self.colls.remove_crawls_from_collection(coll_id, [crawl_id], org)

    async def update_crawl(
        self, crawl_id: str, org: Organization, update: UpdateCrawl, type_=None
    ):
        """Update existing crawl"""
        update_values = update.dict(exclude_unset=True)
        if len(update_values) == 0:
            raise HTTPException(status_code=400, detail="no_update_data")

        # Update collections then unset from update_values
        # We handle these separately due to updates required for collection changes
        collection_ids = update_values.get("collectionIds")
        if collection_ids is not None:
            await self._update_crawl_collections(crawl_id, org, collection_ids)
        update_values.pop("collectionIds", None)

        query = {"_id": crawl_id, "oid": org.id}
        if type_:
            query["type"] = type_

        # update in db
        result = await self.crawls.find_one_and_update(
            query,
            {"$set": update_values},
        )

        if not result:
            raise HTTPException(status_code=404, detail="crawl_not_found")

        return {"updated": True}

    async def update_crawl_state(self, crawl_id: str, state: str):
        """called only when job container is being stopped/canceled"""

        data = {"state": state}
        # if cancelation, set the finish time here
        if state == "canceled":
            data["finished"] = dt_now()

        await self.crawls.find_one_and_update(
            {
                "_id": crawl_id,
                "type": "crawl",
                "state": {"$in": RUNNING_AND_STARTING_STATES},
            },
            {"$set": data},
        )

    async def update_usernames(self, userid: UUID, updated_name: str) -> None:
        """Update username references matching userid"""
        await self.crawls.update_many(
            {"userid": userid}, {"$set": {"userName": updated_name}}
        )

    async def add_crawl_file_replica(
        self, crawl_id: str, filename: str, ref: StorageRef
    ) -> dict[str, object]:
        """Add replica StorageRef to existing CrawlFile"""
        return await self.crawls.find_one_and_update(
            {"_id": crawl_id, "files.filename": filename},
            {
                "$addToSet": {
                    "files.$.replicas": {"name": ref.name, "custom": ref.custom}
                }
            },
        )

    async def shutdown_crawl(self, crawl_id: str, org: Organization, graceful: bool):
        """stop or cancel specified crawl"""
        crawl = await self.get_crawl_raw(crawl_id, org)
        if crawl.get("type") != "crawl":
            return

        result = None
        try:
            result = await self.crawl_manager.shutdown_crawl(
                crawl_id, graceful=graceful
            )

            if result.get("success"):
                if graceful:
                    await self.crawls.find_one_and_update(
                        {"_id": crawl_id, "type": "crawl", "oid": org.id},
                        {"$set": {"stopping": True}},
                    )
                return result

        except Exception as exc:
            # pylint: disable=raise-missing-from
            # if reached here, probably crawl doesn't exist anymore
            raise HTTPException(
                status_code=404, detail=f"crawl_not_found, (details: {exc})"
            )

        # if job no longer running, canceling is considered success,
        # but graceful stoppage is not possible, so would be a failure
        if result.get("error") == "Not Found":
            if not graceful:
                await self.update_crawl_state(crawl_id, "canceled")
                crawl = await self.get_crawl_raw(crawl_id, org)
                if not await self.crawl_configs.stats_recompute_last(
                    crawl["cid"], 0, -1
                ):
                    raise HTTPException(
                        status_code=404,
                        detail=f"crawl_config_not_found: {crawl['cid']}",
                    )

                return {"success": True}

        # return whatever detail may be included in the response
        raise HTTPException(status_code=400, detail=result)

    async def delete_crawls(
        self,
        org: Organization,
        delete_list: DeleteCrawlList,
        type_: str,
        user: Optional[User] = None,
    ):
        """Delete a list of crawls by id for given org"""
        cids_to_update: dict[str, dict[str, int]] = {}

        size = 0

        for crawl_id in delete_list.crawl_ids:
            crawl = await self.get_crawl_raw(crawl_id, org)
            if crawl.get("type") != type_:
                continue

            # Ensure user has appropriate permissions for all crawls in list:
            # - Crawler users can delete their own crawls
            # - Org owners can delete any crawls in org
            if user and (crawl.get("userid") != user.id) and not org.is_owner(user):
                raise HTTPException(status_code=403, detail="not_allowed")

            if type_ == "crawl" and not crawl.get("finished"):
                try:
                    await self.shutdown_crawl(crawl_id, org, graceful=False)
                except Exception as exc:
                    # pylint: disable=raise-missing-from
                    raise HTTPException(
                        status_code=400, detail=f"Error Stopping Crawl: {exc}"
                    )

            crawl_size = await self._delete_crawl_files(crawl, org)
            size += crawl_size

            cid = crawl.get("cid")
            if cid:
                if cids_to_update.get(cid):
                    cids_to_update[cid]["inc"] += 1
                    cids_to_update[cid]["size"] += crawl_size
                else:
                    cids_to_update[cid] = {}
                    cids_to_update[cid]["inc"] = 1
                    cids_to_update[cid]["size"] = crawl_size

            if type_ == "crawl":
                asyncio.create_task(
                    self.event_webhook_ops.create_crawl_deleted_notification(
                        crawl_id, org
                    )
                )
            if type_ == "upload":
                asyncio.create_task(
                    self.event_webhook_ops.create_upload_deleted_notification(
                        crawl_id, org
                    )
                )

        query = {"_id": {"$in": delete_list.crawl_ids}, "oid": org.id, "type": type_}
        res = await self.crawls.delete_many(query)

        quota_reached = await self.orgs.inc_org_bytes_stored(org.id, -size, type_)

        return res.deleted_count, cids_to_update, quota_reached

    async def _delete_crawl_files(self, crawl, org: Organization):
        """Delete files associated with crawl from storage."""
        crawl = BaseCrawl.from_dict(crawl)
        size = 0
        for file_ in crawl.files:
            size += file_.size
            if not await self.storage_ops.delete_crawl_file_object(org, file_):
                raise HTTPException(status_code=400, detail="file_deletion_error")
            await self.background_job_ops.create_delete_replica_jobs(
                org, file_, crawl.id, crawl.type
            )

        return size

    async def _resolve_crawl_refs(
        self,
        crawl: Union[CrawlOut, CrawlOutWithResources],
        org: Optional[Organization],
        add_first_seed: bool = True,
        files: Optional[list[dict]] = None,
    ):
        """Resolve running crawl data"""
        # pylint: disable=too-many-branches
        config = None
        if crawl.cid:
            config = await self.crawl_configs.get_crawl_config(
                crawl.cid, org.id if org else None, active_only=False
            )
        if config and config.config.seeds:
            if add_first_seed:
                first_seed = config.config.seeds[0]
                crawl.firstSeed = first_seed.url
            crawl.seedCount = len(config.config.seeds)

        if hasattr(crawl, "profileid") and crawl.profileid:
            crawl.profileName = await self.crawl_configs.profiles.get_profile_name(
                crawl.profileid, org
            )

        if (
            files
            and crawl.state in SUCCESSFUL_STATES
            and isinstance(crawl, CrawlOutWithResources)
        ):
            crawl.resources = await self._files_to_resources(files, org, crawl.id)

        return crawl

    async def _resolve_signed_urls(
        self, files: List[CrawlFile], org: Organization, crawl_id: Optional[str] = None
    ):
        if not files:
            print("no files")
            return

        delta = timedelta(seconds=self.presign_duration_seconds)

        out_files = []

        for file_ in files:
            presigned_url = file_.presignedUrl
            now = dt_now()

            if not presigned_url or now >= file_.expireAt:
                exp = now + delta
                presigned_url = await self.storage_ops.get_presigned_url(
                    org, file_, self.presign_duration_seconds
                )
                await self.crawls.find_one_and_update(
                    {"files.filename": file_.filename},
                    {
                        "$set": {
                            "files.$.presignedUrl": presigned_url,
                            "files.$.expireAt": exp,
                        }
                    },
                )
                file_.expireAt = exp

            expire_at_str = ""
            if file_.expireAt:
                expire_at_str = file_.expireAt.isoformat()

            out_files.append(
                CrawlFileOut(
                    name=file_.filename,
                    path=presigned_url or "",
                    hash=file_.hash,
                    crc32=file_.crc32,
                    size=file_.size,
                    crawlId=crawl_id,
                    numReplicas=len(file_.replicas) if file_.replicas else 0,
                    expireAt=expire_at_str,
                )
            )

        return out_files

    @contextlib.asynccontextmanager
    async def get_redis(self, crawl_id):
        """get redis url for crawl id"""
        redis_url = self.crawl_manager.get_redis_url(crawl_id)

        redis = await self.crawl_manager.get_redis_client(redis_url)

        try:
            yield redis
        finally:
            await redis.close()

    async def add_to_collection(
        self, crawl_ids: List[str], collection_id: UUID, org: Organization
    ):
        """Add crawls to collection."""
        for crawl_id in crawl_ids:
            crawl_raw = await self.get_crawl_raw(crawl_id, org)
            crawl_collections = crawl_raw.get("collectionIds")
            if crawl_collections and crawl_id in crawl_collections:
                raise HTTPException(
                    status_code=400, detail="crawl_already_in_collection"
                )

            await self.crawls.find_one_and_update(
                {"_id": crawl_id},
                {"$push": {"collectionIds": collection_id}},
            )

    async def remove_from_collection(self, crawl_ids: List[str], collection_id: UUID):
        """Remove crawls from collection."""
        for crawl_id in crawl_ids:
            await self.crawls.find_one_and_update(
                {"_id": crawl_id},
                {"$pull": {"collectionIds": collection_id}},
            )

    async def remove_collection_from_all_crawls(self, collection_id: UUID):
        """Remove collection id from all crawls it's currently in."""
        await self.crawls.update_many(
            {"collectionIds": collection_id},
            {"$pull": {"collectionIds": collection_id}},
        )

    # pylint: disable=too-many-branches, invalid-name, too-many-statements
    async def list_all_base_crawls(
        self,
        org: Optional[Organization] = None,
        userid: Optional[UUID] = None,
        name: Optional[str] = None,
        description: Optional[str] = None,
        collection_id: Optional[UUID] = None,
        states: Optional[List[str]] = None,
        first_seed: Optional[str] = None,
        type_: Optional[str] = None,
        cid: Optional[UUID] = None,
        cls_type: Type[Union[CrawlOut, CrawlOutWithResources]] = CrawlOut,
        page_size: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        sort_by: Optional[str] = None,
        sort_direction: int = -1,
    ):
        """List crawls of all types from the db"""
        # Zero-index page for query
        page = page - 1
        skip = page * page_size

        oid = org.id if org else None

        resources = False
        if cls_type == CrawlOutWithResources:
            resources = True

        query: dict[str, object] = {}
        if type_:
            query["type"] = type_
        if oid:
            query["oid"] = oid

        if userid:
            query["userid"] = userid

        if states:
            # validated_states = [value for value in state if value in ALL_CRAWL_STATES]
            query["state"] = {"$in": states}

        if cid:
            query["cid"] = cid

        aggregate = [
            {"$match": query},
            {"$set": {"firstSeedObject": {"$arrayElemAt": ["$config.seeds", 0]}}},
            {"$set": {"firstSeed": "$firstSeedObject.url"}},
            {"$unset": ["firstSeedObject", "errors", "config"]},
        ]

        if not resources:
            aggregate.extend([{"$unset": ["files"]}])

        if name:
            aggregate.extend([{"$match": {"name": name}}])

        if first_seed:
            aggregate.extend([{"$match": {"firstSeed": first_seed}}])

        if description:
            aggregate.extend([{"$match": {"description": description}}])

        if collection_id:
            aggregate.extend([{"$match": {"collectionIds": {"$in": [collection_id]}}}])

        if sort_by:
            if sort_by not in ("started", "finished", "fileSize"):
                raise HTTPException(status_code=400, detail="invalid_sort_by")
            if sort_direction not in (1, -1):
                raise HTTPException(status_code=400, detail="invalid_sort_direction")

            aggregate.extend([{"$sort": {sort_by: sort_direction}}])

        aggregate.extend(
            [
                {
                    "$facet": {
                        "items": [
                            {"$skip": skip},
                            {"$limit": page_size},
                        ],
                        "total": [{"$count": "count"}],
                    }
                },
            ]
        )

        # Get total
        cursor = self.crawls.aggregate(aggregate)
        results = await cursor.to_list(length=1)
        result = results[0]
        items = result["items"]

        try:
            total = int(result["total"][0]["count"])
        except (IndexError, ValueError):
            total = 0

        crawls = []
        for res in items:
            crawl = cls_type.from_dict(res)

            if resources or crawl.type == "crawl":
                # pass files only if we want to include resolved resources
                files = res.get("files") if resources else None
                crawl = await self._resolve_crawl_refs(crawl, org, files=files)

            crawls.append(crawl)

        return crawls, total

    async def delete_crawls_all_types(
        self,
        delete_list: DeleteCrawlList,
        org: Organization,
        user: Optional[User] = None,
    ):
        """Delete uploaded crawls"""
        crawls: list[str] = []
        uploads: list[str] = []

        for crawl_id in delete_list.crawl_ids:
            crawl = await self.get_crawl_raw(crawl_id, org)
            type_ = crawl.get("type")
            if type_ == "crawl":
                crawls.append(crawl_id)
            if type_ == "upload":
                uploads.append(crawl_id)

        crawls_length = len(crawls)
        uploads_length = len(uploads)

        if crawls_length + uploads_length == 0:
            raise HTTPException(status_code=400, detail="nothing_to_delete")

        deleted_count = 0
        # Value is set in delete calls, but initialize to keep linter happy.
        quota_reached = False

        if crawls_length:
            crawl_delete_list = DeleteCrawlList(crawl_ids=crawls)
            deleted, cids_to_update, quota_reached = await self.delete_crawls(
                org, crawl_delete_list, "crawl", user
            )
            deleted_count += deleted

            for cid, cid_dict in cids_to_update.items():
                cid_size = cid_dict["size"]
                cid_inc = cid_dict["inc"]
                await self.crawl_configs.stats_recompute_last(cid, -cid_size, -cid_inc)

        if uploads_length:
            upload_delete_list = DeleteCrawlList(crawl_ids=uploads)
            deleted, _, quota_reached = await self.delete_crawls(
                org, upload_delete_list, "upload", user
            )
            deleted_count += deleted

        if deleted_count < 1:
            raise HTTPException(status_code=404, detail="crawl_not_found")

        return {"deleted": True, "storageQuotaReached": quota_reached}

    async def get_all_crawl_search_values(
        self, org: Organization, type_: Optional[str] = None
    ):
        """List unique names, first seeds, and descriptions from all captures in org"""
        match_query: dict[str, object] = {"oid": org.id}
        if type_:
            match_query["type"] = type_

        names = await self.crawls.distinct("name", match_query)
        descriptions = await self.crawls.distinct("description", match_query)
        cids = (
            await self.crawls.distinct("cid", match_query)
            if not type_ or type_ == "crawl"
            else []
        )

        # Remove empty strings
        names = [name for name in names if name]
        descriptions = [description for description in descriptions if description]

        first_seeds = set()
        for cid in cids:
            if not cid:
                continue
            config = await self.crawl_configs.get_crawl_config(cid, org.id)
            if not config:
                continue
            first_seed = config.config.seeds[0]
            first_seeds.add(first_seed.url)

        return {
            "names": names,
            "descriptions": descriptions,
            "firstSeeds": list(first_seeds),
        }


# ============================================================================
def init_base_crawls_api(app, user_dep, *args):
    """base crawls api"""
    # pylint: disable=invalid-name, duplicate-code, too-many-arguments, too-many-locals

    ops = BaseCrawlOps(*args)

    org_viewer_dep = ops.orgs.org_viewer_dep
    org_crawl_dep = ops.orgs.org_crawl_dep

    @app.get(
        "/orgs/{oid}/all-crawls",
        tags=["all-crawls"],
        response_model=PaginatedResponse,
    )
    async def list_all_base_crawls(
        org: Organization = Depends(org_viewer_dep),
        pageSize: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        userid: Optional[UUID] = None,
        name: Optional[str] = None,
        state: Optional[str] = None,
        firstSeed: Optional[str] = None,
        description: Optional[str] = None,
        collectionId: Optional[UUID] = None,
        crawlType: Optional[str] = None,
        cid: Optional[UUID] = None,
        sortBy: Optional[str] = "finished",
        sortDirection: int = -1,
    ):
        states = state.split(",") if state else None

        if firstSeed:
            firstSeed = urllib.parse.unquote(firstSeed)

        if name:
            name = urllib.parse.unquote(name)

        if description:
            description = urllib.parse.unquote(description)

        if crawlType and crawlType not in ("crawl", "upload"):
            raise HTTPException(status_code=400, detail="invalid_crawl_type")

        crawls, total = await ops.list_all_base_crawls(
            org,
            userid=userid,
            name=name,
            description=description,
            collection_id=collectionId,
            states=states,
            first_seed=firstSeed,
            type_=crawlType,
            cid=cid,
            page_size=pageSize,
            page=page,
            sort_by=sortBy,
            sort_direction=sortDirection,
        )
        return paginated_format(crawls, total, page, pageSize)

    @app.get("/orgs/{oid}/all-crawls/search-values", tags=["all-crawls"])
    async def get_all_crawls_search_values(
        org: Organization = Depends(org_viewer_dep),
        crawlType: Optional[str] = None,
    ):
        if crawlType and crawlType not in ("crawl", "upload"):
            raise HTTPException(status_code=400, detail="invalid_crawl_type")

        return await ops.get_all_crawl_search_values(org, type_=crawlType)

    @app.get(
        "/orgs/{oid}/all-crawls/{crawl_id}",
        tags=["all-crawls"],
        response_model=CrawlOutWithResources,
    )
    async def get_base_crawl(crawl_id: str, org: Organization = Depends(org_crawl_dep)):
        return await ops.get_crawl(crawl_id, org)

    @app.get(
        "/orgs/all/all-crawls/{crawl_id}/replay.json",
        tags=["all-crawls"],
        response_model=CrawlOutWithResources,
    )
    async def get_base_crawl_admin(crawl_id, user: User = Depends(user_dep)):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")

        return await ops.get_crawl(crawl_id, None)

    @app.get(
        "/orgs/{oid}/all-crawls/{crawl_id}/replay.json",
        tags=["all-crawls"],
        response_model=CrawlOutWithResources,
    )
    async def get_crawl(crawl_id, org: Organization = Depends(org_viewer_dep)):
        return await ops.get_crawl(crawl_id, org)

    @app.patch("/orgs/{oid}/all-crawls/{crawl_id}", tags=["all-crawls"])
    async def update_crawl(
        update: UpdateCrawl, crawl_id: str, org: Organization = Depends(org_crawl_dep)
    ):
        return await ops.update_crawl(crawl_id, org, update)

    @app.post("/orgs/{oid}/all-crawls/delete", tags=["all-crawls"])
    async def delete_crawls_all_types(
        delete_list: DeleteCrawlList,
        user: User = Depends(user_dep),
        org: Organization = Depends(org_crawl_dep),
    ):
        return await ops.delete_crawls_all_types(delete_list, org, user)

    return ops
