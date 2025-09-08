# app.py
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

# Optional Redis support (only used if REDIS_URL set)
try:
    import redis
except Exception:
    redis = None

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# -------------------------
# CONFIG (HARDCODED AS REQUESTED)
# -------------------------
# NOTE: you asked to keep hardcoded credentials — these are kept here.
TW_SID = 'AC07e7e3c3277c49afc1f06feec329afaa'
TW_TOKEN = '7042acd3973a3ca33a1c38a4bd99d1a2'
TW_NUMBER = '+15074835441'
OWM_API_KEY = 'b658cc9374404245188f1eb618a46830'

# You may optionally override via environment variables (left in so you can swap later)
TW_SID = os.environ.get("TW_SID", TW_SID)
TW_TOKEN = os.environ.get("TW_TOKEN", TW_TOKEN)
TW_NUMBER = os.environ.get("TW_NUMBER", TW_NUMBER)
OWM_API_KEY = os.environ.get("OWM_API_KEY", OWM_API_KEY)

# Optional Redis URL — if set and redis package available, we'll use Redis for session storage
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
# HTTP session with retries for external requests
# -------------------------
session = requests.Session()
retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
adapter = HTTPAdapter(max_retries=retries)
session.mount("http://", adapter)
session.mount("https://", adapter)

# -------------------------
# Session store: Redis (if REDIS_URL) else in-memory (dev only)
# -------------------------
use_redis = False
redis_client = None
if REDIS_URL and redis:
    try:
        redis_client = redis.from_url(REDIS_URL)
        # quick test
        redis_client.ping()
        use_redis = True
        app.logger.info("Using Redis session store")
    except Exception as e:
        app.logger.warning("Failed to connect to Redis at REDIS_URL: %s — falling back to in-memory", e)

call_sessions = {}  # in-memory fallback (dev only)

def session_set(call_sid, data: dict):
    if use_redis and redis_client:
        try:
            redis_client.set(f"call:{call_sid}", json.dumps(data), ex=3600)  # expire 1 hour
            return
        except Exception as e:
            app.logger.warning("Redis set failed: %s — using memory", e)
    call_sessions[call_sid] = data

def session_get(call_sid):
    if use_redis and redis_client:
        try:
            v = redis_client.get(f"call:{call_sid}")
            if v:
                return json.loads(v)
            return {}
        except Exception as e:
            app.logger.warning("Redis get failed: %s — using memory", e)
            return call_sessions.get(call_sid, {})
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
# Twilio SMS helper
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
# Twilio request validation decorator
# -------------------------
def require_twilio(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if validator:
            sig = request.headers.get('X-Twilio-Signature', '')
            # Twilio expects the full URL without query string modifications
            valid = False
            try:
                valid = validator.validate(request.url, request.form.to_dict(), sig)
            except Exception as ex:
                app.logger.warning("Twilio request validator error: %s", ex)
                valid = False
            if not valid:
                app.logger.warning("Invalid Twilio signature for URL=%s", request.url)
                return Response("Invalid signature", status=403)
        return f(*args, **kwargs)
    return wrapper

# -------------------------
# Geocoding & weather helpers (OpenWeather)
# -------------------------
def geocode_pin(pin):
    """Resolve Indian PIN to lat/lon using OpenWeather geocoding."""
    if not OWM_API_KEY:
        app.logger.error("OWM_API_KEY not configured")
        return None, None, None
    pin = str(pin).strip()
    try:
        zurl = "http://api.openweathermap.org/geo/1.0/zip"
        params = {"zip": f"{pin},IN", "appid": OWM_API_KEY}
        r = session.get(zurl, params=params, timeout=8)
        if r.status_code == 200:
            j = r.json()
            lat = j.get("lat"); lon = j.get("lon"); name = j.get("name") or pin
            if lat and lon:
                return lat, lon, name
    except Exception as e:
        app.logger.debug("zip geocode failed: %s", e)

    try:
        gurl = "http://api.openweathermap.org/geo/1.0/direct"
        params = {"q": f"{pin},IN", "limit": 1, "appid": OWM_API_KEY}
        r = session.get(gurl, params=params, timeout=8)
        if r.status_code == 200 and r.json():
            item = r.json()[0]
            return item.get("lat"), item.get("lon"), item.get("name") or pin
    except Exception as e:
        app.logger.error("geocode direct failed: %s", e)
    return None, None, None

def fetch_weather(lat, lon):
    if not OWM_API_KEY:
        app.logger.error("OWM_API_KEY not configured")
        return None
    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"lat": lat, "lon": lon, "appid": OWM_API_KEY, "units": "metric"}
        r = session.get(url, params=params, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        app.logger.error("fetch_weather error: %s", e)
        return None

def make_weather_advice(w):
    if not w:
        return "मौसम जानकारी उपलब्ध नहीं है। कृपया बाद में पुनः प्रयास करें।"
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
    else:
        adv.append(f"वर्तमान: {desc}.")

    if rain >= 10:
        adv.append("तेज़ बारिश संभव — फसल/आवरण सुरक्षित करें।")
    elif rain >= 2:
        adv.append("बारिश का चांस — छिड़काव से पहले मौसम देख लें।")

    if temp is not None and temp >= 40:
        adv.append("अत्यधिक गर्मी — 12-16 बजे में बाहर काम सीमित करें।")
    if temp is not None and temp <= 5:
        adv.append("ठंड की संभावना — नाज़ुक फसल/पशु सुरक्षा रखें।")

    if wind_speed >= 10:
        adv.append("हवा तेज़ है — हल्के उपकरण सुरक्षित रखें।")

    if not adv:
        adv.append("कोई तत्काल चेतावनी नहीं।")

    return " ".join(adv)

# -------------------------
# PIN extraction (DTMF or speech)
# -------------------------
NUM_WORDS = {
    "zero":"0","oh":"0","o":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9",
    "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
    "एक":"1","दो":"2","तीन":"3","चार":"4","पाँच":"5","पांच":"5","छह":"6","सात":"7","आठ":"8","नौ":"9","शून्य":"0"
}
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
                if ch.isdigit():
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
# Flask endpoints (Twilio webhooks)
# -------------------------
@app.route('/voice', methods=['POST'])
@require_twilio
def voice():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    app.logger.info("Incoming call from %s (CallSid=%s)", from_number, call_sid)
    session_set(call_sid, {'from': from_number, 'last_speech': ''})
    resp = VoiceResponse()
    g = Gather(input='speech dtmf', num_digits=1, timeout=5, action='/handle-main', method='POST', language='hi-IN')
    g.say('नमस्ते। मंडी भाव के लिए 1, फसल सलाह के लिए 2, मौसम के लिए 3, विशेषज्ञ के लिए 4।', language='hi-IN')
    resp.append(g)
    resp.say('हम आपकी बात नहीं समझ पाए। अलविदा।', language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-main', methods=['POST'])
@require_twilio
def handle_main():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    digits = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    if call_sid:
        s = session_get(call_sid)
        s['last_speech'] = speech
        session_set(call_sid, s)
    app.logger.info("handle-main: from=%s callSid=%s Digits=%s Speech=%s", from_number, call_sid, digits, speech)
    resp = VoiceResponse()

    choice = digits or (
        '1' if 'भाव' in speech else
        '2' if 'सलाह' in speech else
        '3' if 'मौसम' in speech else
        '4' if 'विशेषज्ञ' in speech else None
    )

    if choice == '1':
        resp.say('कानपुर मंडी में गेहूँ का भाव आज ₹2100 प्रति क्विंटल है। SMS पर विस्तृत तालिका भेजने के लिए 1 दबाएँ।', language='hi-IN')
        resp.gather(num_digits=1, action='/price-sms-consent', method='POST', language='hi-IN')
        resp.say('धन्यवाद।', language='hi-IN')

    elif choice == '2':
        g = Gather(input='speech dtmf', action='/handle-advice', method='POST', language='hi-IN')
        g.say('कृपया अपनी फसल और समस्या बताइए। उदाहरण के लिए: धान की पत्तियाँ पीली हो रही हैं।', language='hi-IN')
        resp.append(g)

    elif choice == '3':
        g = Gather(
            input='speech dtmf',
            num_digits=6,
            timeout=8,
            action='/handle-weather',
            method='POST',
            language='hi-IN',
            speech_timeout='auto',
            hints='पिनकोड,zipcode,पिन'
        )
        g.say('कृपया अपना पिनकोड 6 अंकों में बोलें या दबाएँ। उदाहरण: 1 1 0 0 0 1।', language='hi-IN')
        resp.append(g)
        resp.say('माफ़ कीजिये, पिनकोड नहीं मिला। मेनू पर लौटाया जा रहा है।', language='hi-IN')
        resp.redirect('/voice')

    elif choice == '4':
        resp.say('हम आपको विशेषज्ञ से जोड़ रहे हैं। कृपया प्रतीक्षा करें।', language='hi-IN')
        resp.hangup()
    else:
        resp.say('माफ़ कीजिए, मैं समझ नहीं पाया। मेनू पर लौटाया जा रहा है।', language='hi-IN')
        resp.redirect('/voice')

    return Response(str(resp), mimetype='text/xml')

@app.route('/price-sms-consent', methods=['POST'])
@require_twilio
def price_sms_consent():
    call_sid = request.values.get('CallSid')
    digit = request.values.get('Digits')
    from_number = request.values.get('From')
    app.logger.info("price-sms-consent: from=%s callSid=%s digit=%s", from_number, call_sid, digit)
    resp = VoiceResponse()
    if digit == '1':
        sms_body = ("कानपुर मंडी आज के भाव:\nगेहूँ: ₹2100/qtl\nधान: ₹2400/qtl\nमक्का: ₹1850/qtl\n-- Khet Sahayak")
        ok = send_sms(from_number, sms_body)
        if ok:
            resp.say('ठीक है, मंडी भाव SMS पर भेज दिया गया है। धन्यवाद।', language='hi-IN')
        else:
            resp.say('क्षमा करें, SMS भेजने में समस्या हुई।', language='hi-IN')
    else:
        resp.say('ठीक है, SMS नहीं भेजा जाएगा।', language='hi-IN')
    resp.hangup()
    session_pop(call_sid)
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-advice', methods=['POST'])
@require_twilio
def handle_advice():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    speech = request.values.get('SpeechResult') or ''
    if call_sid:
        s = session_get(call_sid)
        s['last_speech'] = speech
        session_set(call_sid, s)
    app.logger.info("handle-advice: from=%s callSid=%s speech=%s", from_number, call_sid, speech)

    resp = VoiceResponse()
    disclaimer = "नोट: यह सामान्य सलाह है। केमिकल उपयोग से पहले स्थानीय कृषि अधिकारी या लेबल निर्देश अवश्य देखें।"

    speech_l = speech.lower()
    if any(k in speech_l for k in ['धान', 'चावल', 'paddy', 'rice']):
        if any(k in speech_l for k in ['पीली', 'पीला', 'yellow']):
            reply = ("धान में पत्तियाँ पीली होना: आमतौर पर नाइट्रोजन की कमी या पानी की कमी हो सकती है। "
                     "कार्रवाई: 1) सिंचाई स्थिति जाँचें। 2) अगर नाइट्रोजन कमी है तो अनुशंसित मात्रा में यूरिया दें। "
                     "3) अधिक सुनिश्चित करने के लिए नमूना भेजें। " + disclaimer)
        else:
            reply = (f"आपने कहा: {speech}. सामान्य सलाह: फसल की वृद्धि के अनुसार जल और पोषण दें। "
                     "अधिक विशिष्ट सलाह के लिए SMS पर भेजने के लिए 1 दबाएँ या विशेषज्ञ से जोड़ने के लिए 4 दबाएँ। " + disclaimer)
    elif any(k in speech_l for k in ['गेहूँ', 'wheat']):
        if any(k in speech_l for k in ['पीला', 'पतला']):
            reply = "गेहूँ में पीला पत्ते आमतौर पर नाइट्रोजन कमी या पानी कम होने से होता है। उचित नमी और उर्वरक प्रबंधन करें। " + disclaimer
        else:
            reply = f"आपने कहा: {speech}. विस्तृत सलाह SMS पर भेजने के लिए 1 दबाएँ। " + disclaimer
    else:
        reply = f"आपने कहा: {speech}. कृपया अधिक जानकारी दें या विशेषज्ञ से कनेक्ट करें। " + disclaimer

    resp.say(reply, language='hi-IN')
    g = Gather(num_digits=1, action='/advice-sms-consent', method='POST', language='hi-IN')
    g.say('क्या आप विस्तृत सलाह SMS पर प्राप्त करना चाहेंगे? हाँ के लिए 1 दबाएँ, नहीं के लिए कुछ भी न दबाएँ।', language='hi-IN')
    resp.append(g)
    resp.say('आपका धन्यवाद।', language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

@app.route('/advice-sms-consent', methods=['POST'])
@require_twilio
def advice_sms_consent():
    call_sid = request.values.get('CallSid')
    digit = request.values.get('Digits')
    from_number = request.values.get('From')
    s = session_get(call_sid)
    last_speech = s.get('last_speech', '')
    app.logger.info("advice-sms-consent: from=%s callSid=%s digit=%s last_speech=%s", from_number, call_sid, digit, last_speech)
    resp = VoiceResponse()
    if digit == '1':
        sms_body = ("विस्तृत सलाह:\n1) स्थिति जाँचें: नमूना/तस्वीर लें\n2) पोषक तत्व संतुलन: नाइट्रोजन की कमी हो तो स्थानीय गाइड के अनुसार खाद डालें\n3) सिंचाई व्यवस्थापन: आवश्यकता अनुसार पानी दें\n(यह सामान्य सलाह है — स्थानीय विशेषज्ञ से संपर्क करें)\nआपने पूछा: " + (last_speech or "—") + "\n-- Khet Sahayak")
        ok = send_sms(from_number, sms_body)
        if ok:
            resp.say('ठीक है, विस्तृत सलाह SMS पर भेज दी गयी है।', language='hi-IN')
        else:
            resp.say('क्षमा करें, SMS भेजने में समस्या हुई।', language='hi-IN')
    else:
        resp.say('ठीक है, SMS नहीं भेजा जाएगा।', language='hi-IN')
    resp.hangup()
    session_pop(call_sid)
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-weather', methods=['POST'])
@require_twilio
def handle_weather():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    digits = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    if call_sid:
        s = session_get(call_sid)
        s['last_speech'] = speech
        session_set(call_sid, s)
    app.logger.info("handle-weather: from=%s callSid=%s Digits=%s Speech=%s", from_number, call_sid, digits, speech)

    pin = extract_pin(digits, speech)
    resp = VoiceResponse()
    if not pin:
        resp.say('माफ़ कीजिये, पिनकोड सही तरीके से नहीं मिला। कृपया कॉल वापस कर के पुनः प्रयास करें या SMS पर पिन भेजें।', language='hi-IN')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    lat, lon, place_name = geocode_pin(pin)
    if not lat or not lon:
        resp.say(f'{pin} के लिए स्थान नहीं मिला। कृपया पिनकोड सत्यापित करें।', language='hi-IN')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    weather = fetch_weather(lat, lon)
    advice = make_weather_advice(weather)
    place_readable = place_name or pin

    resp.say(f"{place_readable} के लिए मौसम: {advice}", language='hi-IN')
    g = Gather(num_digits=1, action='/weather-sms-consent', method='POST', language='hi-IN')
    g.say('क्या आप यह मौसम SMS पर पाना चाहेंगे? हाँ के लिए 1 दबाएँ।', language='hi-IN')
    resp.append(g)
    resp.hangup()

    s = session_get(call_sid)
    s['last_weather'] = weather
    s['last_place'] = place_readable
    session_set(call_sid, s)
    return Response(str(resp), mimetype='text/xml')

@app.route('/weather-sms-consent', methods=['POST'])
@require_twilio
def weather_sms_consent():
    call_sid = request.values.get('CallSid')
    digit = request.values.get('Digits')
    from_number = request.values.get('From')
    session = session_get(call_sid)
    weather = session.get('last_weather')
    place = session.get('last_place', 'आपका स्थान')
    app.logger.info("weather-sms-consent: from=%s callSid=%s digit=%s", from_number, call_sid, digit)
    resp = VoiceResponse()
    if digit == '1' and weather:
        desc = weather.get("weather", [{}])[0].get("description", "")
        temp = weather.get("main", {}).get("temp")
        rain = 0
        if "rain" in weather:
            rain = weather["rain"].get("1h", 0) or weather["rain"].get("3h", 0) or 0
        sms_body = f"मौसम ({place}): {desc}. तापमान ~{temp}°C. बारिश: {rain} mm. सलाह: {make_weather_advice(weather)} — Khet Sahayak"
        ok = send_sms(from_number, sms_body)
        if ok:
            resp.say('मौसम SMS पर भेज दिया गया है।', language='hi-IN')
        else:
            resp.say('SMS भेजने में समस्या हुई।', language='hi-IN')
    else:
        resp.say('ठीक है।', language='hi-IN')
    resp.hangup()
    session_pop(call_sid)
    return Response(str(resp), mimetype='text/xml')

# -------------------------
# Run server
# -------------------------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    # Do not run with debug=True in production.
    debug_mode = os.environ.get("FLASK_ENV", "") == "development"
    app.run(host='0.0.0.0', port=port, debug=debug_mode)
