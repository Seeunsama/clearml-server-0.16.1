import json

from pymongo.database import Database, Collection


def migrate_auth(db: Database):
    collection: Collection = db["user"]
    if "name_1_company_1" in [doc["name"] for doc in collection.list_indexes()]:
        collection.drop_index("name_1_company_1")


def migrate_backend(db: Database):
    collection: Collection = db["user"]
    users = collection.find(
        {"preferences": {"$exists": True, "$ne": None, "$type": "object"}}
    )
    for doc in users:
        collection.update_one(
            {"_id": doc["_id"]}, {"$set": {"preferences": json.dumps(doc["preferences"])}}
        )
