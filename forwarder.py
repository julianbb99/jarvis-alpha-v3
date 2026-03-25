#!/usr/bin/env python3
"""
JARVIS Gold Signal Forwarder V2
Liest: MS GOLD GROUP -> Postet: Gold VIP Signal
"""
import os, asyncio, logging, time
from telethon import TelegramClient, events
from telethon.sessions import StringSession

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(message)s',
    datefmt='%H:%M:%S'
)
log = logging.getLogger('forwarder')

API_ID    = int(os.environ['TELEGRAM_API_ID'])
API_HASH  = os.environ['TELEGRAM_API_HASH']
SESSION   = "1BJWap1sBu4wJvgb0kYlpNdeBjeq712jUS-UbEHAa7vfvaahcs9CtQq2VamRJR21Zuhfhifa5AAfmEc77r-vU7NfuMZQhUJuu3a9w3Fv89llRy-h3zKUOLlQe9GnUJzVyV9xGh6-mGcvo8euyu6SAEijj6jjWfkv0Dc0UTCOK9Glxtmhv3ytc787Et90bt176CUXIeHrOaHN9HNxeDTNwRtu4x2buYl7DFsE6metTeuqKfU_rFqz4pBnmprdRghDkIo271VNYA6pkxeJfskJVEG2nl245J351Ep0iR5mSFGbm7hROMcsVulXoghAwl4Y-6ehA_u1EXUBCVzQUFaU4sp4Y6syWby0="

SOURCE_ID = -1003682518587   # MS GOLD GROUP
TARGET_ID = -1003772081371   # Gold VIP Signal

async def main():
    log.info("Starte JARVIS Gold Forwarder V2...")

    client = TelegramClient(
        StringSession(SESSION),
        API_ID,
        API_HASH,
        connection_retries=10,
        retry_delay=5,
        auto_reconnect=True,
        flood_sleep_threshold=60
    )

    await client.connect()

    if not await client.is_user_authorized():
        log.error("Session nicht autorisiert!")
        return

    me = await client.get_me()
    log.info(f"Eingeloggt als: {me.first_name} (@{me.username})")

    try:
        await client.send_message(TARGET_ID,
            "🤖 JARVIS Forwarder V2 gestartet\n"
            "📡 Lese: MS GOLD GROUP\n"
            "✅ Alle Signale werden instant weitergeleitet"
        )
        log.info("Startup-Nachricht gesendet")
    except Exception as e:
        log.warning(f"Startup-Nachricht Fehler: {e}")

    @client.on(events.NewMessage(chats=SOURCE_ID))
    async def handler(event):
        msg = event.message
        text = msg.text or ""
        log.info(f"Signal: {text[:120]}")
        try:
            if msg.media:
                await client.send_message(TARGET_ID, message=msg)
                log.info("Mit Medien weitergeleitet")
            elif text.strip():
                forwarded = f"🥇 MS GOLD GROUP:\n\n{text}"
                await client.send_message(TARGET_ID, forwarded)
                log.info("Weitergeleitet")
        except Exception as e:
            log.error(f"Weiterleitung Fehler: {e}")

    log.info("Warte auf Signale...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            break
        except Exception as e:
            log.error(f"Crash: {e} - Neustart in 10s...")
            time.sleep(10)
