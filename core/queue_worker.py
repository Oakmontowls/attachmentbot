import asyncio
import aiohttp
from utils.pipeline import process_message_pipeline
from core.logger import log

MESSAGE_QUEUE = asyncio.Queue()

async def start_worker(bot):

	log.info("[QUEUE] Worker started")

	while True:
		message = await MESSAGE_QUEUE.get()

		try:
			await process_message_pipeline(bot, message)

		except Exception as e:
			log.info(f"[QUEUE ERROR] {e}")

		MESSAGE_QUEUE.task_done()