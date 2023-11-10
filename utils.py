import threading
from pathlib import Path
from typing import List

import os
import json
import pymongo
from bson import ObjectId
from werkzeug.security import safe_join
from copy import deepcopy
import contextlib
import hashlib

from flask import Flask, Response, jsonify
from flask_login import UserMixin

from dotenv import load_dotenv
from discord_webhook import DiscordWebhook, DiscordEmbed

import backend.cache
from var import *

load_dotenv()

db = pymongo.MongoClient(os.getenv("MONGO_URL") if os.getenv("MONGO_URL") else "", 27017).vplan
users = db["users"]
creds = db["creds"]
VALID_SCHOOLS = [elem["_id"] for elem in list(creds.find({}))]
meta = db["meta"]


# RESPONSE WRAPPERS
def send_success(data=None) -> Response:
    if data is None:
        data = {}
    return jsonify({"success": True, "data": data})


def send_error(msg: str) -> Response:
    return jsonify({"success": False, "error": msg})


# USER MANAGEMENT
class User(UserMixin):
    def __init__(self, mongo_id: str):
        self.mongo_id = mongo_id
        self.user = None

    def get_id(self):
        return self.mongo_id

    def get_user(self):
        self.user = users.find_one({'_id': ObjectId(self.mongo_id)})
        return self.user

    def update_field(self, field, value):
        self.get_user()
        users.update_one({'_id': ObjectId(self.mongo_id)}, {"$set": {field: value}})

    def get_field(self, field):
        self.get_user()
        return self.user.get(field)

    def get_authorized_schools(self):
        self.get_user()
        return self.user.get("authorized_schools", [])

    def authorize_school(self, school_num: str):
        self.get_user()
        tmp_authorized_schools = self.user.get("authorized_schools", [])
        if school_num not in tmp_authorized_schools:
            tmp_authorized_schools.append(school_num)
            users.update_one({'_id': ObjectId(self.mongo_id)},
                             {"$set": {'authorized_schools': tmp_authorized_schools}})

    def get_settings(self):
        self.get_user()
        cur_settings = deepcopy(DEFAULT_SETTINGS)
        user_settings = self.user.get("settings", {})
        for setting, value in user_settings.items():
            cur_settings[setting] = value
        return cur_settings

    def update_settings(self, user_settings=DEFAULT_SETTINGS) -> Response:
        self.get_user()

        new_settings = {}
        for setting, setting_data in SETTINGS.items():
            validate_function = TYPE_FUNCTIONS[setting_data["type"]]["validation"]
            cur_setting = user_settings.get(setting, setting_data["default"])
            if not validate_function(cur_setting):
                send_error(f"ungültiger Wert für {cur_setting} Einstellung({setting_data['type']} erwartet)")
            conversion_function = TYPE_FUNCTIONS[setting_data["type"]]["conversion"]
            try:
                cur_setting = conversion_function(cur_setting)
            except Exception:
                send_error(f"ungültiger Wert für {cur_setting} Einstellung ({setting_data['type']} erwartet)")
            new_settings[setting] = cur_setting

        users.update_one({'_id': ObjectId(self.mongo_id)}, {"$set": {'settings': new_settings}})
        return send_success()

    # get setting for user, if setting not set get default setting
    def get_setting(self, setting_key):
        self.get_user()
        return self.user.get("settings", {}).get(setting_key, DEFAULT_SETTINGS.get(setting_key, None))

    def get_favourites(self) -> Response:
        return send_success(self.user.get("favourites", {}))
    """
        what does a favourite need:
            -> name by user (default "Favourit {n}")
            -> priority
            -> school_id, plan_type, plan_value
    """

    def set_favourites(self, favourites) -> Response:
        new_favourites = []
        for cur_favourite in favourites:
            favourite = {elem: cur_favourite.get(elem) for elem in ["school_num", "name", "priority", "plan_type", "plan_value", "preferences"]}
            for key in favourite:
                if favourite[key] is None:
                    if favourite["plan_type"] == "room_overview" and key == "plan_value":
                        continue
                    return send_error(f"{key} nicht angegeben")
            if favourite["school_num"] not in self.get_authorized_schools():
                return send_error("Schule nicht autorisiert")
            if len(favourite["name"]) > 40:
                return send_error("Name zu lang")
            if not isinstance(favourite["priority"], int) or favourite["priority"] < 0 or favourite["priority"] > 100:
                return send_error("Priorität muss eine Zahl zwischen 0 und 100 sein")
            if favourite["plan_type"] not in ["forms", "teachers", "rooms", "room_overview"]:
                return send_error("invalide Planart")
            cache = backend.cache.Cache(Path(".cache") / favourite["school_num"])
            if favourite["plan_type"] == "room_overview":
                favourite["plan_value"] = None
                favourite["preferences"] = None
                new_favourites.append(favourite)
                continue
            available_plans = json.loads(cache.get_meta_file(f"{favourite['plan_type']}.json"))
            if favourite["plan_type"] in available_plans:
                available_plans = available_plans[favourite["plan_type"]].keys()
            else:
                available_plans = available_plans.keys()
            if favourite["plan_value"] not in available_plans:
                return send_error("invalider Plan (Klasse/Lehrer/Raum) existiert nicht")
            if favourite["plan_type"] != "forms":
                favourite["preferences"] = None
                new_favourites.append(favourite)
                continue
            available_preferences = json.loads(cache.get_meta_file("forms.json"))["forms"][favourite["plan_value"]]["class_groups"].keys()
            favourite["preferences"] = [elem for elem in favourite["preferences"] if elem in available_preferences]
            new_favourites.append(favourite)
        users.update_one({'_id': ObjectId(self.mongo_id)}, {"$set": {'favourites': new_favourites}})
        return send_success(new_favourites)


def get_user(user_id):
    try:
        return users.find_one({'_id': ObjectId(user_id)})
    except Exception:
        return


class AddStaticFileHashFlask(Flask):
    def __init__(self, *args, **kwargs):
        super(AddStaticFileHashFlask, self).__init__(*args, **kwargs)
        self._file_hash_cache = {}

    def inject_url_defaults(self, endpoint, values):
        super(AddStaticFileHashFlask, self).inject_url_defaults(endpoint, values)
        if endpoint == "static" and "filename" in values and "site.webmanifest" not in values["filename"]:
            filepath = safe_join(self.static_folder, values["filename"])
            if os.path.isfile(filepath):
                cache = self._file_hash_cache.get(filepath)
                mtime = os.path.getmtime(filepath)
                if cache is not None:
                    cached_mtime, cached_hash = cache
                    if cached_mtime == mtime:
                        values["h"] = cached_hash
                        return
                h = hashlib.md5()
                with contextlib.closing(open(filepath, "rb")) as f:
                    h.update(f.read())
                h = h.hexdigest()
                self._file_hash_cache[filepath] = (mtime, h)
                values["h"] = h


# CREDS MANAGEMENT
def json_to_mongo():
    with open("creds.json", "r", encoding="utf-8") as f:
        json_creds = json.load(f)
    for very_short_name, elem in list(json_creds.items()):
        elem["_id"] = elem["school_number"]
        elem["short_name"] = very_short_name
        creds.update_one(
            {"_id": elem["_id"]},
            {"$set": elem},
            upsert=True
        )


# gets the passwords as well
def get_school_by_id(school_num: str):
    result = creds.find_one(
        {"_id": school_num},
    )
    return result


# doesn't get the passwords
def get_all_schools():
    pipeline = [
        {
            "$sort": {"count": pymongo.DESCENDING}
        },
        {
            "$project": {
                #"_id": False,
                "count": False,
                #"hosting": False,
                "comment": False,
            }
        }
    ]
    schools_list = list(creds.aggregate(pipeline))
    for ind, elem in enumerate(schools_list):
        schools_list[ind]["creds_needed"] = True if elem["hosting"]["creds"] else False
        del schools_list[ind]["hosting"]
    return schools_list


def get_all_schools_by_number():
    return {elem["_id"]: elem for elem in get_all_schools()}


def add_database_icons():
    school_icons = os.listdir("client/public/base_static/images/school_icons")
    for school_icon in school_icons:
        cur_num = school_icon.split(".")[0]
        creds.update_one(
            {"_id": cur_num},
            {"$set": {"icon": school_icon}}
        )


def update_school_authorization_count():
    pipeline = [
        {
            "$unwind": "$authorized_schools"
        },
        {
            "$group": {
                "_id": "$authorized_schools",
                "count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "creds",
                "localField": "_id",
                "foreignField": "_id",
                "as": "school_info"
            }
        },
        {
            "$unwind": "$school_info"
        }
    ]

    results = list(users.aggregate(pipeline))
    results = [
        {k: v for k, v in elem.items() if k not in ["school_info"]} for elem in results
    ]
    for result in results:
        creds.update_one(
            {"_id": result["_id"]},
            {"$set": {"count": result["count"]}}
        )


def run_in_background(func):
    def wrapper(*args, **kwargs):
        thread = threading.Thread(target=func, args=args, kwargs=kwargs)
        thread.start()
        return thread
    return wrapper


# WEBHOOKS
@run_in_background
def webhook_send(key: str, message: str = "", embeds: List[DiscordEmbed] = None):
    meta_env = "WEBHOOK_META"
    if os.getenv("PRODUCTION") is None or os.getenv("PRODUCTION") == "False":
        meta_env = "WEBHOOK_TEST"
        key = "WEBHOOK_TEST"
    embeds = [] if not embeds else embeds
    key = key.upper()
    if not os.getenv(key):
        return
    url = os.getenv(key)
    webhook = DiscordWebhook(url=url, content=message, username="VPlan-Bot", avatar_url="https://vplan.fr/static/images/icons/android-chrome-192x192.png")

    for embed in embeds:
        webhook.add_embed(embed)
    """if request:
        if os.getenv("WEBHOOK_META"):
            meta_webhook = DiscordWebhook(url=os.getenv(meta_env), content=message, username="VPlan-Bot", avatar_url="https://vplan.fr/static/images/icons/android-chrome-192x192.png")
            meta_embed = BetterEmbed(title="Metadaten", color="808080")
            if current_user:
                meta_embed.add_embed_field("Username:", f"{current_user.get_field('nickname')}", inline=False)
            meta_embed.add_embed_field("IP-Adresse:", f"`{request.remote_addr}`\n more info at https://whatismyipaddress.com/ip/{request.remote_addr}", inline=False)
            meta_embed.add_embed_field("User-Agent:", f"`{request.headers.get('user-agent')}`")
            meta_webhook.add_embed(meta_embed)
            meta_webhook.execute()"""

    webhook.execute()


class BetterEmbed(DiscordEmbed):

    def add_cleaned_field(self, name: str, value: str, inline: bool = True) -> None:
        while "`" in name:
            name = name.replace("`", "")
        while "`" in value:
            value = value.replace("`", "")
        self.add_embed_field(name, value, inline)


def update_database():
    add_database_icons()
    update_school_authorization_count()
    VALID_SCHOOLS = [elem["_id"] for elem in list(creds.find({}))]


@run_in_background
def meta_to_database(request_data):
    meta.insert_one(request_data)


if __name__ == "__main__":
    # json_to_mongo()
    ...



