from typing import Any, Dict
from unittest import mock

import orjson
import requests

from version import ZULIP_VERSION
from zerver.lib.actions import do_create_user
from zerver.lib.outgoing_webhook import (
    GenericOutgoingWebhookService,
    SlackOutgoingWebhookService,
    do_rest_call,
)
from zerver.lib.test_classes import ZulipTestCase
from zerver.lib.topic import TOPIC_NAME
from zerver.lib.url_encoding import near_message_url
from zerver.lib.users import add_service
from zerver.models import Recipient, Service, UserProfile, get_display_recipient, get_realm


class ResponseMock:
    def __init__(self, status_code: int, content: bytes = b"") -> None:
        self.status_code = status_code
        self.content = content
        self.text = content.decode()


def request_exception_error(final_url: Any, **request_kwargs: Any) -> Any:
    raise requests.exceptions.RequestException("I'm a generic exception :(")


def timeout_error(final_url: Any, **request_kwargs: Any) -> Any:
    raise requests.exceptions.Timeout("Time is up!")


def connection_error(final_url: Any, **request_kwargs: Any) -> Any:
    raise requests.exceptions.ConnectionError()


class DoRestCallTests(ZulipTestCase):
    def mock_event(self, bot_user: UserProfile) -> Dict[str, Any]:
        return {
            # In the tests there is no active queue processor, so retries don't get processed.
            # Therefore, we need to emulate `retry_event` in the last stage when the maximum
            # retries have been exceeded.
            "failed_tries": 3,
            "message": {
                "display_recipient": "Verona",
                "stream_id": 999,
                "sender_id": bot_user.id,
                "sender_email": bot_user.email,
                "sender_realm_id": bot_user.realm.id,
                "sender_realm_str": bot_user.realm.string_id,
                "sender_delivery_email": bot_user.delivery_email,
                "sender_full_name": bot_user.full_name,
                "sender_avatar_source": UserProfile.AVATAR_FROM_GRAVATAR,
                "sender_avatar_version": 1,
                "recipient_type": "stream",
                "recipient_type_id": 999,
                "sender_is_mirror_dummy": False,
                TOPIC_NAME: "Foo",
                "id": "",
                "type": "stream",
                "timestamp": 1,
            },
            "trigger": "mention",
            "user_profile_id": bot_user.id,
            "command": "",
            "service_name": "",
        }

    def test_successful_request(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")

        expect_send_response = mock.patch("zerver.lib.outgoing_webhook.send_response_message")
        with mock.patch.object(
            service_handler, "session"
        ) as session, expect_send_response as mock_send:
            session.post.return_value = ResponseMock(200, orjson.dumps(dict(content="whatever")))
            do_rest_call("", mock_event, service_handler)
        self.assertTrue(mock_send.called)

        for service_class in [GenericOutgoingWebhookService, SlackOutgoingWebhookService]:
            handler = service_class("token", bot_user, "service")
            with mock.patch.object(handler, "session") as session:
                session.post.return_value = ResponseMock(200)
                do_rest_call("", mock_event, handler)
                session.post.assert_called_once()

    def test_retry_request(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")

        with mock.patch.object(service_handler, "session") as session, self.assertLogs(
            level="WARNING"
        ) as m:
            session.post.return_value = ResponseMock(500)
            final_response = do_rest_call("", mock_event, service_handler)
            assert final_response is not None

            self.assertEqual(
                m.output,
                [
                    f'WARNING:root:Message http://zulip.testserver/#narrow/stream/999-Verona/topic/Foo/near/ triggered an outgoing webhook, returning status code 500.\n Content of response (in quotes): "{final_response.text}"'
                ],
            )
        bot_owner_notification = self.get_last_message()
        self.assertEqual(
            bot_owner_notification.content,
            """[A message](http://zulip.testserver/#narrow/stream/999-Verona/topic/Foo/near/) to your bot @_**Outgoing Webhook** triggered an outgoing webhook.
The webhook got a response with status code *500*.""",
        )

        assert bot_user.bot_owner is not None
        self.assertEqual(bot_owner_notification.recipient_id, bot_user.bot_owner.recipient_id)

    def test_fail_request(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")

        expect_fail = mock.patch("zerver.lib.outgoing_webhook.fail_with_message")

        with mock.patch.object(
            service_handler, "session"
        ) as session, expect_fail as mock_fail, self.assertLogs(level="WARNING") as m:
            session.post.return_value = ResponseMock(400)
            final_response = do_rest_call("", mock_event, service_handler)
            assert final_response is not None

            self.assertEqual(
                m.output,
                [
                    f'WARNING:root:Message http://zulip.testserver/#narrow/stream/999-Verona/topic/Foo/near/ triggered an outgoing webhook, returning status code 400.\n Content of response (in quotes): "{final_response.text}"'
                ],
            )

        self.assertTrue(mock_fail.called)

        bot_owner_notification = self.get_last_message()
        self.assertEqual(
            bot_owner_notification.content,
            """[A message](http://zulip.testserver/#narrow/stream/999-Verona/topic/Foo/near/) to your bot @_**Outgoing Webhook** triggered an outgoing webhook.
The webhook got a response with status code *400*.""",
        )

        assert bot_user.bot_owner is not None
        self.assertEqual(bot_owner_notification.recipient_id, bot_user.bot_owner.recipient_id)

    def test_headers(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")

        session = service_handler.session
        with mock.patch.object(session, "send") as mock_send:
            mock_send.return_value = ResponseMock(200)
            final_response = do_rest_call("https://example.com/", mock_event, service_handler)
            assert final_response is not None

            mock_send.assert_called_once()
            prepared_request = mock_send.call_args[0][0]
            user_agent = "ZulipOutgoingWebhook/" + ZULIP_VERSION
            headers = {
                "Content-Type": "application/json",
                "User-Agent": user_agent,
                "X-Smokescreen-Role": "webhook",
            }
            self.assertLessEqual(headers.items(), prepared_request.headers.items())

    def test_error_handling(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")
        bot_user_email = self.example_user_map["outgoing_webhook_bot"]

        def helper(side_effect: Any, error_text: str) -> None:
            with mock.patch.object(service_handler, "session") as session:
                session.post.side_effect = side_effect
                do_rest_call("", mock_event, service_handler)

            bot_owner_notification = self.get_last_message()
            self.assertIn(error_text, bot_owner_notification.content)
            self.assertIn("triggered", bot_owner_notification.content)
            assert bot_user.bot_owner is not None
            self.assertEqual(bot_owner_notification.recipient_id, bot_user.bot_owner.recipient_id)

        with self.assertLogs(level="INFO") as i:
            helper(side_effect=timeout_error, error_text="A timeout occurred.")
            helper(side_effect=connection_error, error_text="A connection error occurred.")

            log_output = [
                f"INFO:root:Trigger event {mock_event['command']} on {mock_event['service_name']} timed out. Retrying",
                f"WARNING:root:Maximum retries exceeded for trigger:{bot_user_email} event:{mock_event['command']}",
                f"INFO:root:Trigger event {mock_event['command']} on {mock_event['service_name']} resulted in a connection error. Retrying",
                f"WARNING:root:Maximum retries exceeded for trigger:{bot_user_email} event:{mock_event['command']}",
            ]

            self.assertEqual(i.output, log_output)

    def test_request_exception(self) -> None:
        bot_user = self.example_user("outgoing_webhook_bot")
        mock_event = self.mock_event(bot_user)
        service_handler = GenericOutgoingWebhookService("token", bot_user, "service")

        expect_logging_exception = self.assertLogs(level="ERROR")
        expect_fail = mock.patch("zerver.lib.outgoing_webhook.fail_with_message")

        # Don't think that we should catch and assert whole log output(which is actually a very big error traceback).
        # We are already asserting bot_owner_notification.content which verifies exception did occur.
        with mock.patch.object(
            service_handler, "session"
        ) as session, expect_logging_exception, expect_fail as mock_fail:
            session.post.side_effect = request_exception_error
            do_rest_call("", mock_event, service_handler)

        self.assertTrue(mock_fail.called)

        bot_owner_notification = self.get_last_message()
        self.assertEqual(
            bot_owner_notification.content,
            """[A message](http://zulip.testserver/#narrow/stream/999-Verona/topic/Foo/near/) to your bot @_**Outgoing Webhook** triggered an outgoing webhook.
When trying to send a request to the webhook service, an exception of type RequestException occurred:
```
I'm a generic exception :(
```""",
        )
        assert bot_user.bot_owner is not None
        self.assertEqual(bot_owner_notification.recipient_id, bot_user.bot_owner.recipient_id)


class TestOutgoingWebhookMessaging(ZulipTestCase):
    def create_outgoing_bot(self, bot_owner: UserProfile) -> UserProfile:
        return self.create_test_bot(
            "outgoing-webhook",
            bot_owner,
            full_name="Outgoing Webhook bot",
            bot_type=UserProfile.OUTGOING_WEBHOOK_BOT,
            service_name="foo-service",
        )

    def test_multiple_services(self) -> None:
        bot_owner = self.example_user("othello")

        bot = do_create_user(
            bot_owner=bot_owner,
            bot_type=UserProfile.OUTGOING_WEBHOOK_BOT,
            full_name="Outgoing Webhook Bot",
            email="whatever",
            realm=bot_owner.realm,
            password=None,
            acting_user=None,
        )

        add_service(
            "weather",
            user_profile=bot,
            interface=Service.GENERIC,
            base_url="weather_url",
            token="weather_token",
        )

        add_service(
            "qotd",
            user_profile=bot,
            interface=Service.GENERIC,
            base_url="qotd_url",
            token="qotd_token",
        )

        sender = self.example_user("hamlet")

        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = ResponseMock(200)
        with mock.patch("zerver.lib.outgoing_webhook.Session") as sessionmaker:
            sessionmaker.return_value = session
            self.send_personal_message(
                sender,
                bot,
                content="some content",
            )

        url_token_tups = set()
        session.post.assert_called()
        for item in session.post.call_args_list:
            args = item[0]
            base_url = args[0]
            kwargs = item[1]
            request_data = kwargs["json"]
            tup = (base_url, request_data["token"])
            url_token_tups.add(tup)
            message_data = request_data["message"]
            self.assertEqual(message_data["content"], "some content")
            self.assertEqual(message_data["sender_id"], sender.id)

        self.assertEqual(
            url_token_tups,
            {
                ("weather_url", "weather_token"),
                ("qotd_url", "qotd_token"),
            },
        )

    def test_pm_to_outgoing_webhook_bot(self) -> None:
        bot_owner = self.example_user("othello")
        bot = self.create_outgoing_bot(bot_owner)
        sender = self.example_user("hamlet")

        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = ResponseMock(
            200, orjson.dumps({"response_string": "Hidley ho, I'm a webhook responding!"})
        )
        with mock.patch("zerver.lib.outgoing_webhook.Session") as sessionmaker:
            sessionmaker.return_value = session
            self.send_personal_message(sender, bot, content="foo")
        last_message = self.get_last_message()
        self.assertEqual(last_message.content, "Hidley ho, I'm a webhook responding!")
        self.assertEqual(last_message.sender_id, bot.id)
        self.assertEqual(
            last_message.recipient.type_id,
            sender.id,
        )
        self.assertEqual(
            last_message.recipient.type,
            Recipient.PERSONAL,
        )

    def test_pm_to_outgoing_webhook_bot_for_407_error_code(self) -> None:
        bot_owner = self.example_user("othello")
        bot = self.create_outgoing_bot(bot_owner)
        sender = self.example_user("hamlet")
        realm = get_realm("zulip")

        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = ResponseMock(407)
        expect_fail = mock.patch("zerver.lib.outgoing_webhook.fail_with_message")

        with mock.patch(
            "zerver.lib.outgoing_webhook.Session"
        ) as sessionmaker, expect_fail as mock_fail, self.assertLogs(level="WARNING"):
            sessionmaker.return_value = session
            message_id = self.send_personal_message(sender, bot, content="foo")

            # create message dict to get the message url
            message = {
                "display_recipient": [{"id": bot.id}, {"id": sender.id}],
                "stream_id": 999,
                TOPIC_NAME: "Foo",
                "id": message_id,
                "type": "",
            }
            message_url = near_message_url(realm, message)

            last_message = self.get_last_message()
            self.assertEqual(
                last_message.content,
                f"[A message]({message_url}) to your bot @_**{bot.full_name}** triggered an outgoing webhook.\n"
                "The URL configured for the webhook is for a private or disallowed network.",
            )
            self.assertEqual(last_message.sender_id, bot.id)
            self.assertEqual(
                last_message.recipient.type_id,
                bot_owner.id,
            )
            self.assertEqual(
                last_message.recipient.type,
                Recipient.PERSONAL,
            )
            self.assertTrue(mock_fail.called)

    def test_stream_message_to_outgoing_webhook_bot(self) -> None:
        bot_owner = self.example_user("othello")
        bot = self.create_outgoing_bot(bot_owner)

        session = mock.Mock(spec=requests.Session)
        session.headers = {}
        session.post.return_value = ResponseMock(
            200, orjson.dumps({"response_string": "Hidley ho, I'm a webhook responding!"})
        )
        with mock.patch("zerver.lib.outgoing_webhook.Session") as sessionmaker:
            sessionmaker.return_value = session
            self.send_stream_message(
                bot_owner, "Denmark", content=f"@**{bot.full_name}** foo", topic_name="bar"
            )
        last_message = self.get_last_message()
        self.assertEqual(last_message.content, "Hidley ho, I'm a webhook responding!")
        self.assertEqual(last_message.sender_id, bot.id)
        self.assertEqual(last_message.topic_name(), "bar")
        display_recipient = get_display_recipient(last_message.recipient)
        self.assertEqual(display_recipient, "Denmark")
