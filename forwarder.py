#!/usr/bin/env python3
"""
JARVIS Gold Signal Forwarder
Liest: MS GOLD GROUP -> Postet: Gold VIP Signal
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
    log.info(f"Eingeloggt als: {me.first_name} (@{me.username})")
    log.info(f"Quelle: MS GOLD GROUP ({SOURCE_ID})")
    log.info(f"Ziel: Gold VIP Signal ({TARGET_ID})")

    try:
        await client.send_message(TARGET_ID,
            "JARVIS Forwarder gestartet\n"
            "Lese: MS GOLD GROUP\n"
            "Alle Signale werden instant weitergeleitet")
        log.info("Startup-Nachricht gesendet")
    except Exception as e:
        log.warning(f"Startup-Nachricht Fehler: {e}")

    @client.on(events.NewMessage(chats=SOURCE_ID))
    async def handler(event):
        msg = event.message
        text = msg.text or ""
        log.info(f"Signal empfangen: {text[:100]}")
        try:
            if msg.media:
                await client.send_message(TARGET_ID, message=msg)
                log.info("Mit Medien weitergeleitet")
            elif text:
                forwarded = f"MS GOLD GROUP Signal:\n\n{text}"
                await client.send_message(TARGET_ID, forwarded)
                log.info("Weitergeleitet")
        except Exception as e:
            log.error(f"Fehler: {e}")

    log.info("Warte auf Signale...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Crash: {e} - Neustart in 10s")
            import time; time.sleep(10)
