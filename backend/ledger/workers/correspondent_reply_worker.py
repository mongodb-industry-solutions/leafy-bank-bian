"""Correspondent reply worker — answers escalated exceptions after a fixed delay.

Polls every few seconds for exceptions awaiting the correspondent whose camt.026 is at least
`CORRESPONDENT_REPLY_SECONDS` old (default 20) and replies. Disable with
`ENABLE_CORRESPONDENT_REPLY_SIM=false` to leave escalations open (the Showcase button still
answers on demand).
"""

from __future__ import annotations

import logging
import time

from database.connection import MongoDBConnection
from services import correspondent_reply

logger = logging.getLogger(__name__)

POLL_SECONDS = 5


def run(connection: MongoDBConnection, db_name: str, delay_seconds: int) -> None:
    logger.info("correspondent_reply_worker starting — replies %ds after escalation", delay_seconds)
    while True:
        try:
            for exception_id in correspondent_reply.reply_due(
                    connection, db_name, delay_seconds=delay_seconds):
                logger.info("correspondent_reply_worker: answered %s", exception_id)
        except Exception:
            logger.exception("correspondent_reply_worker error")
        time.sleep(POLL_SECONDS)
