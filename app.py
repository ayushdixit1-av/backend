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
from urllib.parse import urlparse

# Optional Redis support
try:
    import redis
except Exception:
    redis = None

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# -------------------------
# CONFIG (HARDCODED AS REQUESTED)
# -------------------------
TW_SID = 'AC07e7e3c3277c49afc1f06feec329afaa'
TW_TOKEN = '7042acd3973a3ca33a1c38a4bd99d1a2'
TW_NUMBER = '+15074835441'
OWM_API_KEY = 'b658cc9374404245188f1eb618a46830'

# Allow override by env
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
# Session store (Redis if available, else memory)
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
        app.logger.warning("Redis connect failed: %s — using memory", e)

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
        app.logger.warning("Twilio client not configured")
        return False
    try:
        tw_client.messages.create(body=body, from_=TW_NUMBER, to=to_number)
        app.logger.info("SMS sent to %s", to_number)
        return True
    except Exception as e:
        app.logger.error("Error sending SMS: %s", e)
        return False

# -------------------------
# Twilio request validation
# -------------------------
def _build_public_url_candidates(req):
    candidates = []
    proto = req.headers.get("X-Forwarded-Proto") or req.headers.get("X-Forwarded-Scheme")
    host = req.headers.get("X-Forwarded-Host") or req.headers.get("Host")
    path = req.full_path if req.full_path else req.path
    if path.endswith("?"):
        path = path[:-1]

    if proto and host:
        candidates.append(f"{proto}://{host}{path}")

    if host:
        candidates.append(f"https://{host}{path}")
        candidates.append(f"http://{host}{path}")

    candidates.append(req.url)
    try:
        parsed = urlparse(req.url)
        if parsed.scheme == "http":
            candidates.append(req.url.replace("http://", "https://", 1))
        elif parsed.scheme == "https":
            candidates.append(req.url.replace("https://", "http://", 1))
    except Exception:
        pass

    for c in list(candidates):
        if c.endswith("/") and not c.endswith("/?"):
            candidates.append(c.rstrip("/"))
        elif not c.endswith("/"):
            candidates.append(c + "/")

    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out

def require_twilio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if os.environ.get("TW_SKIP_VALIDATION") == "1":
            return f(*args, **kwargs)
        if validator:
            sig = request.headers.get('X-Twilio-Signature', '')
            valid = False
            for candidate in _build_public_url_candidates(request):
                if validator.validate(candidate, request.form.to_dict(), sig):
                    valid = True
                    break
            if not valid:
                app.logger.warning(f"Invalid Twilio signature. URL used={request.url}")
                return Response("Invalid signature", status=403)
        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Weather helpers
# -------------------------
def geocode_pin(pin):
    if not OWM_API_KEY:
        return None, None, None
    try:
        r = session.get("http://api.openweathermap.org/geo/1.0/zip",
                        params={"zip": f"{pin},IN", "appid": OWM_API_KEY}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            return j.get("lat"), j.get("lon"), j.get("name") or pin
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
    rain = w.get("rain", {}).get("1h", 0) if "rain" in w else 0
    temp = main.get("temp")
    desc = weather.get("description", "").capitalize()
    wind_speed = wind.get("speed", 0)
    adv = [f"वर्तमान: {desc}. तापमान ~{temp:.0f}°C." if temp else f"वर्तमान: {desc}."]
    if rain >= 10:
        adv.append("तेज़ बारिश संभव — फसल सुरक्षित करें।")
    elif rain >= 2:
        adv.append("बारिश हो सकती है — छिड़काव से पहले ध्यान दें।")
    if temp and temp >= 40:
        adv.append("अत्यधिक गर्मी — दोपहर में काम कम करें।")
    if temp and temp <= 5:
        adv.append("बहुत ठंड — फसल/पशु सुरक्षा करें।")
    if wind_speed >= 10:
        adv.append("हवा तेज़ है — उपकरण सुरक्षित रखें।")
    return " ".join(adv)

# -------------------------
# PIN extraction
# -------------------------
NUM_WORDS = {"zero":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9",
             "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
             "एक":"1","दो":"2","तीन":"3","चार":"4","पाँच":"5","पांच":"5","छह":"6","सात":"7","आठ":"8","नौ":"9","शून्य":"0"}
def extract_pin(digits, speech):
    if digits and re.fullmatch(r'\d{6}', digits.strip()):
        return digits.strip()
    s = (speech or "").lower()
    m = re.search(r'(\d{6})', s)
    if m:
        return m.group(1)
    tokens = re.findall(r'\d+|[०१२३४५६७८९]+|[a-zA-Z]+|[\u0900-\u097F]+', s)
    pin_digits = []
    for t in tokens:
        if t in NUM_WORDS:
            pin_digits.append(NUM_WORDS[t])
        elif t.isdigit():
            pin_digits.extend(list(t))
        for ch in t:
            if ch in NUM_WORDS:
                pin_digits.append(NUM_WORDS[ch])
        if len(pin_digits) >= 6:
            return "".join(pin_digits[:6])
    return None

# -------------------------
# Flask endpoints
# -------------------------
@app.route('/voice', methods=['POST'])
@require_twilio
def voice():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    session_set(call_sid, {'from': from_number})
    resp = VoiceResponse()
    g = Gather(input='speech dtmf', num_digits=1, action='/handle-main', method='POST', language='hi-IN')
    g.say('नमस्ते। मंडी भाव के लिए 1, फसल सलाह के लिए 2, मौसम के लिए 3, विशेषज्ञ के लिए 4।', language='hi-IN')
    resp.append(g)
    resp.say('हम आपकी बात नहीं समझ पाए।', language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-main', methods=['POST'])
@require_twilio
def handle_main():
    choice = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    if not choice:
        if 'भाव' in speech: choice = '1'
        elif 'सलाह' in speech: choice = '2'
        elif 'मौसम' in speech: choice = '3'
        elif 'विशेषज्ञ' in speech: choice = '4'
    resp = VoiceResponse()
    if choice == '3':
        g = Gather(input='speech dtmf', num_digits=6, action='/handle-weather', method='POST', language='hi-IN')
        g.say('कृपया अपना पिनकोड बोलें या दबाएँ।', language='hi-IN')
        resp.append(g)
    elif choice == '1':
        resp.say('कानपुर मंडी में गेहूँ का भाव ₹2100 प्रति क्विंटल है।', language='hi-IN')
    elif choice == '2':
        resp.say('फसल सलाह जल्द उपलब्ध होगी।', language='hi-IN')
    elif choice == '4':
        resp.say('विशेषज्ञ से जोड़ रहे हैं।', language='hi-IN')
    else:
        resp.say('माफ़ कीजिये, समझ नहीं पाया।', language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-weather', methods=['POST'])
@require_twilio
def handle_weather():
    digits = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    pin = extract_pin(digits, speech)
    resp = VoiceResponse()
    if not pin:
        resp.say('पिनकोड नहीं मिला।', language='hi-IN')
        return Response(str(resp), mimetype='text/xml')
    lat, lon, place = geocode_pin(pin)
    if not lat or not lon:
        resp.say('स्थान नहीं मिला।', language='hi-IN')
        return Response(str(resp), mimetype='text/xml')
    weather = fetch_weather(lat, lon)
    advice = make_weather_advice(weather)
    resp.say(f"{place} के लिए मौसम: {advice}", language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

# -------------------------
# Run
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False)
