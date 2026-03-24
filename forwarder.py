#!/usr/bin/env python3
"""
JARVIS Gold Signal Forwarder
Liest: MS GOLD GROUP → Postet: Gold VIP Signal
"""
import os, asyncio, logging
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(message)s', datefmt='%H:%M:%S')
log = logging.getLogger('forwarder')

API_ID    = int(os.environ['TELEGRAM_API_ID'])
API_HASH  = os.environ['TELEGRAM_API_HASH']
SESSION   = os.environ['TELEGRAM_SESSION']

SOURCE_ID = -1003682518587   # MS GOLD GROUP
TARGET_ID = -1003772081371   # Gold VIP Signal

async def main():
    client = TelegramClient(StringSession(SESSION), API_ID, API_HASH)
    await client.start()

    me = await client.get_me()
    log.info(f"✅ Eingeloggt als: {me.first_name} (@{me.username})")
    log.info(f"📥 Quelle: MS GOLD GROUP")
    log.info(f"📤 Ziel: Gold VIP Signal")

    # Startup Nachricht in Zielgruppe
    try:
        await client.send_message(TARGET_ID,
            "🤖 <b>JARVIS Forwarder gestartet</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "📥 Lese: MS GOLD GROUP\n"
            "⚡ Alle Signale werden instant weitergeleitet",
            parse_mode='html')
    except Exception as e:
        log.warning(f"Startup-Nachricht: {e}")

    @client.on(events.NewMessage(chats=SOURCE_ID))
    async def handler(event):
        msg = event.message
        text = msg.text or ''
        log.info(f"📨 Signal: {text[:100]}")

        try:
            if msg.media:
                # Mit Bild/Medien weiterleiten
                await client.send_message(TARGET_ID, message=msg)
                log.info("✅ Mit Medien weitergeleitet")
            elif text:
                forwarded = f"🥇 <b>MS GOLD GROUP</b>\n━━━━━━━━━━━━━━━━\n{text}"
                await client.send_message(TARGET_ID, forwarded, parse_mode='html')
                log.info("✅ Text weitergeleitet")
        except Exception as e:
            log.error(f"❌ Fehler beim Weiterleiten: {e}")

    log.info("👂 Warte auf Signale aus MS GOLD GROUP...")
    await client.run_until_disconnected()

if __name__ == '__main__':
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Fehler: {e}")
            asyncio.run(asyncio.sleep(10))
