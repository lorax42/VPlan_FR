from bson import ObjectId
from flask import Blueprint, request, session, Response, jsonify
from flask_login import login_required, current_user, login_user, logout_user

import time
from random import choice

from werkzeug.security import generate_password_hash, check_password_hash
from utils import User, users
from var import *

authorization = Blueprint('authorization', __name__)


@authorization.route('/login', methods=['POST'])
def login() -> Response:
    nickname = request.form.get('nickname')
    password = request.form.get('pw')
    
    user = users.find_one({'nickname': nickname})

    if user is not None and check_password_hash(user["password_hash"], password):
        tmp_user = User(str(user["_id"]))
        login_user(tmp_user)
        session.permanent = True
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Benutzername oder Passwort waren falsch! Bitte versuch es erneut."})


@authorization.route('/signup', methods=['POST'])
def signup() -> Response:
    nickname = request.form.get("nickname")
    password = request.form.get("pw")

    tmp_user = users.find_one({'nickname': nickname})
    if tmp_user is not None:
        return jsonify({"success": False, "error": "Nutzername ist schon vergeben!"})

    if (len(nickname) < 3) or (len(nickname) > 15):
        return jsonify({"success": False, "error": "Nutzername muss zwischen 3 und 15 Zeichen lang sein!"})

    if len(password) < 10:
        return jsonify({"success": False, "error": "Passwort muss mindestens 10 Zeichen lang sein!"})

    tmp_id = users.insert_one({
        'nickname': nickname,
        'admin': False,
        'authorized_schools': [],
        'password_hash': generate_password_hash(password, method='sha256'),
        'time_joined': time.time(),
        'settings': DEFAULT_SETTINGS
    })
    tmp_user = User(str(tmp_id.inserted_id))
    login_user(tmp_user)
    session.permanent = True
    current_user.update_settings({})
    return jsonify({"success": True})


@authorization.route('/logout')
@login_required
def logout() -> Response:
    logout_user()
    return jsonify({"success": True})


@authorization.route('/account', methods=['GET', 'DELETE'])
@login_required
def account() -> Response:
    if request.method == "GET":
        tmp_user = current_user.get_user()
        return jsonify({
                'nickname': tmp_user['nickname'], 
                'authorized_schools': tmp_user['authorized_schools'], 
                'preferences': tmp_user['preferences'], 
                'settings': tmp_user['settings'], 
                'time_joined': tmp_user['time_joined']
            })
    # method must be 'DELETE'
    x = users.delete_one({'_id': ObjectId(current_user.mongo_id)})
    return jsonify({"success": True}) if x.deleted_count == 1 else jsonify({"success": False})


@authorization.route("/settings", methods=['GET', 'DELETE', 'POST'])
@login_required
def settings() -> Response:
    if request.method == "GET":
        return jsonify(current_user.get_user()["settings"])
    if request.method == "DELETE":
        current_user.update_settings()
        return jsonify({"success": True})
    # method must be 'POST'
    return Response("Dafuq, you thought we implemented this lol")


@authorization.route('/authorized_schools', methods=['GET'])
@login_required
def authorized_schools() -> Response:
    return jsonify(
        current_user.get_authorized_schools()
    )


def school_authorized(func):
    def wrapper_thing(*args, **kwargs):
        if not current_user.user:
            current_user.get_user()
        if kwargs.get("school_num") is None:
            return {"error": "no school number provided"}
        if not current_user.user.get("admin"):
            if kwargs.get("school_num") not in current_user.user.get("authorized_schools"):
                return {"error": "user not authorized for specified school"}
        return func(*args, **kwargs)
    return wrapper_thing


@authorization.route("/check_login", methods=["GET"])
def check_login():
    if current_user.is_authenticated:
        response_data = {'logged_in': True}
    else:
        response_data = {'logged_in': False}
    return jsonify(response_data)


@authorization.route("/greeting", methods=["GET"])
@login_required
def greeting():
    if not current_user.user:
        current_user.get_user()
    greetings = [
        "Grüß Gott {name}!",
        "Moin {name}!",
        "Moinsen {name}!",
        "Yo Moinsen {name}!",
        "Servus {name}!",
        "Hi {name}!",
        "Hey {name}!",
        "Hallo {name}!",
        "Hallöchen {name}!",
        "Halli-Hallo {name}!",
        "Hey, was geht ab {name}?",
        "Tachchen {name}!",
        "Na, alles fit, {name}?",
        "Alles Klärchen, {name}?",
        "Jo Digga {name}!",
        "Heyho {name}!",
        "Ahoihoi {name}!",
        "Aloha {name}!",
        "Alles cool im Pool, {name}?",
        "Alles klar in Kanada, {name}?",
        "Alles Roger in Kambodscha, {name}?",
        "Hallöchen mit Öchen {name}!",
        "{name} joined the game",
        "Alles nice im Reis?",
        "Alles cool in Suhl?",
        "Howdy {name}!",
    ]
    random_greeting = choice(greetings).format(name=current_user["nickname"])
    return Response(random_greeting)

