#!/usr/bin/env python3
"""
JARVIS Gold Signal Forwarder
Liest: MS GOLD GROUP → Postet: Gold VIP Signal
"""
import os, asyncio, logging, threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('forwarder')

API_ID    = int(os.environ['TELEGRAM_API_ID'])
API_HASH  = os.environ['TELEGRAM_API_HASH']
SESSION   = os.environ['TELEGRAM_SESSION']

SOURCE_ID = -1003682518587   # MS GOLD GROUP
TARGET_ID = -1003772081371   # Gold VIP Signal

# Health check HTTP server für Render
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'JARVIS Gold Forwarder running')
    def log_message(self, *args):
        pass

def start_health_server():
    port = int(os.environ.get('PORT', 10000))
    server = HTTPServer(('0.0.0.0', port), HealthHandler)
    log.info(f"Health server auf Port {port}")
    server.serve_forever()

async def main():
    # Health server im Hintergrund
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info(f"✅ Eingeloggt als: {me.first_name} (@{me.username})")
    log.info(f"📥 Quelle: MS GOLD GROUP ({SOURCE_ID})")
    log.info(f"📤 Ziel: Gold VIP Signal ({TARGET_ID})")

    try:
        await client.send_message(TARGET_ID,
            "🤖 <b>JARVIS Forwarder gestartet</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📥 Lese: MS GOLD GROUP\n"
            "⚡ Alle Signale werden instant weitergeleitet",
            parse_mode='html')
        log.info("✅ Startup-Nachricht gesendet")
    except Exception as e:
        log.warning(f"Startup-Nachricht Fehler: {e}")

    @client.on(events.NewMessage(chats=SOURCE_ID))
    async def handler(event):
        msg = event.message
        text = msg.text or ''
        log.info(f"📨 Signal: {text[:100]}")
        try:
            if msg.media:
                await client.send_message(TARGET_ID, message=msg)
                log.info("✅ Mit Medien weitergeleitet")
            elif text:
                forwarded = f"🥇 <b>MS GOLD GROUP</b>\n━━━━━━━━━━━━━━━━\n{text}"
                await client.send_message(TARGET_ID, forwarded, parse_mode='html')
                log.info("✅ Weitergeleitet")
        except Exception as e:
            log.error(f"❌ Fehler: {e}")

    log.info("👂 Warte auf Signale...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Crash: {e} — Neustart in 10s")
            import time; time.sleep(10)
