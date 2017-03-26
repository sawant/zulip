# -*- coding: utf-8 -*-
from __future__ import absolute_import

import logging
import sys

from django.conf import settings
from django.contrib.auth.models import AnonymousUser
from django.http import HttpRequest, HttpResponse
from django.test import RequestFactory
from functools import wraps
from mock import patch
if False:
    from mypy_extensions import NoReturn
from typing import Any, Callable, Dict, Mapping, Optional, Text

from zerver.lib.request import JsonableError
from zerver.lib.test_classes import ZulipTestCase
from zerver.logging_handlers import AdminZulipHandler
from zerver.middleware import JsonErrorHandler
from zerver.views.compatibility import check_compatibility
from zerver.worker.queue_processors import QueueProcessingWorker

captured_request = None  # type: Optional[HttpRequest]
captured_exc_info = None
def capture_and_throw(domain=None):
    # type: (Optional[Text]) -> Callable
    def wrapper(view_func):
        # type: (Callable[..., HttpResponse]) -> Callable[..., HttpResponse]
        @wraps(view_func)
        def wrapped_view(request, *args, **kwargs):
            # type: (HttpRequest, *Any, **Any) -> NoReturn
            global captured_request
            captured_request = request
            try:
                raise Exception("Request error")
            except Exception as e:
                global captured_exc_info
                captured_exc_info = sys.exc_info()
                raise e
        return wrapped_view
    return wrapper

class AdminZulipHandlerTest(ZulipTestCase):
    logger = logging.getLogger('django')

    def setUp(self):
        # type: () -> None
        self.handler = AdminZulipHandler()
        # Prevent the exceptions we're going to raise from being printed
        # You may want to disable this when debugging tests
        settings.LOGGING_NOT_DISABLED = False

    def tearDown(self):
        # type: () -> None
        settings.LOGGING_NOT_DISABLED = True

    def get_admin_zulip_handler(self, logger):
        # type: (logging.Logger) -> Any

        # Ensure that AdminEmailHandler does not get filtered out
        # even with DEBUG=True.
        admin_email_handler = [
            h for h in logger.handlers
            if h.__class__.__name__ == "AdminZulipHandler"
        ][0]
        return admin_email_handler

    def test_basic(self):
        # type: () -> None
        """A random exception passes happily through AdminZulipHandler"""
        handler = self.get_admin_zulip_handler(self.logger)
        try:
            raise Exception("Testing Error!")
        except Exception:
            exc_info = sys.exc_info()
        record = self.logger.makeRecord('name', logging.ERROR, 'function', 16, 'message', None, exc_info)  # type: ignore # https://github.com/python/typeshed/pull/1100
        handler.emit(record)

    def run_handler(self, record):
        # type: (logging.LogRecord) -> Dict[str, Any]
        with patch('zerver.logging_handlers.queue_json_publish') as patched_publish:
            self.handler.emit(record)
            patched_publish.assert_called_once()
            event = patched_publish.call_args[0][1]
            self.assertIn("report", event)
            return event["report"]

    def test_request(self):
        # type: () -> None
        """A normal request is handled properly"""
        self.login("hamlet@zulip.com")
        with patch("zerver.decorator.rate_limit") as rate_limit_patch:
            rate_limit_patch.side_effect = capture_and_throw
            result = self.client_get("/json/users")
            self.assert_json_error(result, "Internal server error", status_code=500)
            rate_limit_patch.assert_called_once()

            global captured_request
            global captured_exc_info
            record = self.logger.makeRecord('name', logging.ERROR, 'function', 15, 'message', None, captured_exc_info)  # type: ignore # https://github.com/python/typeshed/pull/1100
            record.request = captured_request

            report = self.run_handler(record)
            self.assertIn("user_email", report)
            self.assertIn("message", report)
            self.assertIn("stack_trace", report)