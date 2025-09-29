# app.py — Web wrapper للبوت على Render
import threading
from fastapi import FastAPI
import trading_bot_0_3 as bot

app = FastAPI()

def run_bot():
    # يشغّل اللوب اللانهائي حقك في ثريد
    bot.main()

t = threading.Thread(target=run_bot, daemon=True)
t.start()

@app.get("/")
def ping():
    return {"ok": True}
