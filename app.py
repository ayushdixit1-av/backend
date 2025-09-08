import os
import logging
import re
import json
import requests
from functools import wraps
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Optional Redis support
try:
    import redis
except Exception:
    redis = None

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# -------------------------
# CONFIG (hardcoded as requested)
# -------------------------
TW_SID = 'AC07e7e3c3277c49afc1f06feec329afaa'
TW_TOKEN = '7042acd3973a3ca33a1c38a4bd99d1a2'
TW_NUMBER = '+15074835441'
OWM_API_KEY = 'b658cc9374404245188f1eb618a46830'

# Allow env override
TW_SID = os.environ.get("TW_SID", TW_SID)
TW_TOKEN = os.environ.get("TW_TOKEN", TW_TOKEN)
TW_NUMBER = os.environ.get("TW_NUMBER", TW_NUMBER)
OWM_API_KEY = os.environ.get("OWM_API_KEY", OWM_API_KEY)
REDIS_URL = os.environ.get("REDIS_URL", None)

# -------------------------
# Twilio client + validator
# -------------------------
tw_client = None
try:
    if TW_SID and TW_TOKEN:
        tw_client = Client(TW_SID, TW_TOKEN)
        app.logger.info("Twilio client initialized")
except Exception as e:
    app.logger.error("Twilio client init failed: %s", e)
    tw_client = None

validator = RequestValidator(TW_TOKEN) if TW_TOKEN else None

# -------------------------
# HTTP session with retries
# -------------------------
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries)
session.mount("http://", adapter)
session.mount("https://", adapter)

# -------------------------
# Session store
# -------------------------
use_redis = False
redis_client = None
if REDIS_URL and redis:
    try:
        redis_client = redis.from_url(REDIS_URL)
        redis_client.ping()
        use_redis = True
        app.logger.info("Using Redis session store")
    except Exception as e:
        app.logger.warning("Failed to connect to Redis: %s — using memory", e)

call_sessions = {}

def session_set(call_sid, data: dict):
    if use_redis and redis_client:
        try:
            redis_client.set(f"call:{call_sid}", json.dumps(data), ex=3600)
            return
        except Exception as e:
            app.logger.warning("Redis set failed: %s", e)
    call_sessions[call_sid] = data

def session_get(call_sid):
    if use_redis and redis_client:
        try:
            v = redis_client.get(f"call:{call_sid}")
            if v:
                return json.loads(v)
        except Exception as e:
            app.logger.warning("Redis get failed: %s", e)
    return call_sessions.get(call_sid, {})

def session_pop(call_sid):
    if use_redis and redis_client:
        try:
            redis_client.delete(f"call:{call_sid}")
            return
        except Exception as e:
            app.logger.warning("Redis delete failed: %s", e)
    call_sessions.pop(call_sid, None)

# -------------------------
# SMS helper
# -------------------------
def send_sms(to_number, body):
    if not tw_client:
        app.logger.warning("Twilio client not configured — SMS not sent")
        return False
    try:
        tw_client.messages.create(body=body, from_=TW_NUMBER, to=to_number)
        app.logger.info("SMS sent to %s", to_number)
        return True
    except Exception as e:
        app.logger.error("Error sending SMS to %s: %s", to_number, e)
        return False

# -------------------------
# Twilio request validation (fixed for Railway/Heroku proxies)
# -------------------------
def _public_url(req):
    proto = req.headers.get("X-Forwarded-Proto", None) or "http"
    host = req.headers.get("X-Forwarded-Host", req.host)
    path = req.full_path if req.full_path else req.path
    if path.endswith("?"):  # strip trailing ?
        path = path[:-1]
    return f"{proto}://{host}{path}"

def require_twilio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if validator:
            sig = request.headers.get("X-Twilio-Signature", "")
            url = _public_url(request)
            params = request.form.to_dict()
            try:
                valid = validator.validate(url, params, sig)
            except Exception as ex:
                app.logger.warning("Twilio validator error: %s", ex)
                valid = False
            if not valid:
                app.logger.warning("Invalid Twilio signature. URL used=%s", url)
                return Response("Invalid signature", status=403)
        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Weather helpers
# -------------------------
def geocode_pin(pin):
    if not OWM_API_KEY:
        return None, None, None
    pin = str(pin).strip()
    try:
        r = session.get("http://api.openweathermap.org/geo/1.0/zip",
                        params={"zip": f"{pin},IN", "appid": OWM_API_KEY}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            lat, lon, name = j.get("lat"), j.get("lon"), j.get("name") or pin
            if lat and lon:
                return lat, lon, name
    except Exception:
        pass
    try:
        r = session.get("http://api.openweathermap.org/geo/1.0/direct",
                        params={"q": f"{pin},IN", "limit": 1, "appid": OWM_API_KEY}, timeout=8)
        if r.status_code == 200 and r.json():
            item = r.json()[0]
            return item.get("lat"), item.get("lon"), item.get("name") or pin
    except Exception:
        pass
    return None, None, None

def fetch_weather(lat, lon):
    try:
        r = session.get("https://api.openweathermap.org/data/2.5/weather",
                        params={"lat": lat, "lon": lon, "appid": OWM_API_KEY, "units": "metric"}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return None

def make_weather_advice(w):
    if not w:
        return "मौसम जानकारी उपलब्ध नहीं है।"
    main = w.get("main", {})
    weather = w.get("weather", [{}])[0]
    wind = w.get("wind", {})
    rain = 0
    if "rain" in w:
        rain = w["rain"].get("1h", 0) or w["rain"].get("3h", 0) or 0
    temp = main.get("temp")
    desc = weather.get("description", "").capitalize()
    wind_speed = wind.get("speed", 0)
    adv = []
    if temp is not None:
        adv.append(f"वर्तमान: {desc}. तापमान ~{temp:.0f}°C.")
    if rain >= 10:
        adv.append("तेज़ बारिश संभव — फसल/आवरण सुरक्षित करें।")
    elif rain >= 2:
        adv.append("बारिश का चांस — छिड़काव से पहले मौसम देख लें।")
    if temp and temp >= 40:
        adv.append("अत्यधिक गर्मी — दोपहर में काम सीमित करें।")
    if temp and temp <= 5:
        adv.append("काफी ठंड — फसल/पशु सुरक्षा करें।")
    if wind_speed >= 10:
        adv.append("तेज़ हवा — हल्के उपकरण सुरक्षित करें।")
    return " ".join(adv) if adv else "कोई विशेष चेतावनी नहीं।"

# -------------------------
# PIN extraction
# -------------------------
NUM_WORDS = {"zero":"0","oh":"0","o":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9",
             "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
             "एक":"1","दो":"2","तीन":"3","चार":"4","पाँच":"5","पांच":"5","छह":"6","सात":"7","आठ":"8","नौ":"9","शून्य":"0"}

def extract_pin(digits, speech):
    if digits and re.fullmatch(r"\d{6}", digits.strip()):
        return digits.strip()
    s = (speech or "").lower()
    m = re.search(r"(\d{6})", s)
    if m:
        return m.group(1)
    tokens = re.findall(r"\d+|[०१२३४५६७८९]+|[a-zA-Z]+|[\u0900-\u097F]+", s)
    pin_digits = []
    for t in tokens:
        t = t.strip().lower()
        if t.isdigit():
            for ch in t:
                pin_digits.append(ch)
                if len(pin_digits) == 6:
                    return "".join(pin_digits)
        elif t in NUM_WORDS:
            pin_digits.append(NUM_WORDS[t])
            if len(pin_digits) == 6:
                return "".join(pin_digits)
        else:
            for ch in t:
                if ch in NUM_WORDS:
                    pin_digits.append(NUM_WORDS[ch])
                    if len(pin_digits) == 6:
                        return "".join(pin_digits)
    return None

# -------------------------
# Flask endpoints (shortened for brevity)
# -------------------------
@app.route("/voice", methods=["POST"])
@require_twilio
def voice():
    call_sid = request.values.get("CallSid")
    from_number = request.values.get("From")
    app.logger.info("Incoming call from %s (CallSid=%s)", from_number, call_sid)
    session_set(call_sid, {"from": from_number})
    resp = VoiceResponse()
    g = Gather(input="speech dtmf", num_digits=1, timeout=5, action="/handle-main", method="POST", language="hi-IN")
    g.say("नमस्ते। मंडी भाव के लिए 1, फसल सलाह के लिए 2, मौसम के लिए 3, विशेषज्ञ के लिए 4।", language="hi-IN")
    resp.append(g)
    return Response(str(resp), mimetype="text/xml")

# ... (keep your handle-main, handle-advice, handle-weather, etc. as before, they don’t need changes)

# -------------------------
# Run server
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    debug_mode = os.environ.get("FLASK_ENV", "") == "development"
    app.run(host="0.0.0.0", port=port, debug=debug_mode)
