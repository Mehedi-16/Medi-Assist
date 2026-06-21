"""
MediAssist - Health Monitor Voice Assistant
Hardware: Raspberry Pi + Arduino Uno (Health Monitor only)
Arduino: BPM + SpO2 + Temperature + LCD
Serial format: BPM:75.2,SpO2:98.1,Temp:36.5
Run: python3 medi_assist.py
"""

import speech_recognition as sr
from gtts import gTTS
import os
import time
import re
import random
import threading
import serial
import serial.tools.list_ports
from collections import deque
from fuzzywuzzy import process
import google.generativeai as genai
from pydub import AudioSegment
from pydub.playback import play
import tempfile

# ?????????????????????????????????????????????
#  CONFIGURATION
# ?????????????????????????????????????????????

GEMINI_API_KEY = "AIzaSyAQ.Ab8RN6IYTEdOItXW0ufE1AP_x2wjEBUIWuh8UxAplpakOog3zg"

# Wake words
WAKE_WORDS        = ["hey medi", "medi assist", "medi", "hey robot"]
WAKE_WORD_ENABLED = True

# Listening settings
PAUSE_THRESHOLD          = 0.8
PHRASE_TIME_LIMIT        = 10
ADJUST_NOISE_DURATION    = 1
DYNAMIC_ENERGY_THRESHOLD = True

# Retry
MAX_RETRY_ATTEMPTS = 3
RETRY_DELAY        = 2

# Context memory
CONTEXT_MEMORY_SIZE = 5
MAX_CHAT_HISTORY    = 10

# Arduino Health Monitor port (None = auto-detect)
ARDUINO_HEALTH_PORT = None   # e.g. '/dev/ttyUSB0'
ARDUINO_BAUD        = 9600

# ?????????????????????????????????????????????
#  ARDUINO CONNECTION
# ?????????????????????????????????????????????

arduino_health = None

def find_arduino_ports():
    ports = list(serial.tools.list_ports.comports())
    found = []
    for p in ports:
        if any(x in (p.description or "") for x in ['Arduino', 'CH340']) \
           or 'ttyUSB' in p.device or 'ttyACM' in p.device:
            found.append(p.device)
            print(f"Found Arduino at: {p.device}")
    return found

def connect_arduino():
    global arduino_health
    ports = [ARDUINO_HEALTH_PORT] if ARDUINO_HEALTH_PORT else find_arduino_ports()

    for port in ports:
        try:
            arduino_health = serial.Serial(port, ARDUINO_BAUD, timeout=1)
            print(f"Health Arduino connected: {port}")
            return
        except Exception as e:
            print(f"Could not connect on {port}: {e}")

    print("WARNING: Health Arduino not connected!")

# ?????????????????????????????????????????????
#  HEALTH DATA (Background Thread)
# ?????????????????????????????????????????????

health_data = {
    "BPM"  : 0.0,
    "SpO2" : 0.0,
    "Temp" : 0.0,
    "valid": False
}

def read_health_data_thread():
    """Continuously reads health data from Arduino."""
    global health_data
    while True:
        try:
            if arduino_health and arduino_health.is_open:
                line = arduino_health.readline().decode('utf-8', errors='ignore').strip()
                # Format: BPM:75.2,SpO2:98.1,Temp:36.5
                if 'BPM:' in line and 'SpO2:' in line and 'Temp:' in line:
                    parts = {}
                    for seg in line.split(','):
                        if ':' in seg:
                            k, v = seg.split(':', 1)
                            try:
                                parts[k.strip()] = float(v.strip())
                            except:
                                pass
                    if {'BPM', 'SpO2', 'Temp'}.issubset(parts):
                        health_data['BPM']   = parts['BPM']
                        health_data['SpO2']  = parts['SpO2']
                        health_data['Temp']  = parts['Temp']
                        # Finger ??? ???? check (Arduino ?? same logic)
                        health_data['valid'] = (
                            parts['BPM']  > 50  and
                            parts['BPM']  < 120 and
                            parts['Temp'] > 35.0
                        )
            else:
                time.sleep(1)
        except Exception:
            time.sleep(0.5)

# ?????????????????????????????????????????????
#  GEMINI API
# ?????????????????????????????????????????????

gemini_model = None
def init_gemini():
    global gemini_model
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel('gemini-1.5-flash')
        print("Gemini initialized successfully.")
    except Exception as e:
        print(f"Gemini init error: {e}")
        gemini_model = None

chat_history = []

def add_to_chat_history(role, content):
    chat_history.append({"role": role, "content": content})
    if len(chat_history) > MAX_CHAT_HISTORY:
        chat_history.pop(0)

def get_gemini_response(prompt):
    if gemini_model is None:
        return "Sorry, I cannot connect to my knowledge base right now."
    try:
        add_to_chat_history("user", prompt)
        context = (
            "You are MediAssist, a medical robot voice assistant in a hospital. "
            "Keep responses very brief (1-2 sentences), clear, and helpful. "
            "If asked about health values, give simple medical advice. "
            "Previous conversation:\n"
        )
        for msg in chat_history[:-1]:
            context += f"{msg['role']}: {msg['content']}\n"
        context += f"\nCurrent: {prompt}"

        response     = gemini_model.generate_content(context)
        cleaned_text = re.sub(r'[*#]', '', response.text).strip()

        sentences = [s.strip() for s in cleaned_text.split('.') if s.strip()]
        if len(sentences) > 2:
            cleaned_text = '. '.join(sentences[:2]) + '.'

        add_to_chat_history("assistant", cleaned_text)
        return cleaned_text
    except Exception as e:
        print(f"Gemini error: {e}")
        return "Sorry, I could not get a response right now."

# ?????????????????????????????????????????????
#  COMMAND TEMPLATES
# ?????????????????????????????????????????????

command_templates = {
    "health_check": [
        "health", "vitals", "my health", "heart rate", "heartbeat",
        "oxygen", "spo2", "temperature", "body temperature",
        "check health", "health status", "how am i", "my condition",
        "?????????", "????????", "?????????", "????????"
    ],
    "emergency": [
        "emergency", "help", "urgent", "call doctor", "call nurse",
        "i need help", "danger", "critical",
        "?????", "???????", "??????? ????", "????"
    ],
    "greeting": [
        "hello", "hi", "hey", "good morning", "good afternoon",
        "good evening", "??????", "???????", "?????????????????"
    ],
    "name": [
        "what's your name", "who are you", "your name",
        "????? ??? ??", "?? ????"
    ],
    "capabilities": [
        "what can you do", "help", "features", "commands",
        "???? ?? ???? ????", "???????"
    ],
    "time": [
        "what time is it", "time", "current time",
        "????? ????", "???? ??"
    ],
    "thanks": [
        "thank you", "thanks", "appreciate it",
        "???????", "????????"
    ],
    "user_name": [
        "my name is", "i am", "call me", "i'm",
        "???? ???", "???"
    ],
    "exit": [
        "exit", "quit", "goodbye", "bye", "shutdown",
        "???? ???", "??????", "?????? ?????"
    ],
}

# ?????????????????????????????????????????????
#  RESPONSES
# ?????????????????????????????????????????????
responses = {
    "greeting": [
        "Hello! I am MediAssist, your medical robot. How can I help you?",
        "Hi there! MediAssist at your service. How are you feeling?",
        "Hello! I am here to assist you. What do you need?"
    ],
    "name"        : "I am MediAssist, your hospital robot assistant. I monitor health and assist medical staff.",
    "capabilities": ("I can check your heart rate, oxygen level, and body temperature. "
                     "I can also respond to emergencies and answer your medical questions."),
    "time"        : "The current time is {current_time}.",
    "thanks"      : [
        "You are welcome! Stay healthy.",
        "My pleasure! Let me know if you need anything.",
        "Anytime! That is what I am here for."
    ],
    "emergency"   : "Emergency alert! Calling for help immediately. Please stay calm.",
    "health_normal": ("Your heart rate is {bpm} beats per minute, "
                      "oxygen level is {spo2} percent, "
                      "and body temperature is {temp} degrees Celsius. "
                      "Everything looks normal."),
    "health_alert"  : "Warning! {alerts}. Please consult a doctor immediately.",
    "health_no_data": "Please place your finger on the sensor so I can read your vitals.",
    "user_name_confirm": "Nice to meet you, {name}! How can I assist you today?",
    "exit"   : ["Goodbye! Stay safe and healthy.", "Shutting down. Take care!"],
    "unknown": [
        "I am not sure about that. Let me think...",
        "Could you please rephrase that?",
        "I did not understand. Can you say it differently?"
    ],
    "no_command"    : "I did not hear anything. Could you please repeat?",
    "activated"     : "MediAssist activated and ready.",
    "wake_listening": "I am listening.",
    "initial"       : "Hello! I am MediAssist. I am here to monitor your health and assist you.",
    "shutdown"      : "MediAssist shutting down. Goodbye!",
}

# ?????????????????????????????????????????????
#  CONVERSATION STATE
# ?????????????????????????????????????????????

conversation_state = {
    "user_name"         : None,
    "session_start_time": time.time(),
}

context_memory = deque(maxlen=CONTEXT_MEMORY_SIZE)

# ?????????????????????????????????????????????
#  SPEAK
# ?????????????????????????????????????????????

def speak(text):
    print(f"MediAssist: {text}")
    def _speak():
        tts = gTTS(text=text, lang="en", slow=False)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as fp:
            temp_path = fp.name
        tts.save(temp_path)
        sound = AudioSegment.from_mp3(temp_path)
        play(sound)
        os.remove(temp_path)

    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            _speak()
            return
        except Exception as e:
            print(f"Speak attempt {attempt+1} failed: {e}")
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                time.sleep(RETRY_DELAY)

# ?????????????????????????????????????????????
#  LISTEN
# ?????????????????????????????????????????????

def listen_for_audio(timeout=5, phrase_time_limit=10, adjust_noise=True):
    r = sr.Recognizer()
    r.pause_threshold = PAUSE_THRESHOLD
    with sr.Microphone() as source:
        if adjust_noise and DYNAMIC_ENERGY_THRESHOLD:
            print(f"Adjusting noise ({ADJUST_NOISE_DURATION}s)...")
            r.adjust_for_ambient_noise(source, duration=ADJUST_NOISE_DURATION)
        try:
            print("Listening...")
            audio = r.listen(source, timeout=timeout, phrase_time_limit=phrase_time_limit)
            return audio
        except sr.WaitTimeoutError:
            return None
        except Exception as e:
            print(f"Audio error: {e}")
            return None
def recognize_speech(audio):
    if audio is None:
        return None
    r = sr.Recognizer()
    # English first
    try:
        text = r.recognize_google(audio, language="en-US")
        print(f"Recognized (EN): {text}")
        return text.lower()
    except sr.UnknownValueError:
        pass
    except sr.RequestError as e:
        print(f"STT error: {e}")
        return None
    # Bengali fallback
    try:
        text = r.recognize_google(audio, language="bn-BD")
        print(f"Recognized (BN): {text}")
        return text.lower()
    except:
        return None

# ?????????????????????????????????????????????
#  WAKE WORD
# ?????????????????????????????????????????????

wake_word_activated = False

def listen_for_wake_word():
    global wake_word_activated
    if not WAKE_WORD_ENABLED or wake_word_activated:
        return True

    print("Waiting for wake word...")
    r = sr.Recognizer()

    while True:
        audio = listen_for_audio(timeout=None, phrase_time_limit=3,
                                  adjust_noise=not wake_word_activated)
        if audio is None:
            continue
        try:
            text = r.recognize_google(audio, language="en-US").lower()
            print(f"Heard: {text}")
            if any(w in text for w in WAKE_WORDS):
                wake_word_activated = True
                return True
        except:
            continue

# ?????????????????????????????????????????????
#  COMMAND MATCHING
# ?????????????????????????????????????????????

SIMILARITY_THRESHOLD = 65

def match_command(text):
    if text is None:
        return None, 0
    best_match      = None
    best_confidence = 0
    for cmd_type, templates in command_templates.items():
        match, confidence = process.extractOne(text, templates)
        if confidence > best_confidence:
            best_match      = cmd_type
            best_confidence = confidence
    if best_confidence >= SIMILARITY_THRESHOLD:
        return best_match, best_confidence
    return None, 0

# ?????????????????????????????????????????????
#  HEALTH ALERT CHECK
# ?????????????????????????????????????????????

def check_health_alerts():
    alerts = []
    bpm  = health_data['BPM']
    spo2 = health_data['SpO2']
    temp = health_data['Temp']

    if bpm > 0:
        if bpm > 120:
            alerts.append(f"heart rate is very high at {bpm:.0f} beats per minute")
        elif bpm < 50:
            alerts.append(f"heart rate is very low at {bpm:.0f} beats per minute")
    if spo2 > 0:
        if spo2 < 90:
            alerts.append(f"oxygen level is critically low at {spo2:.0f} percent")
        elif spo2 < 95:
            alerts.append(f"oxygen level is slightly low at {spo2:.0f} percent")
    if temp > 0:
        if temp > 38.5:
            alerts.append(f"body temperature indicates fever at {temp:.1f} degrees")
        elif temp < 35.0:
            alerts.append(f"body temperature is dangerously low at {temp:.1f} degrees")
    return alerts

# ?????????????????????????????????????????????
#  NAME EXTRACTION
# ?????????????????????????????????????????????

def extract_user_name(text):
    patterns = [
        r"(?:my name is|i am|i'm|call me) ([a-z]+)",
        r"([a-z]+) is my name",
        r"(?:???? ???|???) ([^\s]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1).capitalize()
    return None

# ?????????????????????????????????????????????
#  GET RESPONSE
# ?????????????????????????????????????????????

def get_response(key, **kwargs):
    val = responses.get(key, "Sorry, an error occurred.")
    if isinstance(val, list):
        val = random.choice(val)
    try:
        return val.format(**kwargs)
    except KeyError:
        return val

# ?????????????????????????????????????????????
#  PROCESS COMMAND
# ?????????????????????????????????????????????

def process_command(command):
    if command is None:
        speak(get_response("no_command"))
        return True
print(f"Processing: '{command}'")
    cmd_type, confidence = match_command(command)
    print(f"Matched: {cmd_type} ({confidence})")

    response_text = ""

    # ?? Health Check ??
    if cmd_type == "health_check":
        if not health_data['valid']:
            response_text = get_response("health_no_data")
        else:
            alerts = check_health_alerts()
            if alerts:
                response_text = get_response("health_alert", alerts=", and ".join(alerts))
            else:
                response_text = get_response(
                    "health_normal",
                    bpm  = f"{health_data['BPM']:.0f}",
                    spo2 = f"{health_data['SpO2']:.0f}",
                    temp = f"{health_data['Temp']:.1f}"
                )

    # ?? Emergency ??
    elif cmd_type == "emergency":
        response_text = get_response("emergency")

    # ?? Greeting ??
    elif cmd_type == "greeting":
        name = conversation_state["user_name"]
        response_text = (f"Hello, {name}! How can I help you?"
                         if name else get_response("greeting"))

    # ?? Name ??
    elif cmd_type == "name":
        response_text = get_response("name")

    # ?? Capabilities ??
    elif cmd_type == "capabilities":
        response_text = get_response("capabilities")

    # ?? Time ??
    elif cmd_type == "time":
        response_text = get_response("time", current_time=time.strftime("%I:%M %p"))

    # ?? Thanks ??
    elif cmd_type == "thanks":
        response_text = get_response("thanks")

    # ?? User Name ??
    elif cmd_type == "user_name":
        name = extract_user_name(command)
        if name:
            conversation_state["user_name"] = name
            response_text = get_response("user_name_confirm", name=name)
        else:
            response_text = "I did not catch your name. Could you say it again?"

    # ?? Exit ??
    elif cmd_type == "exit":
        name = conversation_state.get("user_name")
        response_text = (f"Goodbye, {name}! Stay healthy."
                         if name else get_response("exit"))
        speak(response_text)
        return False

    # ?? Unknown ? Gemini ??
    else:
        print("Querying Gemini...")
        if health_data['valid']:
            prompt = (
                f"{command}. "
                f"(Patient vitals - BPM: {health_data['BPM']:.0f}, "
                f"SpO2: {health_data['SpO2']:.0f}%, "
                f"Temp: {health_data['Temp']:.1f}°C)"
            )
        else:
            prompt = command
        response_text = get_gemini_response(prompt)

    speak(response_text)

    context_memory.append({
        "timestamp"   : time.time(),
        "command"     : command,
        "command_type": cmd_type,
        "confidence"  : confidence,
        "response"    : response_text,
    })

    return True

# ?????????????????????????????????????????????
#  BACKGROUND HEALTH ALERT MONITOR
# ?????????????????????????????????????????????

last_alert_time = 0
ALERT_COOLDOWN  = 30

def health_alert_monitor():
    global last_alert_time
    while True:
        try:
            if health_data['valid']:
                alerts = check_health_alerts()
                if alerts and time.time() - last_alert_time > ALERT_COOLDOWN:
                    last_alert_time = time.time()
                    speak(f"Automatic alert! {', and '.join(alerts)}. Please seek medical attention.")
            time.sleep(10)
        except Exception as e:
            print(f"Alert monitor error: {e}")
            time.sleep(5)

# ?????????????????????????????????????????????
#  MAIN
# ?????????????????????????????????????????????

def main():
    print("=" * 50)
    print("  MediAssist Starting...")
    print("=" * 50)

    # Connect Arduino
    connect_arduino()

    # Background threads
    threading.Thread(target=read_health_data_thread, daemon=True).start()
    threading.Thread(target=health_alert_monitor,    daemon=True).start()

    # Init Gemini
    init_gemini()

    # Startup
    speak(get_response("activated"))
# Wake word
    if WAKE_WORD_ENABLED:
        speak(f"Say '{WAKE_WORDS[0]}' to activate me.")
        listen_for_wake_word()
        speak(get_response("wake_listening"))

    time.sleep(0.5)
    speak(get_response("initial"))

    running       = True
    last_activity = time.time()
    idle_spoken   = 0

    while running:
        try:
            now = time.time()
            if now - last_activity > 30 and idle_spoken == 0:
                speak("I am still here if you need anything.")
                idle_spoken = 1
            elif now - last_activity > 60 and idle_spoken == 1:
                speak("Just call my name if you need help.")
                idle_spoken = 2

            audio   = listen_for_audio(timeout=5, phrase_time_limit=PHRASE_TIME_LIMIT,
                                        adjust_noise=False)
            command = recognize_speech(audio)

            if command is not None:
                last_activity = time.time()
                idle_spoken   = 0
                running       = process_command(command)

            time.sleep(0.1)

        except KeyboardInterrupt:
            speak("Shutting down.")
            running = False
        except Exception as e:
            print(f"Main loop error: {e}")
            time.sleep(1)

    speak(get_response("shutdown"))

    if arduino_health and arduino_health.is_open:
        arduino_health.close()

    print("MediAssist shut down.")

if name == "__main__":
    main()
