import speech_recognition as sr
import requests
import pyttsx3

PI_URL = "http://192.168.133.41:5000/voice"

engine = pyttsx3.init()
r = sr.Recognizer()

while True:
    with sr.Microphone() as source:
        print("🎤 Speak something...")
        audio = r.listen(source)

    try:
        text = r.recognize_google(audio)
        print("You said:", text)

        # 👉 Pi তে পাঠানো
        res = requests.post(PI_URL, json={"text": text})
        reply = res.json()["response"]

        print("Pi says:", reply)

        # 👉 Laptop speaker এ শোনা
        engine.say(reply)
        engine.runAndWait()

    except:
        print("❌ Could not understand")
