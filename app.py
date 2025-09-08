# app.py (full file) - improved Twilio signature validator (aggressive candidates + raw body fallback)
import os
import logging
import re
import json
import requests
from functools import wraps
from urllib.parse import urlparse
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client
from twilio.request_validator import RequestValidator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# -------------------------
# CONFIG (hardcoded as requested)
# -------------------------
TW_SID = 'AC07e7e3c3277c49afc1f06feec329afaa'
TW_TOKEN = '7042acd3973a3ca33a1c38a4bd99d1a2'
TW_NUMBER = '+15074835441'
OWM_API_KEY = 'b658cc9374404245188f1eb618a46830'

# allow optional env override
TW_SID = os.environ.get("TW_SID", TW_SID)
TW_TOKEN = os.environ.get("TW_TOKEN", TW_TOKEN)
TW_NUMBER = os.environ.get("TW_NUMBER", TW_NUMBER)
OWM_API_KEY = os.environ.get("OWM_API_KEY", OWM_API_KEY)

# quick flag to bypass validation for debugging (set TW_SKIP_VALIDATION=1)
SKIP_VALIDATION = os.environ.get("TW_SKIP_VALIDATION", "") == "1"

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
# In-memory session store (dev). Use Redis in prod if desired.
# -------------------------
call_sessions = {}

def session_set(call_sid, data: dict):
    call_sessions[call_sid] = data

def session_get(call_sid):
    return call_sessions.get(call_sid, {})

def session_pop(call_sid):
    call_sessions.pop(call_sid, None)

# -------------------------
# Helper to send SMS (Twilio)
# -------------------------
def send_sms(to_number, body):
    if not tw_client:
        app.logger.warning("Twilio client not configured — not sending SMS")
        return False
    try:
        tw_client.messages.create(body=body, from_=TW_NUMBER, to=to_number)
        app.logger.info("SMS sent to %s", to_number)
        return True
    except Exception as e:
        app.logger.error("Error sending SMS to %s: %s", to_number, e)
        return False

# -------------------------
# Build URL candidates (aggressive)
# -------------------------
def _build_public_url_candidates(req):
    """
    Return list of candidate public URLs Twilio might have signed.
    Prioritises forwarded proto/host (Railway/Heroku) and includes variants.
    """
    candidates = []

    # forwarded headers commonly set by proxies/platforms
    forwarded_proto = req.headers.get("X-Forwarded-Proto") or req.headers.get("X-Forwarded-Scheme")
    forwarded_host = req.headers.get("X-Forwarded-Host")
    host_header = req.headers.get("Host")

    # path including query (Flask full_path includes query and ends with '?' if none)
    path = req.full_path if req.full_path is not None else req.path
    if path.endswith('?'):
        path = path[:-1]

    # 1) Use forwarded proto + forwarded host if present (highest priority)
    if forwarded_proto and forwarded_host:
        candidates.append(f"{forwarded_proto}://{forwarded_host}{path}")

    # 2) Use forwarded proto + Host header (if Host present)
    if forwarded_proto and host_header:
        candidates.append(f"{forwarded_proto}://{host_header}{path}")

    # 3) Common combos using Host header
    if host_header:
        candidates.append(f"https://{host_header}{path}")
        candidates.append(f"http://{host_header}{path}")

    # 4) Use X-Forwarded-Host if present alone (https/http variants)
    if forwarded_host:
        candidates.append(f"https://{forwarded_host}{path}")
        candidates.append(f"http://{forwarded_host}{path}")

    # 5) Add request.url as seen by Flask (could be internal)
    candidates.append(req.url)

    # 6) If request.url uses http, add https flip and vice versa
    try:
        parsed = urlparse(req.url)
        if parsed.scheme == "http":
            candidates.append(req.url.replace("http://", "https://", 1))
        elif parsed.scheme == "https":
            candidates.append(req.url.replace("https://", "http://", 1))
    except Exception:
        pass

    # 7) Add variants with/without trailing slash
    ext = list(candidates)
    for c in ext:
        if c.endswith("/") and not c.endswith("/?"):
            candidates.append(c.rstrip("/"))
        elif not c.endswith("/"):
            candidates.append(c + "/")

    # Deduplicate while preserving order
    seen = set()
    out = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    app.logger.debug("URL candidates for Twilio validation: %s", out)
    return out

# -------------------------
# Validator decorator (tries params then raw body)
# -------------------------
def require_twilio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if SKIP_VALIDATION:
            app.logger.info("TW_SKIP_VALIDATION=1 -> skipping Twilio signature validation")
            return f(*args, **kwargs)

        if not validator:
            app.logger.warning("No Twilio validator configured (TW_TOKEN missing). Allowing request.")
            return f(*args, **kwargs)

        sig = request.headers.get("X-Twilio-Signature", "")
        # prefer form params (typical Twilio POST)
        form_params = request.form.to_dict()
        raw_body = request.get_data(as_text=True) or ""

        # If both empty and there are query params, include them
        if not form_params and request.args:
            form_params = request.args.to_dict()

        url_candidates = _build_public_url_candidates(request)

        app.logger.debug("Attempting Twilio validation. Signature header: %s", sig)
        app.logger.debug("Form params: %s", form_params)
        app.logger.debug("Raw body length: %d", len(raw_body))

        validated = False
        for candidate in url_candidates:
            try:
                # If form params available, validate with them
                if form_params:
                    if validator.validate(candidate, form_params, sig):
                        app.logger.debug("Validated with candidate (form params): %s", candidate)
                        validated = True
                        break
                else:
                    # No form params -> try validating with raw body (some clients sign raw data)
                    if raw_body:
                        if validator.validate(candidate, raw_body, sig):
                            app.logger.debug("Validated with candidate (raw body): %s", candidate)
                            validated = True
                            break
            except Exception as ex:
                app.logger.debug("Validator exception for candidate %s: %s", candidate, ex)
                continue

        if not validated:
            # Log diagnostics (safe to show here; avoid leaking secrets)
            app.logger.warning("Invalid Twilio signature. Tried candidates: %s", url_candidates)
            app.logger.debug("Request headers: %s", dict(request.headers))
            app.logger.debug("Form params: %s", form_params)
            app.logger.debug("Raw body (first 1000 chars): %s", (raw_body[:1000] + '...') if len(raw_body) > 1000 else raw_body)
            # Helpful message to user in logs — in production you may want to 403 silently
            return Response("Invalid signature", status=403)

        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Minimal weather helpers (kept simple)
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
            return j.get("lat"), j.get("lon"), j.get("name") or pin
    except Exception as e:
        app.logger.debug("zip geocode failed: %s", e)
    try:
        r = session.get("http://api.openweathermap.org/geo/1.0/direct",
                        params={"q": f"{pin},IN", "limit": 1, "appid": OWM_API_KEY}, timeout=8)
        if r.status_code == 200 and r.json():
            i = r.json()[0]
            return i.get("lat"), i.get("lon"), i.get("name") or pin
    except Exception as e:
        app.logger.error("geocode direct failed: %s", e)
    return None, None, None

def fetch_weather(lat, lon):
    try:
        r = session.get("https://api.openweathermap.org/data/2.5/weather",
                        params={"lat": lat, "lon": lon, "appid": OWM_API_KEY, "units": "metric"}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error("fetch_weather error: %s", e)
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
    adv = []
    if temp is not None:
        adv.append(f"वर्तमान: {desc}. तापमान ~{temp:.0f}°C.")
    else:
        adv.append(f"वर्तमान: {desc}.")
    if rain >= 10:
        adv.append("तेज़ बारिश संभव — फसल सुरक्षित रखें।")
    elif rain >= 2:
        adv.append("बारिश का चांस — छिड़काव से पहले मौसम देख लें।")
    if temp is not None and temp >= 40:
        adv.append("अत्यधिक गर्मी — दोपहर में काम कम करें।")
    if temp is not None and temp <= 5:
        adv.append("ठंड से सुरक्षा रखें।")
    if wind_speed >= 10:
        adv.append("हवा तेज़ है — उपकरण सुरक्षित रखें।")
    return " ".join(adv)

# -------------------------
# PIN extraction helper
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
        t_clean = t.strip().lower()
        if t_clean.isdigit():
            for ch in t_clean:
                pin_digits.append(ch)
                if len(pin_digits) == 6:
                    return "".join(pin_digits)
        elif t_clean in NUM_WORDS:
            pin_digits.append(NUM_WORDS[t_clean])
            if len(pin_digits) == 6:
                return "".join(pin_digits)
        else:
            for ch in t_clean:
                if ch in NUM_WORDS:
                    pin_digits.append(NUM_WORDS[ch])
                    if len(pin_digits) == 6:
                        return "".join(pin_digits)
    return None

# -------------------------
# Flask endpoints
# -------------------------
@app.route("/voice", methods=["POST"])
@require_twilio
def voice():
    call_sid = request.values.get("CallSid")
    from_number = request.values.get("From")
    app.logger.info("Incoming call from %s (CallSid=%s)", from_number, call_sid)
    session_set(call_sid, {"from": from_number, "last_speech": ""})
    resp = VoiceResponse()
    g = Gather(input="speech dtmf", num_digits=1, timeout=5, action="/handle-main", method="POST", language="hi-IN")
    g.say("नमस्ते। मंडी भाव के लिए 1, फसल सलाह के लिए 2, मौसम के लिए 3, विशेषज्ञ के लिए 4।", language="hi-IN")
    resp.append(g)
    resp.say("हम आपकी बात नहीं समझ पाए।", language="hi-IN")
    return Response(str(resp), mimetype="text/xml")

@app.route("/handle-main", methods=["POST"])
@require_twilio
def handle_main():
    call_sid = request.values.get("CallSid")
    digits = request.values.get("Digits")
    speech = (request.values.get("SpeechResult") or "").strip()
    if call_sid:
        s = session_get(call_sid)
        s["last_speech"] = speech
        session_set(call_sid, s)
    resp = VoiceResponse()
    choice = digits or (
        "1" if "भाव" in speech else
        "2" if "सलाह" in speech else
        "3" if "मौसम" in speech else
        "4" if "विशेषज्ञ" in speech else None
    )
    if choice == "1":
        resp.say("कानपुर मंडी में गेहूँ का भाव आज ₹2100 प्रति क्विंटल है।", language="hi-IN")
    elif choice == "2":
        g = Gather(input="speech dtmf", action="/handle-advice", method="POST", language="hi-IN")
        g.say("कृपया अपनी फसल और समस्या बताइए।", language="hi-IN")
        resp.append(g)
    elif choice == "3":
        g = Gather(input="speech dtmf", num_digits=6, action="/handle-weather", method="POST", language="hi-IN")
        g.say("कृपया अपना पिनकोड 6 अंकों में बोलें या दबाएँ।", language="hi-IN")
        resp.append(g)
    elif choice == "4":
        resp.say("विशेषज्ञ से जोड़ रहे हैं।", language="hi-IN")
    else:
        resp.say("माफ़ कीजिये, मैं समझ नहीं पाया।", language="hi-IN")
    return Response(str(resp), mimetype="text/xml")

@app.route("/handle-weather", methods=["POST"])
@require_twilio
def handle_weather():
    call_sid = request.values.get("CallSid")
    digits = request.values.get("Digits")
    speech = (request.values.get("SpeechResult") or "").strip()
    if call_sid:
        s = session_get(call_sid)
        s["last_speech"] = speech
        session_set(call_sid, s)
    pin = extract_pin(digits, speech)
    resp = VoiceResponse()
    if not pin:
        resp.say("पिनकोड सही नहीं मिला।", language="hi-IN")
        return Response(str(resp), mimetype="text/xml")
    lat, lon, place = geocode_pin(pin)
    if not lat or not lon:
        resp.say("पिनकोड के लिए स्थान नहीं मिला।", language="hi-IN")
        return Response(str(resp), mimetype="text/xml")
    weather = fetch_weather(lat, lon)
    advice = make_weather_advice(weather)
    resp.say(f"{place} के लिए मौसम: {advice}", language="hi-IN")
    return Response(str(resp), mimetype="text/xml")

# -------------------------
# Run server
# -------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 3000))
    app.run(host="0.0.0.0", port=port, debug=False)
