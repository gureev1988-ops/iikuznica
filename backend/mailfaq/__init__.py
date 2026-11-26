"""Official correspondence, immediate FAQ publication and quality commands."""

from .api import build_router
from .faq import (
    FaqMatch, MailError, current_faq_notification, find_faq, reconcile_document_revision,
    replace_faq, set_coverage, withdraw_faq,
)
from .service import POLL_KIND, SEND_KIND, MailService, bind_inbound, create_request, send_followup
from .transport import ImapInbox, MailConfig, MailSimulator, SmtpTransport

__all__ = [
    "FaqMatch",
    "ImapInbox",
    "MailConfig",
    "MailError",
    "MailService",
    "MailSimulator",
    "POLL_KIND",
    "SEND_KIND",
    "SmtpTransport",
    "bind_inbound",
    "build_router",
    "create_request",
    "current_faq_notification",
    "find_faq",
    "reconcile_document_revision",
    "replace_faq",
    "send_followup",
    "set_coverage",
    "withdraw_faq",
]
