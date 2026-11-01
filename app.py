from flask import Flask, request, jsonify
import asyncio
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import requests
import json
import like_pb2
import uid_generator_pb2
import visit_count_pb2
from google.protobuf.message import DecodeError
from collections import OrderedDict
from datetime import date
import threading

app = Flask(__name__)

# ✅ Valid API keys
VALID_API_KEYS = {
    "hkysxbhyws"
}

# 🇧🇩 Only Bangladesh region allowed
ALLOWED_REGION = "BD"
BD_LIKE_URL = "https://clientbp.ggpolarbear.com/LikeProfile"
BD_INFO_URL = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"

# ⚙️ Config
# প্রতি ইউজার 220 লাইক
# প্রতি গেস্ট আইডি দিনে 50 লাইক
# token_bd.json এ 230 টা গেস্ট আইডি থাকবে
TOKEN_DAILY_LIMIT = 50        # প্রতিটা token দিনে 50 বার like
TOTAL_TOKENS = 230            # মোট গেস্ট আইডি সংখ্যা
TARGET_LIKES_PER_USER = 220   # প্রতি ইউজার 220 লাইক
# মোট দৈনিক লাইক ক্যাপাসিটি = 230 × 50 = 11500
# প্রতি ইউজার 220 লাইক => সর্বোচ্চ 11500 // 220 = 52 ইউজার
DAILY_LIMIT = 52              # দিনে সর্বোচ্চ 52 user (52 × 220 = 11440 ≤ 11500)
TOKEN_FAIL_THRESHOLD = 5      # 5 বার fail হলে token skip

# 📊 State management
used_count = 0
token_usage = {}
token_failures = {}
last_reset_date = date.today()
lock = threading.Lock()


def reset_if_new_day():
    """নতুন দিনে সব কাউন্টার রিসেট হয় — সব টোকেন আবার 50 লাইক পায়"""
    global used_count, token_usage, token_failures, last_reset_date
    today = date.today()
    if today != last_reset_date:
        used_count = 0
        token_usage = {}
        token_failures = {}
        last_reset_date = today
        app.logger.info("🔄 New day — counters reset. সব টোকেন আবার 50 লাইক পাবে.")


def load_tokens():
    try:
        with open("token_bd.json", "r") as f:
            return json.load(f)
    except Exception as e:
        app.logger.error(f"Error loading BD tokens: {e}")
        return None


def encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode('utf-8')
    except Exception as e:
        app.logger.error(f"Error encrypting message: {e}")
        return None


def create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating protobuf message: {e}")
        return None


async def send_request(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers) as response:
                text = await response.text()
                status_code = response.status
                return {
                    "token": token,
                    "response": text,
                    "status_code": status_code,
                    "success": status_code == 200
                }
    except Exception as e:
        app.logger.error(f"Exception in send_request: {e}")
        return {"token": token, "response": None, "status_code": 0, "success": False}


def get_all_available_tokens(tokens):
    """
    সব টোকেন থেকে যেসব এখনো 50 লাইক হয়নি এবং fail হয়নি সেগুলো রিটার্ন করে।
    কোনো ব্যাচ কনসেপ্ট নেই — সব 230 টোকেন একসাথে ব্যবহার হয়।
    """
    available = []
    for idx, entry in enumerate(tokens):
        used = token_usage.get(idx, 0)
        fails = token_failures.get(idx, 0)
        if used < TOKEN_DAILY_LIMIT and fails < TOKEN_FAIL_THRESHOLD:
            available.append((idx, entry["token"]))
    return available


def total_remaining_likes():
    """সব টোকেনে এখনো কত লাইক বাকি আছে তা হিসাব করে"""
    tokens = load_tokens() or []
    remaining = 0
    for idx in range(len(tokens)):
        used = token_usage.get(idx, 0)
        fails = token_failures.get(idx, 0)
        if fails < TOKEN_FAIL_THRESHOLD:
            remaining += max(TOKEN_DAILY_LIMIT - used, 0)
    return remaining


async def send_multiple_requests(uid, region, url):
    """
    একটি ইউজারকে 220 লাইক দেওয়ার চেষ্টা করে।
    প্রতিটি টোকেন সর্বোচ্চ 50 লাইক দিতে পারে।
    220 লাইকের জন্য কমপক্ষে ceil(220/50)=5 টোকেন লাগবে (সাধারণত 220 টোকেন পর্যন্ত)।
    যেহেতু প্রতিটি টোকেন একবারে একটি লাইক দেয়, তাই 220 লাইক পেতে
    আমাদের 220 টি আলাদা রিকোয়েস্ট পাঠাতে হবে — প্রতিটি ভিন্ন টোকেন থেকে।
    """
    global token_usage, token_failures

    protobuf_message = create_protobuf_message(uid, region)
    if protobuf_message is None:
        return None
    encrypted_uid = encrypt_message(protobuf_message)
    if encrypted_uid is None:
        return None

    tokens = load_tokens()
    if not tokens:
        return None

    likes_achieved = 0
    target_likes = TARGET_LIKES_PER_USER
    all_results = []

    # যতক্ষণ 220 লাইক না হয় বা available টোকেন না শেষ হয়
    while likes_achieved < target_likes:
        available = get_all_available_tokens(tokens)
        if not available:
            app.logger.warning("⚠️ সব টোকেন exhausted — আজ আর লাইক দেওয়া সম্ভব না")
            break

        # এই রাউন্ডে যত টোকেন দরকার (বাকি লাইকের সমান, তবে available এর বেশি নয়)
        needed = target_likes - likes_achieved
        batch = available[:needed]

        tasks = [send_request(encrypted_uid, token, url) for idx, token in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            idx = batch[i][0]
            if isinstance(result, dict) and result.get("success"):
                token_usage[idx] = token_usage.get(idx, 0) + 1
                likes_achieved += 1
            else:
                token_failures[idx] = token_failures.get(idx, 0) + 1
            all_results.append(result)

        # যদি কোনো অগ্রগতি না হয় (সব fail), লুপ ভাঙি
        if not any(isinstance(r, dict) and r.get("success") for r in results):
            app.logger.warning("⚠️ এই রাউন্ডে কোনো লাইক সফল হয়নি — লুপ থামানো হলো")
            break

    app.logger.info(f"UID {uid}: মোট {likes_achieved}/{target_likes} লাইক দেওয়া হয়েছে")
    return all_results


def create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception as e:
        app.logger.error(f"Error creating uid protobuf: {e}")
        return None


def enc(uid):
    protobuf_data = create_protobuf(uid)
    if protobuf_data is None:
        return None
    return encrypt_message(protobuf_data)


def make_request(encrypt, region, token):
    try:
        edata = bytes.fromhex(encrypt)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB55"
        }
        response = requests.post(BD_INFO_URL, data=edata, headers=headers, verify=False)
        decoded = visit_count_pb2.Info()
        decoded.ParseFromString(response.content)
        return decoded
    except DecodeError as e:
        app.logger.error(f"DecodeError: {e}")
        return None
    except Exception as e:
        app.logger.error(f"Error in make_request: {e}")
        return None


@app.route('/like', methods=['GET'])
def handle_requests():
    global used_count

    with lock:
        reset_if_new_day()

        api_key = request.args.get("key")
        if api_key not in VALID_API_KEYS:
            result = OrderedDict([("error", "Invalid or missing API key"), ("status", 3)])
            return app.response_class(
                response=json.dumps(result, separators=(',', ':')),
                status=401, mimetype='application/json'
            )

        uid = request.args.get("uid")
        region = ALLOWED_REGION
        if not uid:
            return {"error": "UID is required"}, 400

        try:
            # ✅ চেক: সব টোকেনের 50 লাইক লিমিট শেষ কিনা
            remaining_likes = total_remaining_likes()
            if remaining_likes < TARGET_LIKES_PER_USER:
                result = OrderedDict([
                    ("error", "সব গেস্ট আইডির দৈনিক 50 লাইক লিমিট শেষ। আগামীকাল আবার চেষ্টা করুন (4:00 AM IST এর পরে)।"),
                    ("status", 4),
                    ("total_remaining_likes", remaining_likes),
                    ("required_per_user", TARGET_LIKES_PER_USER)
                ])
                return app.response_class(
                    response=json.dumps(result, separators=(',', ':')),
                    status=429, mimetype='application/json'
                )

            if used_count >= DAILY_LIMIT:
                result = OrderedDict([
                    ("error", "Daily user limit reached. Try again after 4:00 AM IST."),
                    ("status", 4), ("daily_limit", DAILY_LIMIT),
                    ("used", used_count), ("remaining", 0)
                ])
                return app.response_class(
                    response=json.dumps(result, separators=(',', ':')),
                    status=429, mimetype='application/json'
                )

            tokens = load_tokens()
            if not tokens:
                raise Exception("Failed to load BD tokens.")
            token = tokens[0]['token']
            encrypted_uid = enc(uid)
            if encrypted_uid is None:
                raise Exception("Encryption of UID failed.")

            before = make_request(encrypted_uid, region, token)
            if before is None:
                raise Exception("Failed to get initial info.")
            before_like = before.AccountInfo.Likes

            asyncio.run(send_multiple_requests(uid, region, BD_LIKE_URL))

            after = make_request(encrypted_uid, region, token)
            if after is None:
                raise Exception("Failed to get final info.")
            after_like = after.AccountInfo.Likes
            like_given = after_like - before_like
            status = 1 if like_given > 0 else 2

            if status == 1:
                used_count += 1

            remaining_users = max(DAILY_LIMIT - used_count, 0)
            remaining_likes_now = total_remaining_likes()

            result = OrderedDict([
                ("LikesGivenByAPI", like_given),
                ("LikesafterCommand", after_like),
                ("LikesbeforeCommand", before_like),
                ("PlayerNickname", after.AccountInfo.PlayerNickname),
                ("Level", after.AccountInfo.Levels),
                ("Region", "BD"),
                ("UID", after.AccountInfo.UID),
                ("status", status),
                ("daily_limit", DAILY_LIMIT),
                ("used", used_count),
                ("remaining_users", remaining_users),
                ("total_remaining_likes", remaining_likes_now)
            ])

            return app.response_class(
                response=json.dumps(result, separators=(',', ':')),
                status=200, mimetype='application/json'
            )

        except Exception as e:
            app.logger.error(f"Error: {e}")
            return {"error": str(e)}, 500


@app.route('/remain', methods=['GET'])
def remain_info():
    global used_count, token_usage, token_failures
    reset_if_new_day()
    tokens = load_tokens() or []
    active_tokens = sum(
        1 for i in range(len(tokens))
        if token_usage.get(i, 0) < TOKEN_DAILY_LIMIT
        and token_failures.get(i, 0) < TOKEN_FAIL_THRESHOLD
    )
    remaining_likes = total_remaining_likes()

    data = {
        "server": "BD",
        "daily_limit_users": DAILY_LIMIT,
        "remaining_users": max(DAILY_LIMIT - used_count, 0),
        "used_users": used_count,
        "target_likes_per_user": TARGET_LIKES_PER_USER,
        "token_daily_limit": TOKEN_DAILY_LIMIT,
        "total_tokens": len(tokens),
        "active_tokens_today": active_tokens,
        "total_remaining_likes_today": remaining_likes,
        "max_users_possible_today": remaining_likes // TARGET_LIKES_PER_USER,
        "token_fail_threshold": TOKEN_FAIL_THRESHOLD,
        "reset_info": "4:00 AM IST"
    }
    return jsonify(data)


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)