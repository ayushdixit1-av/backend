# app.py
import os
import logging
import re
import requests
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Gather
from twilio.rest import Client

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# -------------------------
# CONFIG (use env vars in production)
# -------------------------
TW_SID =   'AC07e7e3c3277c49afc1f06feec329afaa' # e.g. 'ACxxxxxxxx'
TW_TOKEN = '7042acd3973a3ca33a1c38a4bd99d1a2'# e.g. 'your_auth_token'
TW_NUMBER = '+15074835441'# e.g. '+1XXXXXXXXXX'
OWM_API_KEY = 'b658cc9374404245188f1eb618a46830' # set this to your OpenWeather API key

# Initialize Twilio client (if configured)
tw_client = None
if TW_SID and TW_TOKEN and "PLACEHOLDER" not in (TW_SID, TW_TOKEN):
    try:
        tw_client = Client(TW_SID, TW_TOKEN)
        app.logger.info("Twilio client initialized")
    except Exception as e:
        app.logger.error("Twilio init failed: %s", e)
        tw_client = None
else:
    app.logger.warning("Twilio credentials not configured (or placeholders used) — SMS disabled")

# In-memory call session store (dev only)
call_sessions = {}

# -------------------------
# Helpers
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

def geocode_pin(pin):
    """Resolve Indian PIN via OpenWeather geocoding. Returns (lat, lon, name) or (None, None, None)."""
    if not OWM_API_KEY:
        app.logger.error("OWM_API_KEY not set")
        return None, None, None
    pin = str(pin).strip()
    try:
        # Try zip endpoint
        zurl = "http://api.openweathermap.org/geo/1.0/zip"
        params = {"zip": f"{pin},IN", "appid": OWM_API_KEY}
        r = requests.get(zurl, params=params, timeout=8)
        if r.status_code == 200:
            j = r.json()
            lat = j.get("lat"); lon = j.get("lon"); name = j.get("name") or pin
            if lat and lon:
                return lat, lon, name
    except Exception:
        app.logger.debug("zip geocode failed, trying direct search")

    try:
        gurl = "http://api.openweathermap.org/geo/1.0/direct"
        params = {"q": f"{pin},IN", "limit": 1, "appid": OWM_API_KEY}
        r = requests.get(gurl, params=params, timeout=8)
        if r.status_code == 200 and r.json():
            item = r.json()[0]
            return item.get("lat"), item.get("lon"), item.get("name") or pin
    except Exception as e:
        app.logger.error("direct geocode failed: %s", e)
    return None, None, None

def fetch_weather(lat, lon):
    if not OWM_API_KEY:
        app.logger.error("OWM_API_KEY not set")
        return None
    try:
        url = "https://api.openweathermap.org/data/2.5/weather"
        params = {"lat": lat, "lon": lon, "appid": OWM_API_KEY, "units": "metric"}
        r = requests.get(url, params=params, timeout=8)
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

# Extract a 6-digit PIN from DTMF or spoken text (supports numeric words)
NUM_WORDS = {
    "zero":"0","oh":"0","o":"0","one":"1","two":"2","three":"3","four":"4","five":"5","six":"6","seven":"7","eight":"8","nine":"9",
    # Hindi numerals and words
    "०":"0","१":"1","२":"2","३":"3","४":"4","५":"5","६":"6","७":"7","८":"8","९":"9",
    "एक":"1","दो":"2","दोह":"2","तीन":"3","चार":"4","पाँच":"5","पांच":"5","छह":"6","सात":"7","आठ":"8","नौ":"9","शून्य":"0"
}
def extract_pin(digits, speech):
    """Return a 6-digit PIN string or None."""
    # prefer DTMF digits if exactly 6
    if digits and re.fullmatch(r'\d{6}', digits.strip()):
        return digits.strip()
    s = (speech or "").lower()
    # look for contiguous 6-digit in speech
    m = re.search(r'(\d{6})', s)
    if m:
        return m.group(1)
    # tokenise and map number words -> digits
    tokens = re.findall(r'\d+|[०१२३४५६७८९]+|[a-zA-Z]+|[\u0900-\u097F]+', s)
    pin_digits = []
    for t in tokens:
        t_clean = t.strip().lower()
        if t_clean.isdigit():
            # if token has several digits, take them
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
            # try to extract single Devanagari digit char
            for ch in t_clean:
                if ch in NUM_WORDS:
                    pin_digits.append(NUM_WORDS[ch])
                    if len(pin_digits) == 6:
                        return "".join(pin_digits)
    return None

# -------------------------
# Flask endpoints
# -------------------------
@app.route('/voice', methods=['POST'])
def voice():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    app.logger.info("Incoming call from %s (CallSid=%s)", from_number, call_sid)
    call_sessions[call_sid] = {'from': from_number, 'last_speech': ''}
    resp = VoiceResponse()
    g = Gather(input='speech dtmf', num_digits=1, timeout=5, action='/handle-main', method='POST', language='hi-IN')
    g.say('नमस्ते। मंडी भाव के लिए 1, फसल सलाह के लिए 2, मौसम के लिए 3, विशेषज्ञ के लिए 4।', language='hi-IN')
    resp.append(g)
    resp.say('हम आपकी बात नहीं समझ पाए। अलविदा।', language='hi-IN')
    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-main', methods=['POST'])
def handle_main():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    digits = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    if call_sid:
        call_sessions.setdefault(call_sid, {})['last_speech'] = speech
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
        # Prompt for PIN (DTMF) or spoken PIN
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
        # fallback if no input
        resp.say('माफ़ कीजिये, पिनकोड नहीं मिला। मेनू पर लौटाया जा रहा है।', language='hi-IN')
        resp.redirect('/voice')

    elif choice == '4':
        resp.say('हम आपको विशेषज्ञ से जोड़ रहे हैं। कृपया प्रतीक्षा करें।', language='hi-IN')
        resp.hangup()
    else:
        resp.say('माफ़ कीजिए, मैं समझ नहीं पाया। मेनू पर लौटाया जा रहा है।', language='hi-IN')
        resp.redirect('/voice')

    return Response(str(resp), mimetype='text/xml')

@app.route('/handle-weather', methods=['POST'])
def handle_weather():
    call_sid = request.values.get('CallSid')
    from_number = request.values.get('From')
    digits = request.values.get('Digits')
    speech = (request.values.get('SpeechResult') or '').strip()
    if call_sid:
        call_sessions.setdefault(call_sid, {})['last_speech'] = speech
    app.logger.info("handle-weather: from=%s callSid=%s Digits=%s Speech=%s", from_number, call_sid, digits, speech)

    # extract PIN
    pin = extract_pin(digits, speech)
    resp = VoiceResponse()
    if not pin:
        resp.say('माफ़ कीजिये, पिनकोड सही तरीके से नहीं मिला। कृपया कॉल वापस कर के पुनः प्रयास करें या SMS पर पिन भेजें।', language='hi-IN')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    # geocode
    lat, lon, place_name = geocode_pin(pin)
    if not lat or not lon:
        resp.say(f'{pin} के लिए स्थान नहीं मिला। कृपया पिनकोड सत्यापित करें।', language='hi-IN')
        resp.hangup()
        return Response(str(resp), mimetype='text/xml')

    weather = fetch_weather(lat, lon)
    advice = make_weather_advice(weather)
    place_readable = place_name or pin

    # speak short advice and ask to send SMS
    resp.say(f"{place_readable} के लिए मौसम: {advice}", language='hi-IN')
    g = Gather(num_digits=1, action='/weather-sms-consent', method='POST', language='hi-IN')
    g.say('क्या आप यह मौसम SMS पर पाना चाहेंगे? हाँ के लिए 1 दबाएँ।', language='hi-IN')
    resp.append(g)
    resp.hangup()

    # store snapshot for SMS
    call_sessions.setdefault(call_sid, {})['last_weather'] = weather
    call_sessions.setdefault(call_sid, {})['last_place'] = place_readable
    return Response(str(resp), mimetype='text/xml')

@app.route('/weather-sms-consent', methods=['POST'])
def weather_sms_consent():
    call_sid = request.values.get('CallSid')
    digit = request.values.get('Digits')
    from_number = request.values.get('From')
    session = call_sessions.get(call_sid, {})
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
    call_sessions.pop(call_sid, None)
    return Response(str(resp), mimetype='text/xml')

# Run
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=True)
