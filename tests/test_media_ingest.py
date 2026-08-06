from email import policy
from email.parser import BytesParser
from pathlib import Path

import pytest

from hermes_cloud.core.db import open_database
from hermes_cloud.core.events import EventStore
from hermes_cloud.ingest.media import MediaEnvelope, MediaIngress, MediaRejected


class FakeDescriber:
    def describe(self, content: bytes, mime_type: str) -> str:
        assert content == b"synthetic-photo" and mime_type == "image/jpeg"
        return "Elternabend am 12. September um 18 Uhr."


class FakeTranscriber:
    def transcribe(self, content: bytes, mime_type: str) -> str:
        assert content == b"synthetic-voice" and mime_type == "audio/ogg"
        return "Bitte den Ausflug bis Freitag bestätigen."


def envelope(content: bytes, mime: str, external_id: str) -> MediaEnvelope:
    return MediaEnvelope(
        external_id=external_id,
        context_key="telegram:family",
        sender="Parent <parent@example.test>",
        content=content,
        mime_type=mime,
    )


def test_photo_and_voice_become_canonical_durable_events(tmp_path: Path) -> None:
    with open_database(tmp_path / "media.db") as database:
        ingress = MediaIngress(EventStore(database))
        photo = ingress.photo(envelope(b"synthetic-photo", "image/jpeg", "p-1"), FakeDescriber())
        voice = ingress.voice(envelope(b"synthetic-voice", "audio/ogg", "v-1"), FakeTranscriber())

        assert photo.event.source == "photo" and voice.event.source == "voice"
        assert BytesParser(policy=policy.default).parsebytes(photo.event.raw).get_content().startswith(
            "Elternabend"
        )
        assert "Ausflug" in BytesParser(policy=policy.default).parsebytes(voice.event.raw).get_content()
        assert ingress.photo(
            envelope(b"synthetic-photo", "image/jpeg", "p-1"), FakeDescriber()
        ).created is False


def test_media_type_and_size_are_fail_closed(tmp_path: Path) -> None:
    with open_database(tmp_path / "media.db") as database:
        ingress = MediaIngress(EventStore(database))
        with pytest.raises(MediaRejected):
            ingress.photo(envelope(b"script", "text/html", "bad"), FakeDescriber())
        with pytest.raises(MediaRejected):
            ingress.voice(envelope(b"", "audio/ogg", "empty"), FakeTranscriber())
