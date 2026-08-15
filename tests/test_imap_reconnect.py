import pytest

from labelagent.config import Config
from labelagent.ingest import ImapFetcher


class StubClient:
    def __init__(self, search_result):
        self.search_result = search_result
        self.calls = 0

    def uid(self, command, *args):
        assert command == "SEARCH"
        self.calls += 1
        if isinstance(self.search_result, Exception):
            raise self.search_result
        return self.search_result


class StubMailbox:
    def __init__(self, client, logout_error: Exception | None = None):
        self.client = client
        self.logout_error = logout_error
        self.logged_out = False

    def logout(self):
        self.logged_out = True
        if self.logout_error is not None:
            raise self.logout_error


class RotatingImapFetcher(ImapFetcher):
    """Test fetcher that supplies a new mailbox whenever the cache is cleared."""

    def __init__(self, config: Config, mailboxes: list[StubMailbox]):
        first, *remaining = mailboxes
        super().__init__(config, mailbox=first)
        self.remaining_mailboxes = remaining

    def mailbox(self):
        if self._mailbox is None:
            self._mailbox = self.remaining_mailboxes.pop(0)
        return self._mailbox


def config() -> Config:
    return Config(
        data_dir="unused",
        imap_host="imap.gmail.com",
        imap_user="elaine@example.com",
        imap_password="app-password",
        imap_folder="INBOX",
    )


def test_fetch_reconnects_once_after_a_stale_socket_even_if_logout_fails():
    stale_client = StubClient(
        OSError(10054, "An existing connection was forcibly closed by the remote host")
    )
    fresh_client = StubClient(("OK", [b""]))
    stale_mailbox = StubMailbox(stale_client, logout_error=OSError("socket already dead"))
    fresh_mailbox = StubMailbox(fresh_client)
    fetcher = RotatingImapFetcher(config(), [stale_mailbox, fresh_mailbox])

    assert fetcher.fetch_candidates() == []
    assert stale_mailbox.logged_out is True
    assert stale_client.calls == 1
    assert fresh_client.calls == 1
    assert fetcher._mailbox is fresh_mailbox


def test_fetch_surfaces_the_second_failure_without_retrying_forever():
    first_mailbox = StubMailbox(StubClient(OSError("stale connection")))
    second_mailbox = StubMailbox(StubClient(OSError("still disconnected")))
    unused_third = StubMailbox(StubClient(("OK", [b""])))
    fetcher = RotatingImapFetcher(
        config(), [first_mailbox, second_mailbox, unused_third]
    )

    with pytest.raises(OSError, match="still disconnected"):
        fetcher.fetch_candidates()

    assert first_mailbox.logged_out is True
    assert fetcher._mailbox is second_mailbox
    assert fetcher.remaining_mailboxes == [unused_third]
