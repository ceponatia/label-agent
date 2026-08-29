import pytest

from labelagent.config import Config
from labelagent.ingest import PROCESSED_LABEL, ImapFetcher, IngestError


class StubMailbox:
    def __init__(self, client):
        self.client = client

    def logout(self):
        pass


class GmailClient:
    def __init__(self, create_response=("OK", [b"created"]), store_responses=None):
        self.create_response = create_response
        self.store_responses = list(store_responses or [])
        self.calls: list[tuple] = []

    def create(self, mailbox):
        self.calls.append(("CREATE", mailbox))
        return self.create_response

    def uid(self, command, *args):
        self.calls.append((command, *args))
        if command != "STORE":
            return "OK", [b""]
        if self.store_responses:
            return self.store_responses.pop(0)
        return "OK", [b"stored"]


def config() -> Config:
    return Config(
        data_dir="unused",
        imap_host="imap.gmail.com",
        imap_user="elaine@example.com",
        imap_password="app-password",
        imap_folder="INBOX",
    )


def fetcher(client: GmailClient) -> ImapFetcher:
    return ImapFetcher(config(), mailbox=StubMailbox(client))


def test_finalize_creates_processed_label_then_labels_and_archives_message():
    client = GmailClient()
    f = fetcher(client)

    f.finalize_processed("101")

    assert client.calls == [
        ("CREATE", PROCESSED_LABEL),
        ("STORE", "101", "+X-GM-LABELS", f"({PROCESSED_LABEL})"),
        ("STORE", "101", "-X-GM-LABELS", r"(\Inbox)"),
    ]


def test_processed_label_is_created_only_once_per_fetcher():
    client = GmailClient()
    f = fetcher(client)

    f.finalize_processed("101")
    f.finalize_processed("102")

    assert client.calls.count(("CREATE", PROCESSED_LABEL)) == 1


def test_existing_processed_label_is_not_an_error():
    client = GmailClient(
        create_response=(
            "NO",
            [b"[ALREADYEXISTS] Duplicate folder name label-agent/processed (Failure)"],
        )
    )

    fetcher(client).finalize_processed("101")

    assert client.calls[-1] == ("STORE", "101", "-X-GM-LABELS", r"(\Inbox)")


def test_archive_failure_removes_processed_label_so_next_poll_can_retry():
    client = GmailClient(
        store_responses=[
            ("OK", [b"label applied"]),
            ("NO", [b"archive failed"]),
            ("OK", [b"label removed"]),
        ]
    )

    with pytest.raises(IngestError, match="IMAP STORE failed"):
        fetcher(client).finalize_processed("101")

    assert client.calls[-1] == (
        "STORE",
        "101",
        "-X-GM-LABELS",
        f"({PROCESSED_LABEL})",
    )


def test_archive_and_rollback_failure_reports_both_problems():
    client = GmailClient(
        store_responses=[
            ("OK", [b"label applied"]),
            ("NO", [b"archive failed"]),
            ("NO", [b"rollback failed"]),
        ]
    )

    with pytest.raises(IngestError) as error:
        fetcher(client).finalize_processed("101")

    text = str(error.value)
    assert "could not archive processed message 101" in text
    assert "could not roll back label-agent/processed" in text


def test_create_failure_prevents_message_from_being_marked_processed():
    client = GmailClient(create_response=("NO", [b"permission denied"]))

    with pytest.raises(IngestError, match="IMAP CREATE failed"):
        fetcher(client).finalize_processed("101")

    assert client.calls == [("CREATE", PROCESSED_LABEL)]
