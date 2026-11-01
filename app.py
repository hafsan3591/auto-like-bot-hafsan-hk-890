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

# ⚙️ Batch rotation config
# token_bd.json এ মোট 660 guest id থাকবে → 3 batch × 220
BATCH_SIZE = 220              # প্রতি batch এ 220 token (guest id)
TOKEN_DAILY_LIMIT = 20        # প্রতিটা token দিনে 20 বার like
DAILY_LIMIT = 220             # ✅ পাবলিক দিনে সর্বোচ্চ 220 user target
TOKEN_FAIL_THRESHOLD = 5      # 5 বার fail হলে token skip

# 📊 State management
used_count = 0
token_usage = {}
token_failures = {}
current_batch = 0
last_reset_date = date.today()
lock = threading.Lock()


def reset_if_new_day():
    global used_count, token_usage, token_failures, current_batch, last_reset_date
    today = date.today()
    if today != last_reset_date:
        used_count = 0
        token_usage = {}
        token_failures = {}
        current_batch = 0
        last_reset_date = today
        app.logger.info("🔄 New day — counters reset")


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


def get_available_tokens(tokens):
    """
    ✅ Batch rotation logic:
    - প্রথমে batch 0 (0-219 token) দিয়ে like হবে, প্রতিটা 20 বার
    - batch 0 এর সব token exhausted হলে current_batch = 1 → (220-439)
    - batch 1 exhausted হলে current_batch = 2 → (440-659)
    - সব batch exhausted হলে None return
    """
    global current_batch, token_usage, token_failures

    total_batches = (len(tokens) + BATCH_SIZE - 1) // BATCH_SIZE

    # বর্তমান batch থেকে শুরু করে পরের batch গুলো চেক করো
    while current_batch < total_batches:
        start = current_batch * BATCH_SIZE
        end = min(start + BATCH_SIZE, len(tokens))
        batch_tokens = []

        for idx in range(start, end):
            used = token_usage.get(idx, 0)
            fails = token_failures.get(idx, 0)
            if used < TOKEN_DAILY_LIMIT and fails < TOKEN_FAIL_THRESHOLD:
                batch_tokens.append((idx, tokens[idx]["token"]))

        if batch_tokens:
            # এই batch এ এখনো available token আছে
            return batch_tokens

        # এই batch পুরো exhausted → পরের batch এ যাও
        app.logger.info(f"➡️ Batch {current_batch} exhausted — switching to batch {current_batch + 1}")
        current_batch += 1

    return None


async def send_multiple_requests(uid, region, url):
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

    available = get_available_tokens(tokens)
    if not available:
        return {"error": "All token batches exhausted for today"}

    tasks = []
    for idx, token in available:
        tasks.append(send_request(encrypted_uid, token, url))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, result in enumerate(results):
        idx = available[i][0]
        if isinstance(result, dict) and result.get("success"):
            token_usage[idx] = token_usage.get(idx, 0) + 1
        else:
            token_failures[idx] = token_failures.get(idx, 0) + 1

    return results


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
            if used_count >= DAILY_LIMIT:
                result = OrderedDict([
                    ("error", "Daily limit reached. Try again after 4:00 AM IST."),
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

            remaining = max(DAILY_LIMIT - used_count, 0)

            result = OrderedDict([
                ("LikesGivenByAPI", like_given),
                ("LikesafterCommand", after_like),
                ("LikesbeforeCommand", before_like),
                ("PlayerNickname", after.AccountInfo.PlayerNickname),
                ("Level", after.AccountInfo.Levels),
                ("Region", "BD"),
                ("UID", after.AccountInfo.UID),
                ("status", status),
                ("current_batch", current_batch),
                ("daily_limit", DAILY_LIMIT),
                ("used", used_count),
                ("remaining", remaining)
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
    global used_count, current_batch, token_usage, token_failures
    reset_if_new_day()
    tokens = load_tokens() or []
    total_batches = (len(tokens) + BATCH_SIZE - 1) // BATCH_SIZE
    active_tokens = sum(
        1 for i in range(len(tokens))
        if token_usage.get(i, 0) < TOKEN_DAILY_LIMIT
        and token_failures.get(i, 0) < TOKEN_FAIL_THRESHOLD
    )

    # প্রতি batch এর status
    batch_status = []
    for b in range(total_batches):
        s = b * BATCH_SIZE
        e = min(s + BATCH_SIZE, len(tokens))
        active = sum(
            1 for i in range(s, e)
            if token_usage.get(i, 0) < TOKEN_DAILY_LIMIT
            and token_failures.get(i, 0) < TOKEN_FAIL_THRESHOLD
        )
        batch_status.append({
            "batch": b,
            "tokens": e - s,
            "active_tokens": active,
            "exhausted": active == 0
        })

    data = {
        "server": "BD",
        "daily_limit": DAILY_LIMIT,
        "remaining_users": max(DAILY_LIMIT - used_count, 0),
        "used": used_count,
        "total_tokens": len(tokens),
        "batch_size": BATCH_SIZE,
        "total_batches": total_batches,
        "current_batch": current_batch,
        "active_tokens_today": active_tokens,
        "token_daily_limit": TOKEN_DAILY_LIMIT,
        "token_fail_threshold": TOKEN_FAIL_THRESHOLD,
        "batch_status": batch_status,
        "reset_info": "4:00 AM IST"
    }
    return jsonify(data)


if __name__ == '__main__':
    app.run(debug=True, use_reloader=False)