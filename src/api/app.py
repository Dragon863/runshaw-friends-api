import json
import re
from typing import Optional
import aiohttp
import asyncpg
import dotenv
from appwrite.client import Client
from appwrite.services.account import Account
from appwrite.services.users import Users
from fastapi import FastAPI, Depends, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from utils.env import getFromEnv
from utils.db.init import init_db
from utils.models import *
from utils.notifications import sendNotification
from fastapi.security.http import HTTPBearer
from apitally.fastapi import ApitallyMiddleware

dotenv.load_dotenv()


adminClient = Client()
adminClient.set_endpoint(getFromEnv("APPWRITE_ENDPOINT"))
adminClient.set_project(getFromEnv("APPWRITE_PROJECT_ID"))
adminClient.set_key(getFromEnv("APPWRITE_API_KEY"))
users = Users(adminClient)

DATABASE_URL = getFromEnv("DATABASE_URL")


async def connect_db():
    return await asyncpg.create_pool(
        DATABASE_URL,
        user="postgres",
        password=getFromEnv("DATABASE_PWD"),
    )


db_pool = None


async def startup_event():
    global db_pool
    db_pool = await connect_db()
    await init_db(db_pool)


async def shutdown_event():
    if db_pool:
        await db_pool.close()


app = FastAPI(
    title="My Runshaw API",
    description="The API used by the backend of the My Runshaw app to manage friendships, timetables, push notifications, buses and more. To authenticate with this API, you must provide an Appwrite JWT in the Authorization header.",
    version=getFromEnv("API_VERSION"),
    on_startup=[startup_event],
    on_shutdown=[shutdown_event],
    contact={
        "name": "Daniel Benge",
        "url": "https://danieldb.uk",
    },
    terms_of_service="https://privacy.danieldb.uk/terms",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    ApitallyMiddleware,
    client_id=getFromEnv("APITALLY_CLIENT_ID"),
    env="prod",
)

security = HTTPBearer()


async def authenticate(req: Request):
    """Authenticate users with their JWT from Appwrite"""
    try:
        authorization = req.headers.get("Authorization", None)
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail="Unauthorized. Please provide an Authorization header.",
            )
        if "Bearer" in authorization:
            token = authorization.split(" ")[1]
        else:
            token = authorization
        authClient = Client()
        authClient.set_endpoint(getFromEnv("APPWRITE_ENDPOINT"))
        authClient.set_project(getFromEnv("APPWRITE_PROJECT_ID"))
        authClient.set_jwt(token)
        account = Account(authClient)
        user = account.get()
        req.user_id = user["$id"]
        return user
    except Exception as e:
        raise HTTPException(status_code=401, detail="Unauthorized; invalid token.")


@app.get(
    "/api/friends",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def get_friends(req: Request, auth_user: dict = Depends(authenticate)):
    """Fetch friends for the authenticated user."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM friend_requests 
                    WHERE (sender_id = $1 OR receiver_id = $1)
                    AND status = 'accepted'
                    ORDER BY updated_at ASC
                """,
                req.user_id.lower(),
            )
            return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch friends")


@app.get(
    "/api/name/get/{user_id}",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def get_name(req: Request, user_id: str, auth_user: dict = Depends(authenticate)):
    """Fetch the name of a user by their ID."""
    try:
        users = Users(adminClient)
        user: dict = users.get(user_id)
        return JSONResponse({"name": user["name"]})
    except Exception as e:
        return JSONResponse({"error": "User not found"}, status_code=404)


@app.post(
    "/api/name/get/batch",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def get_names(
    req: Request, body: BatchGetBody, auth_user: dict = Depends(authenticate)
):
    """Fetch the names of multiple users by their IDs. Called on app startup."""
    try:
        names = {}
        async with aiohttp.ClientSession() as session:
            for user_id in body.user_ids:
                try:
                    api_res = await session.get(
                        f"{getFromEnv('APPWRITE_ENDPOINT')}/users/{user_id}",
                        headers={
                            "x-appwrite-project": getFromEnv("APPWRITE_PROJECT_ID"),
                            "x-appwrite-key": getFromEnv("APPWRITE_API_KEY"),
                            "user-agent": "ApppwritePythonSDK/7.0.0",
                            "x-sdk-name": "Python",
                            "x-sdk-platform": "server",
                            "x-sdk-language": "python",
                            "x-sdk-version": "7.0.0",
                            "content-type": "application/json",
                        },
                    )
                    user = await api_res.json()
                    names[user_id] = user["name"]
                except Exception as e:
                    names[user_id] = "Unknown User"
            return JSONResponse(
                names,
                media_type="application/json",
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
    except Exception as e:
        return JSONResponse({"error": "Failed to fetch names"}, status_code=500)


@app.get(
    "/api/exists/{user_id}",
    tags=["Auth"],
)
def user_exists(user_id):
    try:
        users = Users(adminClient)
        users.get(user_id)
        return JSONResponse({"exists": True})
    except Exception as e:
        return JSONResponse({"exists": False}, 404)


@app.post(
    "/api/block",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def unfriend_user(
    req: Request,
    blocked_id_body: BlockedID,
):
    """Unfriends a user by their ID. Route name preserved for backward compatibility"""

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """DELETE FROM friend_requests 
                WHERE (sender_id = $1 AND receiver_id = $2) 
                    OR (sender_id = $2 AND receiver_id = $1)""",
                req.user_id.lower(),
                blocked_id_body.blocked_id.lower(),
            )
            await conn.execute(
                "INSERT INTO blocked_users (blocker_id, blocked_id) VALUES ($1, $2)",
                req.user_id.lower(),
                blocked_id_body.blocked_id.lower(),
            )
            return JSONResponse(
                {"message": "User blocked and friendship removed (if applicable)"},
                201,
            )
    except Exception as e:
        return JSONResponse({"error": "You are not friends with this user"}, 409)


@app.delete(
    "/api/block",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def unblock_user(
    req: Request,
    blocked_id: BlockedID,
):
    """Block a user by their ID."""

    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM blocked_users WHERE blocker_id = $1 AND blocked_id = $2",
                (req.user_id.lower(), blocked_id.lower()),
            )
            return JSONResponse({"message": "User unblocked successfully"}, 201)
    except Exception as e:
        return JSONResponse({"error": "User is not blocked"}, 409)


@app.post(
    "/api/timetable",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Timetable"],
)
async def add_timetable(
    req: Request,
    timetable: Timetable,
):
    """Add a timetable to the authenticated user's account."""
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO timetables (user_id, timetable)
                VALUES ($1, $2)
                ON CONFLICT (user_id)
                DO UPDATE SET timetable = $2, updated_at = CURRENT_TIMESTAMP""",
                req.user_id.lower(),
                json.dumps(timetable.dict()["timetable"]),
            )
            return JSONResponse({"message": "Timetable uploaded successfully"}, 201)
    except Exception as e:
        return JSONResponse({"error": "Failed to upload timetable"}, 500)


@app.get(
    "/api/timetable",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Timetable"],
)
async def get_timetable(
    req: Request,
    user_id: Optional[str] = None,
):
    """
    Fetch the timetable for a user. If `user_id` is not provided, fetch the timetable
    for the requester. Only allow access if the requester is the user or their friend.
    """

    user_id = user_id or req.user_id  # If user_id_for is None, use the requester's ID

    if user_id != req.user_id:
        async with db_pool.acquire() as conn:
            friendship = await conn.fetchrow(
                """SELECT * FROM friend_requests
                WHERE status = 'accepted'
                AND ((sender_id = $1 AND receiver_id = $2)
                OR (sender_id = $2 AND receiver_id = $1))
                """,
                req.user_id.lower(),
                user_id.lower(),
            )
            if not friendship:
                return JSONResponse({"error": "Unauthorised access"}, 403)

    async with db_pool.acquire() as conn:
        timetable = await conn.fetchval(
            "SELECT timetable FROM timetables WHERE user_id = $1", user_id.lower()
        )

    if not timetable:
        return JSONResponse({"error": "Timetable not found"}, 404)

    return JSONResponse({"timetable": json.loads(timetable)})


@app.post(
    "/api/timetable/batch_get",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Timetable"],
)
async def batch_get_timetable(req: Request, request_body: BatchGetBody):
    """Fetch the timetables for multiple users. Called on app startup"""
    user_ids = request_body.user_ids
    if not req.user_id:
        return JSONResponse({"error": "No user IDs provided"}, 400)

    async with db_pool.acquire() as conn:
        for user_id in user_ids:
            friendship = await conn.fetchrow(
                """SELECT * FROM friend_requests
                WHERE status = 'accepted'
                AND ((sender_id = $1 AND receiver_id = $2)
                OR (sender_id = $2 AND receiver_id = $1))
                """,
                req.user_id.lower(),
                user_id.lower(),
            )
            if not friendship and not user_id == req.user_id:
                return JSONResponse({"error": "Unauthorised access"}, 403)

        timetables = await conn.fetch(
            """
            SELECT user_id, timetable
            FROM timetables
            WHERE user_id = ANY($1::text[])
            """,
            user_ids,
        )

    # Ensure all requested users have a timetable entry
    for user_id in user_ids:
        if not any(row["user_id"] == user_id for row in timetables):
            timetables.append(
                {
                    "user_id": user_id,
                    "timetable": '{"data": []}',  # Ensuring valid JSON
                }
            )

    return JSONResponse(
        {
            timetable["user_id"]: {
                "data": json.loads(timetable["timetable"] or '{"data": []}')["data"],
            }
            for timetable in timetables
        }
    )


@app.get(
    "/api/bus",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Buses"],
)
async def get_buses(req: Request):
    """
    Gets bus bay information from the database
    """
    async with db_pool.acquire() as conn:
        buses = await conn.fetch("SELECT * FROM bus")
        return [dict(bus) for bus in buses]


@app.get(
    "/api/bus/for",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Buses"],
)
async def get_bus_for(req: Request, user_id: str):
    """
    Gets the bus number for a user with the given ID as a query parameter
    """
    async with db_pool.acquire() as conn:
        friendship = await conn.fetchrow(
            """SELECT * FROM friend_requests
            WHERE status = 'accepted'
            AND ((sender_id = $1 AND receiver_id = $2)
            OR (sender_id = $2 AND receiver_id = $1))
            """,
            req.user_id.lower(),
            user_id.lower(),
        )
        if not friendship:
            return JSONResponse({"error": "Unauthorised access"}, 403)

        buses = await conn.fetch(
            "SELECT bus FROM extra_bus_subscriptions WHERE user_id = $1", user_id
        )

    users = Users(adminClient)
    user = users.get(user_id)
    preferences: dict = user.get("prefs", {"bus_number": None})

    toReturn = []
    if "bus_number" in preferences:
        if preferences["bus_number"]:
            toReturn.append(preferences["bus_number"])

    for bus in buses:
        toReturn.append(bus["bus"])

    if len(toReturn) == 0:
        return JSONResponse("Not set")
    return JSONResponse(", ".join(toReturn))


@app.post(
    "/api/extra_buses/add",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Buses"],
)
async def add_extra_buses(req: Request, buses: ExtraBusRequestBody):
    """
    Subscribe to a bus number for push notifications
    """
    if not buses.bus_number:
        return JSONResponse({"error": "bus_number is required"}, 400)

    bus_number = buses.bus_number

    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "INSERT INTO extra_bus_subscriptions (user_id, bus) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                req.user_id,
                bus_number,
            )
            return JSONResponse({"message": "Bus added successfully"}, 201)
        except Exception as e:
            return JSONResponse({"error": "Bus already added"}, 409)


@app.post(
    "/api/extra_buses/remove",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Buses"],
)
async def remove_extra_buses(req: Request, buses: ExtraBusRequestBody):
    """
    Unsubscribe from a bus number for push notifications
    """
    if not buses.bus_number:
        return JSONResponse({"error": "bus_number is required"}, 400)

    bus_number = buses.bus_number

    async with db_pool.acquire() as conn:
        try:
            await conn.execute(
                "DELETE FROM extra_bus_subscriptions WHERE user_id = $1 AND bus = $2",
                req.user_id,
                bus_number,
            )
            return JSONResponse({"message": "Bus removed successfully"}, 201)
        except Exception as e:
            return JSONResponse({"error": "Bus not found"}, 404)


@app.get(
    "/api/extra_buses/get",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Buses"],
)
async def get_extra_buses(req: Request):
    """
    Get the extra bus numbers the user is subscribed to for push notifications
    """
    async with db_pool.acquire() as conn:
        buses = await conn.fetch(
            "SELECT bus FROM extra_bus_subscriptions WHERE user_id = $1", req.user_id
        )
        return [dict(bus) for bus in buses]


@app.post(
    "/api/account/close",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Compliance"],
)
async def close_account(req: Request):
    """Close the authenticated user's account."""
    try:
        users = Users(adminClient)
        users.delete(req.user_id)

        async with db_pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM blocked_users WHERE blocker_id = $1 OR blocked_id = $1",
                req.user_id,
            )
            await conn.execute(
                "DELETE FROM friend_requests WHERE sender_id = $1 OR receiver_id = $1",
                req.user_id,
            )
            await conn.execute("DELETE FROM timetables WHERE user_id = $1", req.user_id)

            app_id = getFromEnv("ONESIGNAL_APP_ID")
            alias_label = "external_id"
            alias_id = req.user_id

            url = f"https://api.onesignal.com/apps/{app_id}/users/by/{alias_label}/{alias_id}"
            headers = {
                "Authorization": f"Bearer {getFromEnv('ONESIGNAL_API_KEY')}",
            }

            response = await aiohttp.ClientSession().delete(url, headers=headers)
            if response.status != 200:
                print(f"Failed to delete OneSignal user: {req.user_id}")

            return JSONResponse({"message": "Account deleted successfully"}, 200)

    except Exception as e:
        return JSONResponse({"error": "Failed to close account"}, 500)


@app.post(
    "/api/cache/get/pfp-versions",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Profile Pictures"],
)
async def get_pfp_versions(req: Request, body: BatchGetBody):
    """This route will be called upon opening the app. It will return the current version of the profile pictures for the users provided in the JSON body under the key "user_ids"""
    if not body.user_ids:
        return JSONResponse({"error": "user_ids is required"}, 400)

    async with db_pool.acquire() as conn:
        versions = await conn.fetch(
            "SELECT user_id, version FROM profile_pics WHERE user_id = ANY($1::text[])",
            body.user_ids,
        )

    for user_id in body.user_ids:
        if not any([row["user_id"] == user_id for row in versions]):
            versions.append(
                {
                    "user_id": user_id,
                    "version": 0,
                }
            )
            # Add an empty version for users that don't have one in the DB (often due to not updating since an old release or not at all)

    return JSONResponse(
        {version["user_id"]: version["version"] for version in versions}
    )


@app.post(
    "/api/cache/update/pfp-version",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Profile Pictures"],
)
async def update_pfp_version(req: Request):
    """Update the version of a user's profile picture."""
    async with db_pool.acquire() as conn:
        current_version = await conn.fetchval(
            "SELECT version FROM profile_pics WHERE user_id = $1", req.user_id
        )
        if not current_version:
            await conn.execute(
                "INSERT INTO profile_pics (user_id, version) VALUES ($1, $2)",
                req.user_id,
                1,
            )
        else:
            new_version = int(current_version) + 1

            await conn.execute(
                "UPDATE profile_pics SET version = $1 WHERE user_id = $2",
                new_version,
                req.user_id,
            )

        return JSONResponse(
            {"message": "Profile picture version updated successfully"}, 200
        )


@app.post(
    "/api/friend-requests",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def send_friend_request(req: Request, request_body: FriendRequestBody):
    """
    Send a friend request to a user by their ID.
    """
    receiver = request_body.receiver_id.lower()
    sender = req.user_id.lower()

    if not receiver:
        return JSONResponse({"error": "receiver_id is required"}, 400)

    if receiver == sender:
        return JSONResponse({"error": "Cannot send a friend request to yourself"}, 400)

    try:
        users = Users(adminClient)
        users.get(receiver)
    except Exception as e:
        return JSONResponse({"error": "Invalid receiver_id"}, 404)

    async with db_pool.acquire() as conn:
        try:
            # First check if a friend request exists in either direction
            existing_request = await conn.fetchrow(
                "SELECT * FROM friend_requests WHERE (sender_id = $1 AND receiver_id = $2) OR (sender_id = $2 AND receiver_id = $1)",
                sender,
                receiver,
            )
            if existing_request:
                return JSONResponse({"error": "Friend request already exists"}, 409)

            await conn.execute(
                """
                    INSERT INTO friend_requests (sender_id, receiver_id) VALUES ($1, $2)
                """,
                sender,
                receiver,
            )
            sendNotification(
                message="You have a new friend request!",
                userIds=[receiver],
                title="Friend Request",
                ttl=60 * 60 * 24 * 2,
                small_icon="friend",  # Fun story: this used to default to a bus icon, which seemed vaguely threatening for android users!
            )
            return JSONResponse({"message": "Friend request sent"}, 201)
        except Exception as e:
            return JSONResponse(
                {"error": "An error occurred while sending the friend request"}, 500
            )


@app.get(
    "/api/friend-requests",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def get_friend_requests(req: Request, status: str = "pending"):
    """Fetch friend requests for the authenticated user."""
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM friend_requests WHERE receiver_id = $1 AND status = $2",
                req.user_id,
                status,
            )
            return [dict(row) for row in rows]
    except Exception as e:
        return JSONResponse({"error": "Failed to fetch friend requests"}, 500)


@app.put(
    "/api/friend-requests/{request_id}",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Friends"],
)
async def handle_friend_request(
    req: Request, request_id: int, request_body: FriendRequestHandleBody
):
    """
    Accept or decline a friend request by its ID.
    """
    action = request_body.action
    if action not in ["accept", "decline"]:
        return JSONResponse({"error": "Invalid action"}, 400)

    async with db_pool.acquire() as conn:
        request = await conn.fetchrow(
            "SELECT * FROM friend_requests WHERE id = $1", request_id
        )
        if not request:
            return JSONResponse({"error": "Friend request not found"}, 404)

        if request["receiver_id"] != req.user_id:
            return JSONResponse({"error": "Unauthorised access"}, 403)

        if request["status"] != "pending":
            return JSONResponse(
                {"error": "Friend request has already been handled"}, 409
            )

        if action == "accept":
            await conn.execute(
                "UPDATE friend_requests SET status = 'accepted' WHERE id = $1",
                request_id,
            )
            sendNotification(
                message="Your friend request has been accepted!",
                userIds=[request["sender_id"]],
                title="Friend Request Accepted",
                ttl=60 * 60 * 24 * 2,
                small_icon="friend",
            )
            return JSONResponse({"message": "Friend request accepted"}, 200)
        else:
            try:
                await conn.execute(
                    """DELETE FROM friend_requests 
                    WHERE (sender_id = $1 AND receiver_id = $2) 
                        OR (sender_id = $2 AND receiver_id = $1)""",
                    request["sender_id"],
                    request["receiver_id"],
                )

                sendNotification(
                    message="Your friend request has been declined.",
                    userIds=[request["sender_id"]],
                    title="Friend Request Declined",
                    ttl=60 * 60 * 24 * 2,
                    small_icon="friend",
                )
                return JSONResponse({"message": "Friend request declined"}, 200)
            except Exception as e:
                return JSONResponse({"error": "Failed to decline friend request"}, 500)


@app.post(
    "/api/timetable/associate",
    dependencies=[Depends(authenticate), Depends(security)],
    tags=["Timetable"],
)
async def get_meta(req: Request, body: TimetableAssociationBody):
    """New in version 1.3.0 as migration to daily updating of timetables begins"""
    pattern = re.compile(r"https://webservices\.runshaw\.ac\.uk/timetable\.ashx\?id=.*")
    if not pattern.match(body.url):
        return JSONResponse(
            {"error": "Invalid URL. Must be a Runshaw timetable URL"}, 400
        )
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO timetable_associations (user_id, url)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE SET url = $2
                """,
                req.user_id,
                body.url,
            )
            return JSONResponse(
                {"message": "Timetable URL associated successfully"}, 201
            )
    except Exception as e:
        return JSONResponse({"error": "Failed to associate timetable URL"}, 500)
