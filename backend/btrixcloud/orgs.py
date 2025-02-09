"""
Organization API handling
"""
# pylint: disable=too-many-lines
import math
import os
import time
import urllib.parse
from uuid import UUID, uuid4
from datetime import datetime

from typing import Optional, TYPE_CHECKING

from pymongo import ReturnDocument
from pymongo.errors import AutoReconnect, DuplicateKeyError
from fastapi import APIRouter, Depends, HTTPException, Request

from .models import (
    SUCCESSFUL_STATES,
    RUNNING_STATES,
    STARTING_STATES,
    Organization,
    StorageRef,
    OrgQuotas,
    OrgQuotaUpdate,
    OrgMetrics,
    OrgWebhookUrls,
    CreateOrg,
    RenameOrg,
    UpdateRole,
    RemovePendingInvite,
    RemoveFromOrg,
    AddToOrgRequest,
    InvitePending,
    InviteToOrgRequest,
    UserRole,
    User,
    PaginatedResponse,
)
from .pagination import DEFAULT_PAGE_SIZE, paginated_format
from .utils import slug_from_name

if TYPE_CHECKING:
    from .invites import InviteOps
else:
    InviteOps = object


DEFAULT_ORG = os.environ.get("DEFAULT_ORG", "My Organization")


# ============================================================================
# pylint: disable=too-many-public-methods, too-many-instance-attributes
class OrgOps:
    """Organization API operations"""

    invites: InviteOps
    default_primary: Optional[StorageRef]

    def __init__(self, mdb, invites):
        self.orgs = mdb["organizations"]
        self.crawls_db = mdb["crawls"]
        self.profiles_db = mdb["profiles"]
        self.colls_db = mdb["collections"]

        self.router = None
        self.org_viewer_dep = None
        self.org_crawl_dep = None
        self.org_owner_dep = None
        self.org_public = None

        self.default_primary = None

        self.invites = invites

    def set_default_primary_storage(self, storage: StorageRef):
        """set default primary storage"""
        self.default_primary = storage

    async def init_index(self):
        """init lookup index"""
        while True:
            try:
                await self.orgs.create_index("name", unique=True)
                return await self.orgs.create_index("slug", unique=True)
            # pylint: disable=duplicate-code
            except AutoReconnect:
                print(
                    "Database connection unavailable to create index. Will try again in 5 scconds",
                    flush=True,
                )
                time.sleep(5)

    async def add_org(self, org: Organization):
        """Add new org"""
        try:
            return await self.orgs.insert_one(org.to_dict())
        except DuplicateKeyError:
            print(f"Organization name {org.name} already in use - skipping", flush=True)

    async def create_new_org_for_user(
        self,
        org_name: str,
        user: User,
    ) -> Organization:
        # pylint: disable=too-many-arguments
        """Create new organization with default storage for new user"""
        id_ = uuid4()

        org = Organization(
            id=id_,
            name=org_name,
            slug=slug_from_name(org_name),
            users={str(user.id): UserRole.OWNER},
            storage=self.default_primary,
        )
        primary_name = self.default_primary and self.default_primary.name

        print(
            f"Creating new org {org_name} with storage {primary_name}",
            flush=True,
        )
        await self.add_org(org)
        return org

    async def get_orgs_for_user(
        # pylint: disable=too-many-arguments
        self,
        user: User,
        role: UserRole = UserRole.VIEWER,
        page_size: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
        calculate_total=True,
    ):
        """Get all orgs a user is a member of"""
        # Zero-index page for query
        page = page - 1
        skip = page_size * page

        if user.is_superuser:
            query = {}
        else:
            query = {f"users.{user.id}": {"$gte": role.value}}

        total = 0
        if calculate_total:
            total = await self.orgs.count_documents(query)

        cursor = self.orgs.find(query, skip=skip, limit=page_size)
        results = await cursor.to_list(length=page_size)
        orgs = [Organization.from_dict(res) for res in results]

        return orgs, total

    async def get_org_for_user_by_id(
        self, oid: UUID, user: User, role: UserRole = UserRole.VIEWER
    ):
        """Get an org for user by unique id"""
        query: dict[str, object]
        if user.is_superuser:
            query = {"_id": oid}
        else:
            query = {f"users.{user.id}": {"$gte": role.value}, "_id": oid}
        res = await self.orgs.find_one(query)
        return Organization.from_dict(res)

    async def get_org_by_id(self, oid: UUID):
        """Get an org by id"""
        res = await self.orgs.find_one({"_id": oid})
        return Organization.from_dict(res)

    async def get_default_org(self):
        """Get default organization"""
        res = await self.orgs.find_one({"default": True})
        if res:
            return Organization.from_dict(res)

    async def create_default_org(self):
        """Create default organization if doesn't exist."""
        await self.init_index()

        default_org = await self.get_default_org()
        if default_org:
            if default_org.name == DEFAULT_ORG:
                print("Default organization already exists - skipping", flush=True)
            else:
                default_org.name = DEFAULT_ORG
                default_org.slug = slug_from_name(DEFAULT_ORG)
                await self.update_full(default_org)
                print(f'Default organization renamed to "{DEFAULT_ORG}"', flush=True)
            return

        id_ = uuid4()
        org = Organization(
            id=id_,
            name=DEFAULT_ORG,
            slug=slug_from_name(DEFAULT_ORG),
            users={},
            storage=self.default_primary,
            default=True,
        )
        primary_name = self.default_primary and self.default_primary.name
        print(
            f'Creating Default Organization "{DEFAULT_ORG}". Storage: {primary_name}',
            flush=True,
        )
        await self.add_org(org)

    async def check_all_org_default_storages(self, storage_ops) -> None:
        """ensure all default storages references by this org actually exist

        designed to help prevent removal of a 'storage' entry if
        an org is still referencing that storage"""
        storage_names = list(storage_ops.default_storages.keys())
        errors = 0

        async for org_data in self.orgs.find(
            {"storage.custom": False, "storage.name": {"$nin": storage_names}}
        ):
            org = Organization.from_dict(org_data)
            print(f"Org {org.slug} uses unknown primary storage {org.storage.name}")
            errors += 1

        async for org_data in self.orgs.find(
            {
                "storageReplicas.custom": False,
                "storageReplicas.name": {"$nin": storage_names},
            }
        ):
            org = Organization.from_dict(org_data)
            print(f"Org {org.slug} uses an unknown replica storage")
            errors += 1

        if errors:
            raise TypeError(
                f"{errors} orgs use undefined storages, exiting."
                + " Please check the 'storages' array in your config"
            )

    async def update_full(self, org: Organization):
        """Update existing org"""
        await self.orgs.find_one_and_update(
            {"_id": org.id}, {"$set": org.to_dict()}, upsert=True
        )

    async def update_users(self, org: Organization):
        """Update org users"""
        return await self.orgs.find_one_and_update(
            {"_id": org.id}, {"$set": org.dict(include={"users"})}
        )

    async def update_slug_and_name(self, org: Organization):
        """Update org slug"""
        return await self.orgs.find_one_and_update(
            {"_id": org.id}, {"$set": {"slug": org.slug, "name": org.name}}
        )

    async def update_storage_refs(self, org: Organization):
        """Update storage + replicas for given org"""
        set_dict = org.dict(include={"storage": True, "storageReplicas": True})

        return await self.orgs.find_one_and_update({"_id": org.id}, {"$set": set_dict})

    async def update_custom_storages(self, org: Organization):
        """Update storage on an existing organization"""

        set_dict = org.dict(include={"customStorages": True})

        return await self.orgs.find_one_and_update({"_id": org.id}, {"$set": set_dict})

    async def update_quotas(self, org: Organization, quotas: OrgQuotas):
        """update organization quotas"""

        previous_extra_mins = (
            org.quotas.extraExecMinutes
            if (org.quotas and org.quotas.extraExecMinutes)
            else 0
        )
        previous_gifted_mins = (
            org.quotas.giftedExecMinutes
            if (org.quotas and org.quotas.giftedExecMinutes)
            else 0
        )

        update = quotas.dict(
            exclude_unset=True, exclude_defaults=True, exclude_none=True
        )

        quota_updates = []
        for prev_update in org.quotaUpdates or []:
            quota_updates.append(prev_update.dict())
        quota_updates.append(
            OrgQuotaUpdate(update=update, modified=datetime.now()).dict()
        )

        await self.orgs.find_one_and_update(
            {"_id": org.id},
            {
                "$set": {
                    "quotas": update,
                    "quotaUpdates": quota_updates,
                }
            },
        )

        # Inc org available fields for extra/gifted execution time as needed
        if quotas.extraExecMinutes is not None:
            extra_secs_diff = (quotas.extraExecMinutes - previous_extra_mins) * 60
            if org.extraExecSecondsAvailable + extra_secs_diff <= 0:
                await self.orgs.find_one_and_update(
                    {"_id": org.id},
                    {"$set": {"extraExecSecondsAvailable": 0}},
                )
            else:
                await self.orgs.find_one_and_update(
                    {"_id": org.id},
                    {"$inc": {"extraExecSecondsAvailable": extra_secs_diff}},
                )

        if quotas.giftedExecMinutes is not None:
            gifted_secs_diff = (quotas.giftedExecMinutes - previous_gifted_mins) * 60
            if org.giftedExecSecondsAvailable + gifted_secs_diff <= 0:
                await self.orgs.find_one_and_update(
                    {"_id": org.id},
                    {"$set": {"giftedExecSecondsAvailable": 0}},
                )
            else:
                await self.orgs.find_one_and_update(
                    {"_id": org.id},
                    {"$inc": {"giftedExecSecondsAvailable": gifted_secs_diff}},
                )

    async def update_event_webhook_urls(self, org: Organization, urls: OrgWebhookUrls):
        """Update organization event webhook URLs"""
        return await self.orgs.find_one_and_update(
            {"_id": org.id},
            {"$set": {"webhookUrls": urls.dict(exclude_unset=True)}},
            return_document=ReturnDocument.AFTER,
        )

    async def handle_new_user_invite(self, invite_token: UUID, user: User):
        """Handle invite from a new user"""
        new_user_invite = await self.invites.get_valid_invite(invite_token, user.email)
        await self.add_user_by_invite(new_user_invite, user)
        await self.invites.remove_invite(invite_token)
        return new_user_invite

    async def add_user_by_invite(self, invite: InvitePending, user: User):
        """Add user to an org from an InvitePending, if any.

        If there's no org to add to (eg. superuser invite), just return.
        """
        if not invite.oid:
            return

        org = await self.get_org_by_id(invite.oid)
        if not org:
            raise HTTPException(
                status_code=400, detail="Invalid Invite Code, No Such Organization"
            )

        await self.add_user_to_org(org, user.id, invite.role)
        return True

    async def add_user_to_org(
        self, org: Organization, userid: UUID, role: Optional[UserRole] = None
    ):
        """Add user to organization with specified role"""
        org.users[str(userid)] = role or UserRole.OWNER
        await self.update_users(org)

    async def get_org_owners(self, org: Organization):
        """Return list of org's Owner users."""
        org_owners = []
        for key, value in org.users.items():
            if value == UserRole.OWNER:
                org_owners.append(key)
        return org_owners

    async def get_max_pages_per_crawl(self, oid: UUID):
        """Return org-specific max pages per crawl setting or 0."""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.quotas.maxPagesPerCrawl
        return 0

    async def inc_org_bytes_stored(self, oid: UUID, size: int, type_="crawl"):
        """Increase org bytesStored count (pass negative value to subtract)."""
        if type_ == "crawl":
            await self.orgs.find_one_and_update(
                {"_id": oid}, {"$inc": {"bytesStored": size, "bytesStoredCrawls": size}}
            )
        elif type_ == "upload":
            await self.orgs.find_one_and_update(
                {"_id": oid},
                {"$inc": {"bytesStored": size, "bytesStoredUploads": size}},
            )
        elif type_ == "profile":
            await self.orgs.find_one_and_update(
                {"_id": oid},
                {"$inc": {"bytesStored": size, "bytesStoredProfiles": size}},
            )
        return await self.storage_quota_reached(oid)

    # pylint: disable=invalid-name
    async def storage_quota_reached(self, oid: UUID) -> bool:
        """Return boolean indicating if storage quota is met or exceeded."""
        quota = await self.get_org_storage_quota(oid)
        if not quota:
            return False

        org = await self.orgs.find_one({"_id": oid})
        org = Organization.from_dict(org)

        if org.bytesStored >= quota:
            return True

        return False

    async def get_monthly_crawl_exec_seconds(self, oid: UUID) -> int:
        """Return monthlyExecSeconds for current month"""
        org = await self.orgs.find_one({"_id": oid})
        org = Organization.from_dict(org)
        yymm = datetime.utcnow().strftime("%Y-%m")
        try:
            return org.monthlyExecSeconds[yymm]
        except KeyError:
            return 0

    async def exec_mins_quota_reached(
        self, oid: UUID, include_extra: bool = True
    ) -> bool:
        """Return bool for if execution minutes quota is reached"""
        if include_extra:
            gifted_seconds = await self.get_gifted_exec_secs_available(oid)
            if gifted_seconds:
                return False

            extra_seconds = await self.get_extra_exec_secs_available(oid)
            if extra_seconds:
                return False

        monthly_quota = await self.get_org_exec_mins_monthly_quota(oid)
        if monthly_quota:
            monthly_exec_seconds = await self.get_monthly_crawl_exec_seconds(oid)
            monthly_exec_minutes = math.floor(monthly_exec_seconds / 60)
            if monthly_exec_minutes >= monthly_quota:
                return True

        return False

    async def get_org_storage_quota(self, oid: UUID) -> int:
        """return max allowed concurrent crawls, if any"""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.quotas.storageQuota
        return 0

    async def get_org_exec_mins_monthly_quota(self, oid: UUID) -> int:
        """return max allowed execution mins per month, if any"""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.quotas.maxExecMinutesPerMonth
        return 0

    async def get_extra_exec_secs_available(self, oid: UUID) -> int:
        """return extra billable rollover seconds available, if any"""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.extraExecSecondsAvailable
        return 0

    async def get_gifted_exec_secs_available(self, oid: UUID) -> int:
        """return gifted rollover seconds available, if any"""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.giftedExecSecondsAvailable
        return 0

    async def set_origin(self, org: Organization, request: Request):
        """Get origin from request and store in db for use in event webhooks"""
        headers = request.headers
        scheme = headers.get("X-Forwarded-Proto")
        host = headers.get("Host")

        if not scheme or not host:
            origin = os.environ.get("APP_ORIGIN")
        else:
            origin = f"{scheme}://{host}"

        await self.orgs.find_one_and_update(
            {"_id": org.id}, {"$set": {"origin": origin}}
        )

    async def inc_org_time_stats(self, oid, duration, is_exec_time=False):
        """inc crawl duration stats for org

        Overage is applied only to crawlExecSeconds - monthlyExecSeconds,
        giftedExecSeconds, and extraExecSeconds are added to only up to quotas
        """
        # pylint: disable=too-many-return-statements
        key = "crawlExecSeconds" if is_exec_time else "usage"
        yymm = datetime.utcnow().strftime("%Y-%m")
        await self.orgs.find_one_and_update(
            {"_id": oid}, {"$inc": {f"{key}.{yymm}": duration}}
        )

        if not is_exec_time:
            return

        org = await self.get_org_by_id(oid)

        monthly_exec_secs_used = await self.get_monthly_crawl_exec_seconds(oid)

        monthly_quota_mins = await self.get_org_exec_mins_monthly_quota(oid)
        monthly_quota_secs = monthly_quota_mins * 60

        if (
            not monthly_quota_secs
            and not org.giftedExecSecondsAvailable
            and not org.extraExecSecondsAvailable
        ):
            return

        monthly_remaining_time = monthly_quota_secs - monthly_exec_secs_used

        # If adding duration won't pass monthly quota, add duration and return
        if duration <= monthly_remaining_time:
            return await self.orgs.find_one_and_update(
                {"_id": oid}, {"$inc": {f"monthlyExecSeconds.{yymm}": duration}}
            )

        # Otherwise, add execution seconds to monthlyExecSeconds up to quota
        await self.orgs.find_one_and_update(
            {"_id": oid},
            {"$inc": {f"monthlyExecSeconds.{yymm}": monthly_remaining_time}},
        )

        if not org.giftedExecSecondsAvailable and not org.extraExecSecondsAvailable:
            return

        secs_over_quota = duration - monthly_remaining_time

        # If we've surpassed monthly base quota, use gifted and extra exec minutes
        # in that order if available, track their usage per month, and recalculate
        # extraExecSecondsAvailable and giftedExecSecondsAvailable as needed
        gifted_secs_available = org.giftedExecSecondsAvailable
        if gifted_secs_available:
            if secs_over_quota <= gifted_secs_available:
                return await self.orgs.find_one_and_update(
                    {"_id": oid},
                    {
                        "$inc": {
                            f"giftedExecSeconds.{yymm}": secs_over_quota,
                            "giftedExecSecondsAvailable": -secs_over_quota,
                        }
                    },
                )

            # If seconds over quota is higher than gifted seconds available,
            # use remaining gifted gifted time and then move on
            await self.orgs.find_one_and_update(
                {"_id": oid},
                {
                    "$inc": {f"giftedExecSeconds.{yymm}": gifted_secs_available},
                    "$set": {"giftedExecSecondsAvailable": 0},
                },
            )
            secs_over_quota = secs_over_quota - gifted_secs_available

        # If we still have an overage, apply to extra up to quota
        secs_to_use = min(secs_over_quota, org.extraExecSecondsAvailable)
        if secs_to_use:
            return await self.orgs.find_one_and_update(
                {"_id": oid},
                {
                    "$inc": {
                        f"extraExecSeconds.{yymm}": secs_to_use,
                        "extraExecSecondsAvailable": -secs_to_use,
                    }
                },
            )

    async def get_max_concurrent_crawls(self, oid):
        """return max allowed concurrent crawls, if any"""
        org = await self.orgs.find_one({"_id": oid})
        if org:
            org = Organization.from_dict(org)
            return org.quotas.maxConcurrentCrawls
        return 0

    async def get_org_metrics(self, org: Organization):
        """Calculate and return org metrics"""
        # pylint: disable=too-many-locals
        storage_quota = await self.get_org_storage_quota(org.id)
        max_concurrent_crawls = await self.get_max_concurrent_crawls(org.id)

        # Calculate these counts in loop to avoid having db iterate through
        # archived items several times.
        archived_item_count = 0
        crawl_count = 0
        upload_count = 0
        page_count = 0

        async for item in self.crawls_db.find({"oid": org.id}):
            if item["state"] not in SUCCESSFUL_STATES:
                continue
            archived_item_count += 1
            type_ = item.get("type")
            if type_ == "crawl":
                crawl_count += 1
            if type_ == "upload":
                upload_count += 1
            if item.get("stats"):
                page_count += item.get("stats", {}).get("done", 0)

        profile_count = await self.profiles_db.count_documents({"oid": org.id})
        workflows_running_count = await self.crawls_db.count_documents(
            {"oid": org.id, "state": {"$in": RUNNING_STATES}}
        )
        workflows_queued_count = await self.crawls_db.count_documents(
            {"oid": org.id, "state": {"$in": STARTING_STATES}}
        )
        collections_count = await self.colls_db.count_documents({"oid": org.id})
        public_collections_count = await self.colls_db.count_documents(
            {"oid": org.id, "isPublic": True}
        )

        return {
            "storageUsedBytes": org.bytesStored,
            "storageUsedCrawls": org.bytesStoredCrawls,
            "storageUsedUploads": org.bytesStoredUploads,
            "storageUsedProfiles": org.bytesStoredProfiles,
            "storageQuotaBytes": storage_quota,
            "archivedItemCount": archived_item_count,
            "crawlCount": crawl_count,
            "uploadCount": upload_count,
            "pageCount": page_count,
            "profileCount": profile_count,
            "workflowsRunningCount": workflows_running_count,
            "maxConcurrentCrawls": max_concurrent_crawls,
            "workflowsQueuedCount": workflows_queued_count,
            "collectionsCount": collections_count,
            "publicCollectionsCount": public_collections_count,
        }

    async def get_all_org_slugs(self):
        """Return list of all org slugs."""
        slugs = await self.orgs.distinct("slug", {})
        return {"slugs": slugs}

    async def get_org_slugs_by_ids(self):
        """Return dict with {id: slug} for all orgs."""
        slug_id_map = {}
        async for org in self.orgs.find({}):
            slug_id_map[org["_id"]] = org["slug"]
        return slug_id_map


# ============================================================================
# pylint: disable=too-many-statements
def init_orgs_api(app, mdb, user_manager, invites, user_dep):
    """Init organizations api router for /orgs"""
    # pylint: disable=too-many-locals,invalid-name

    ops = OrgOps(mdb, invites)

    async def org_dep(oid: UUID, user: User = Depends(user_dep)):
        org = await ops.get_org_for_user_by_id(oid, user)
        if not org:
            raise HTTPException(status_code=404, detail="org_not_found")
        if not org.is_viewer(user):
            raise HTTPException(
                status_code=403,
                detail="User does not have permission to view this organization",
            )

        return org

    async def org_crawl_dep(
        org: Organization = Depends(org_dep), user: User = Depends(user_dep)
    ):
        if not org.is_crawler(user):
            raise HTTPException(
                status_code=403, detail="User does not have permission to modify crawls"
            )

        return org

    async def org_owner_dep(
        org: Organization = Depends(org_dep), user: User = Depends(user_dep)
    ):
        if not org.is_owner(user):
            raise HTTPException(
                status_code=403,
                detail="User does not have permission to perform this action",
            )

        return org

    async def org_public(oid: UUID):
        org = await ops.get_org_by_id(oid)
        if not org:
            raise HTTPException(status_code=404, detail="org_not_found")

        return org

    router = APIRouter(
        prefix="/orgs/{oid}",
        dependencies=[Depends(org_dep)],
        responses={404: {"description": "Not found"}},
    )

    ops.router = router
    ops.org_viewer_dep = org_dep
    ops.org_crawl_dep = org_crawl_dep
    ops.org_owner_dep = org_owner_dep
    ops.org_public = org_public

    @app.get("/orgs", tags=["organizations"], response_model=PaginatedResponse)
    async def get_orgs(
        user: User = Depends(user_dep),
        pageSize: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
    ):
        results, total = await ops.get_orgs_for_user(
            user, page_size=pageSize, page=page
        )
        serialized_results = [
            await res.serialize_for_user(user, user_manager) for res in results
        ]
        return paginated_format(serialized_results, total, page, pageSize)

    @app.post("/orgs/create", tags=["organizations"])
    async def create_org(
        new_org: CreateOrg,
        user: User = Depends(user_dep),
    ):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")

        id_ = uuid4()

        slug = new_org.slug
        if not slug:
            slug = slug_from_name(new_org.name)

        org = Organization(
            id=id_, name=new_org.name, slug=slug, users={}, storage=ops.default_primary
        )
        if not await ops.add_org(org):
            return {"added": False, "error": "already_exists"}

        return {"id": id_, "added": True}

    @router.get("", tags=["organizations"])
    async def get_org(
        org: Organization = Depends(org_dep), user: User = Depends(user_dep)
    ):
        org_out = await org.serialize_for_user(user, user_manager)
        org_out.storageQuotaReached = await ops.storage_quota_reached(org.id)
        org_out.execMinutesQuotaReached = await ops.exec_mins_quota_reached(org.id)
        return org_out

    @router.post("/rename", tags=["organizations"])
    async def rename_org(
        rename: RenameOrg,
        org: Organization = Depends(org_owner_dep),
    ):
        org.name = rename.name
        if rename.slug:
            org.slug = rename.slug
        else:
            org.slug = slug_from_name(rename.name)

        try:
            await ops.update_slug_and_name(org)
        except DuplicateKeyError:
            # pylint: disable=raise-missing-from
            raise HTTPException(status_code=400, detail="duplicate_org_name")

        return {"updated": True}

    @router.post("/quotas", tags=["organizations"])
    async def update_quotas(
        quotas: OrgQuotas,
        org: Organization = Depends(org_owner_dep),
        user: User = Depends(user_dep),
    ):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")

        await ops.update_quotas(org, quotas)

        return {"updated": True}

    @router.post("/event-webhook-urls", tags=["organizations"])
    async def update_event_webhook_urls(
        urls: OrgWebhookUrls,
        request: Request,
        org: Organization = Depends(org_owner_dep),
    ):
        await ops.set_origin(org, request)
        result = await ops.update_event_webhook_urls(org, urls)

        if not result:
            return {"updated": False}

        return {"updated": True}

    @router.patch("/user-role", tags=["organizations"])
    async def set_role(
        update: UpdateRole,
        org: Organization = Depends(org_owner_dep),
        user: User = Depends(user_dep),
    ):
        other_user = await user_manager.get_by_email(update.email)
        if not other_user:
            raise HTTPException(
                status_code=400, detail="No user found for specified e-mail"
            )

        if other_user.email == user.email:
            raise HTTPException(status_code=400, detail="Can't change own role!")

        await ops.add_user_to_org(org, other_user.id, update.role)

        return {"updated": True}

    @router.post("/invite", tags=["invites"])
    async def invite_user_to_org(
        invite: InviteToOrgRequest,
        request: Request,
        org: Organization = Depends(org_owner_dep),
        user: User = Depends(user_dep),
    ):
        if await invites.invite_user(
            invite,
            user,
            user_manager,
            org=org,
            allow_existing=True,
            headers=request.headers,
        ):
            return {"invited": "new_user"}

        return {"invited": "existing_user"}

    @app.post("/orgs/invite-accept/{token}", tags=["invites"])
    async def accept_invite(token: str, user: User = Depends(user_dep)):
        invite = await invites.accept_user_invite(user, token, user_manager)

        await ops.add_user_by_invite(invite, user)
        return {"added": True}

    @router.get("/invites", tags=["invites"])
    async def get_pending_org_invites(
        org: Organization = Depends(org_owner_dep),
        pageSize: int = DEFAULT_PAGE_SIZE,
        page: int = 1,
    ):
        pending_invites, total = await user_manager.invites.get_pending_invites(
            org, page_size=pageSize, page=page
        )
        return paginated_format(pending_invites, total, page, pageSize)

    @router.post("/invites/delete", tags=["invites"])
    async def delete_invite(
        invite: RemovePendingInvite, org: Organization = Depends(org_owner_dep)
    ):
        # URL decode email just in case
        email = urllib.parse.unquote(invite.email)
        result = await user_manager.invites.remove_invite_by_email(email, org.id)
        if result.deleted_count > 0:
            return {
                "removed": True,
                "count": result.deleted_count,
            }
        raise HTTPException(status_code=404, detail="invite_not_found")

    @router.post("/remove", tags=["invites"])
    async def remove_user_from_org(
        remove: RemoveFromOrg, org: Organization = Depends(org_owner_dep)
    ) -> dict[str, bool]:
        other_user = await user_manager.get_by_email(remove.email)

        if org.is_owner(other_user):
            org_owners = await ops.get_org_owners(org)
            if len(org_owners) == 1:
                raise HTTPException(
                    status_code=400, detail="Can't remove only owner from org"
                )
        try:
            del org.users[str(other_user.id)]
        except KeyError:
            # pylint: disable=raise-missing-from
            raise HTTPException(status_code=404, detail="no_such_org_user")

        await ops.update_users(org)
        return {"removed": True}

    @router.post("/add-user", tags=["invites"])
    async def add_new_user_to_org(
        invite: AddToOrgRequest,
        org: Organization = Depends(org_owner_dep),
        user: User = Depends(user_dep),
    ):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")

        await user_manager.create_non_super_user(
            invite.email, invite.password, invite.name
        )
        update_role = UpdateRole(role=invite.role, email=invite.email)
        await set_role(update_role, org, user)
        return {"added": True}

    @router.get("/metrics", tags=["organizations"], response_model=OrgMetrics)
    async def get_org_metrics(org: Organization = Depends(org_dep)):
        return await ops.get_org_metrics(org)

    @app.get("/orgs/slugs", tags=["organizations"])
    async def get_all_org_slugs(user: User = Depends(user_dep)):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")
        return await ops.get_all_org_slugs()

    @app.get("/orgs/slug-lookup", tags=["organizations"])
    async def get_all_org_slugs_with_ids(user: User = Depends(user_dep)):
        if not user.is_superuser:
            raise HTTPException(status_code=403, detail="Not Allowed")
        return await ops.get_org_slugs_by_ids()

    return ops
