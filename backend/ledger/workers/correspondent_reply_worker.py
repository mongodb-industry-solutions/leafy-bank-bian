"""Correspondent reply worker — the simulated correspondent's side of an investigation.

Polls every few seconds and, per escalated exception:
1. answers with a camt.111 once our camt.110 is `CORRESPONDENT_REPLY_SECONDS` old (default
   20, simulated pacing — not a SWIFT response time);
2. re-checks once the correspondent's corrected camt.053 has arrived (the transactions
   statement worker books it), which is what closes the exception.

Disable with `ENABLE_CORRESPONDENT_REPLY_SIM=false` to leave escalations open (the Showcase
buttons still drive both steps on demand).
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
            for exception_id in correspondent_reply.apply_due(connection, db_name):
                logger.info("correspondent_reply_worker: corrected statement closed %s",
                            exception_id)
        except Exception:
            logger.exception("correspondent_reply_worker error")
        time.sleep(POLL_SECONDS)
