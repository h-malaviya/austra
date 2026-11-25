"""Check due Gmail deliveries independently of the browser session."""

import asyncio
import logging
from datetime import timedelta

import psycopg

from austra.integrations.automatic_replies import dispatch_automatic_reply, plan_automatic_reply
from austra.integrations.email_delivery_recovery import reconcile_email_delivery
from austra.integrations.gmail_replies import (
    REPLY_CHECK_BACKOFF,
    REPLY_CHECK_INTERVAL,
    REPLY_TRACKING_PERIOD,
    capture_tracked_replies,
)
from austra.integrations.workflow_notifications import (
    dispatch_owner_notification,
    enqueue_due_follow_up,
)

logger = logging.getLogger(__name__)
RECENT_DELIVERY = timedelta(minutes=15)
FIRST_DAY = timedelta(days=1)
FIRST_DAY_INTERVAL = timedelta(minutes=5)
OLDER_DELIVERY_INTERVAL = timedelta(minutes=30)
SCAN_INTERVAL_SECONDS = 5


def check_due_gmail_reply(db, root) -> bool:
    """Claim one due job. The next check stays scheduled if this process exits."""
    with db.transaction():
        claimed = db.execute(
            "WITH due AS (SELECT j.id FROM outbox_jobs j "
            "JOIN organizations o ON o.id = j.organization_id AND o.status = 'active' "
            "JOIN departments d ON (d.organization_id, d.id) = (j.organization_id, j.department_id) "
            "AND d.archived_at IS NULL "
            "WHERE j.state = 'delivered' AND j.topic = 'capability.email' "
            "AND j.reply_tracking_until > now() AND j.reply_check_at <= now() "
            "ORDER BY j.reply_check_at, j.id LIMIT 1 FOR UPDATE OF j SKIP LOCKED) "
            "UPDATE outbox_jobs j SET reply_check_at = now() + CASE "
            "WHEN j.reply_tracking_until > now() + %s THEN %s "
            "WHEN j.reply_tracking_until > now() + %s THEN %s ELSE %s END "
            "FROM due WHERE j.id = due.id "
            "RETURNING j.organization_id, j.department_id, j.id, j.reply_check_at",
            (REPLY_TRACKING_PERIOD - RECENT_DELIVERY, REPLY_CHECK_INTERVAL,
             REPLY_TRACKING_PERIOD - FIRST_DAY, FIRST_DAY_INTERVAL, OLDER_DELIVERY_INTERVAL),
        ).fetchone()
    if claimed is None:
        return False
    org, department, job, scheduled_at = claimed
    try:
        capture_tracked_replies(db, root, org, department, job)
    except (LookupError, OSError, RuntimeError, ValueError) as error:
        logger.warning("Gmail reply check deferred for delivery %s (%s)", job, type(error).__name__)
        with db.transaction():
            db.execute(
                "UPDATE outbox_jobs SET reply_check_at = GREATEST(reply_check_at, now() + %s), "
                "reply_check_failed = true "
                "WHERE organization_id = %s AND department_id = %s AND id = %s AND reply_check_at = %s",
                (REPLY_CHECK_BACKOFF, org, department, job, scheduled_at),
            )
    return True


async def run_gmail_reply_worker(connect, root):
    def check():
        with connect() as db:
            reconciled = reconcile_email_delivery(db)
            checked = check_due_gmail_reply(db, root)
            planned = plan_automatic_reply(db)
            responded = dispatch_automatic_reply(db, root)
            reminded = enqueue_due_follow_up(db)
            notified = dispatch_owner_notification(db, root)
            return reconciled or checked or planned or responded or reminded or notified

    while True:
        try:
            has_work = await asyncio.to_thread(check)
        except psycopg.Error as error:
            logger.warning("Gmail reply scheduler will retry (%s)", type(error).__name__)
            has_work = False
        if not has_work:
            await asyncio.sleep(SCAN_INTERVAL_SECONDS)
