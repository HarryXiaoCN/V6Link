"""Full loopback IPv6 test: friend request, encrypted messages, files."""

from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

from v6link.crypto import EncryptedSession
from v6link.engine import ChatEngine, _valid_announced_hosts
from v6link.filestore import cleanup_temp_file, iter_plaintext
from v6link.vault import Vault


def _ipv6_loopback_available() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


IPV6_LOOPBACK = _ipv6_loopback_available()


class EventCollector:
    def __init__(self):
        self.events: list[dict[str, Any]] = []
        self._new = asyncio.Event()
        self._consumed: set[int] = set()

    def __call__(self, payload: dict[str, Any]) -> None:
        self.events.append(payload)
        self._new.set()

    async def wait(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            for index, event in enumerate(self.events):
                if index in self._consumed:
                    continue
                if predicate(event):
                    self._consumed.add(index)
                    return event
            remaining = deadline - loop.time()
            if remaining <= 0:
                recent = [event.get("type") for event in self.events[-15:]]
                raise AssertionError(f"等待事件超时；最近事件：{recent}")
            self._new.clear()
            try:
                await asyncio.wait_for(self._new.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass


def _message_ready(message: dict[str, Any]) -> bool:
    return message.get("kind") == "file" and message.get("status") == "ready"


@unittest.skipUnless(IPV6_LOOPBACK, "IPv6 loopback is unavailable on this runner")
class IPv6EndToEndTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dirs: list[tempfile.TemporaryDirectory] = []
        self._engines: list[ChatEngine] = []
        self._files: list[Path] = []

    async def asyncTearDown(self):
        for engine in reversed(self._engines):
            try:
                await engine.stop()
            except Exception:
                pass
        for path in self._files:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        for directory in self._dirs:
            directory.cleanup()

    async def _new_engine(self, name: str):
        directory = tempfile.TemporaryDirectory(prefix=f"v6link-{name}-")
        self._dirs.append(directory)
        vault = Vault(directory.name, display_name=name, listen_port=0)
        events = EventCollector()
        engine = ChatEngine(
            vault,
            events,
            listen_port=0,
            downloads_dir=Path(directory.name) / "downloads",
        )
        await engine.start()
        self._engines.append(engine)
        return engine, events
        
    async def test_friend_request_text_and_parallel_files(self):
        alice, alice_events = await self._new_engine("Alice")
        bob, bob_events = await self._new_engine("Bob")
        self.assertNotEqual(alice.port, bob.port)

        # Reserve then release a loopback port so the first saved address is
        # certainly dead. Alice must fail over to the second address.
        dead_socket = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        dead_socket.bind(("::1", 0))
        dead_port = dead_socket.getsockname()[1]
        dead_socket.close()
        alice_contact = await alice.add_contact(
            "Bob",
            addresses=[
                {"host": "::1", "port": dead_port, "label": "stale address"},
                {"host": "::1", "port": bob.port, "label": "loopback"},
            ],
        )
        first_message_id = await alice.send_text(alice_contact["id"], "你好 Bob，这是加密消息")

        request_event = await bob_events.wait(lambda e: e.get("type") == "contact_request", timeout=20)
        self.assertEqual(request_event["request"]["peer_id"], alice.identity.peer_id)
        bob_contact = await bob.accept_pending_request(request_event["request"]["id"])

        accepted = await alice_events.wait(lambda e: e.get("type") == "friend_accepted", timeout=20)
        self.assertEqual(accepted["contact"]["peer_id"], bob.identity.peer_id)

        delivered = await bob_events.wait(
            lambda e: e.get("type") == "message_new"
            and e["message"].get("text") == "你好 Bob，这是加密消息",
            timeout=25,
        )
        self.assertEqual(delivered["message"]["direction"], "in")
        sent = await alice_events.wait(
            lambda e: e.get("type") == "message_updated"
            and e["message"]["id"] == first_message_id
            and e["message"]["status"] == "sent",
            timeout=20,
        )
        self.assertEqual(sent["message"]["status"], "sent")

        # Reply in the other direction.
        await bob.send_text(bob_contact["id"], "收到，传输已加密")
        reply = await alice_events.wait(
            lambda e: e.get("type") == "message_new"
            and e["message"].get("text") == "收到，传输已加密",
            timeout=20,
        )
        self.assertEqual(reply["message"]["direction"], "in")

        # Parallel encrypted file upload, large enough to span many chunks.
        with tempfile.NamedTemporaryFile(prefix="v6link-file-", delete=False) as handle:
            payload = (b"V6LINK_FILE_PAYLOAD_MARKER_7d21" + os.urandom(8192)) * 520
            handle.write(payload)
            source = Path(handle.name)
        self._files.append(source)
        self.assertGreater(len(payload), 4 * 1024 * 1024)
        start = asyncio.get_running_loop().time()
        file_message_id = await alice.send_file(alice_contact["id"], source)
        offer = await bob_events.wait(
            lambda e: e.get("type") == "file_offer_received",
            timeout=30,
        )
        await bob.respond_file_offer(offer["offer"]["file_id"], True)
        incoming = await bob_events.wait(
            lambda e: e.get("type") == "message_new"
            and e["message"].get("kind") == "file",
            timeout=30,
        )
        self.assertEqual(incoming["message"]["status"], "receiving")
        ready_event = await bob_events.wait(
            lambda e: e.get("type") == "message_updated" and _message_ready(e["message"]),
            timeout=60,
        )
        auto_saved = await bob_events.wait(
            lambda e: e.get("type") == "file_auto_saved",
            timeout=30,
        )
        saved_path = Path(auto_saved["path"])
        self.assertTrue(saved_path.is_file())
        self.assertEqual(saved_path.read_bytes(), payload)
        refreshed = bob.vault.get_message(auto_saved["message_id"])
        self.assertEqual(Path(refreshed["file"]["export_path"]).resolve(), saved_path.resolve())
        elapsed = asyncio.get_running_loop().time() - start
        file_message = ready_event["message"]
        self.assertEqual(file_message["file"]["size"], len(payload))
        meta = file_message["file"]
        plaintext = await asyncio.to_thread(
            lambda: b"".join(iter_plaintext(bob.vault.data_dir, meta))
        )
        self.assertEqual(hashlib.sha256(plaintext).digest(), hashlib.sha256(payload).digest())
        self.assertGreater(ready_event["message"]["file"]["size"], 0)
        sender_done = await alice_events.wait(
            lambda e: e.get("type") == "message_updated"
            and e["message"]["id"] == file_message_id
            and e["message"]["status"] == "sent",
            timeout=30,
        )
        self.assertEqual(sender_done["message"]["kind"], "file")

        # The at-rest attachment must not expose the payload marker.
        container = (bob.vault.data_dir / meta["enc_path"]).read_bytes()
        self.assertNotIn(b"V6LINK_FILE_PAYLOAD_MARKER_7d21", container)

        # Decrypt-to-temp and export APIs both produce authenticated bytes.
        opened = await bob.open_attachment(file_message["id"])
        try:
            self.assertEqual(Path(opened).read_bytes(), payload)
        finally:
            cleanup_temp_file(Path(opened))
        export_target = Path(tempfile.mkdtemp(prefix="v6link-export-")) / "export.bin"
        self._files.append(export_target)
        await bob.export_attachment(file_message["id"], export_target)
        self.assertEqual(export_target.read_bytes(), payload)
        exported = bob.vault.get_message(file_message["id"])
        self.assertEqual(
            Path(exported["file"]["export_path"]).resolve(),
            export_target.resolve(),
        )

        # Return path with a smaller file.
        with tempfile.NamedTemporaryFile(prefix="v6link-reply-", delete=False) as handle:
            reply_payload = os.urandom(2 * 1024 * 1024 + 333)
            handle.write(reply_payload)
            reply_source = Path(handle.name)
        self._files.append(reply_source)
        await bob.send_file(bob_contact["id"], reply_source)
        back_offer = await alice_events.wait(
            lambda e: e.get("type") == "file_offer_received",
            timeout=30,
        )
        await alice.respond_file_offer(back_offer["offer"]["file_id"], True)
        back_ready = await alice_events.wait(
            lambda e: e.get("type") == "message_updated" and _message_ready(e["message"]),
            timeout=60,
        )
        back_plain = await asyncio.to_thread(
            lambda: b"".join(iter_plaintext(alice.vault.data_dir, back_ready["message"]["file"]))
        )
        self.assertEqual(hashlib.sha256(back_plain).digest(), hashlib.sha256(reply_payload).digest())

        # Identity pinning and auto-learned addresses are persisted.
        alice_bob = alice.vault.find_contact_by_peer_id(bob.identity.peer_id)
        bob_alice = bob.vault.find_contact_by_peer_id(alice.identity.peer_id)
        self.assertIsNotNone(alice_bob)
        self.assertIsNotNone(bob_alice)
        self.assertTrue(alice_bob["verified"] is False)
        self.assertTrue(
            any(address["host"] == "::1" and address["port"] == bob.port for address in alice_bob["addresses"])
        )
        self.assertTrue(
            any(address["host"] == "::1" and address["port"] == alice.port for address in bob_alice["addresses"])
        )
        self.assertGreater(elapsed, 0)

    async def _new_pair(self):
        alice, alice_events = await self._new_engine("Alice")
        bob, bob_events = await self._new_engine("Bob")
        alice_contact = await alice.add_contact(
            "Bob",
            peer_id=bob.identity.peer_id,
            addresses=[{"host": "::1", "port": bob.port}],
        )
        bob_contact = await bob.add_contact(
            "Alice",
            peer_id=alice.identity.peer_id,
            addresses=[{"host": "::1", "port": alice.port}],
        )
        return alice, alice_events, alice_contact, bob, bob_events, bob_contact

    async def test_file_offer_requires_explicit_receiver_consent(self):
        alice, alice_events, alice_contact, bob, bob_events, bob_contact = await self._new_pair()
        with tempfile.NamedTemporaryFile(prefix="v6link-reject-", delete=False) as handle:
            handle.write(os.urandom(2 * 1024 * 1024))
            source = Path(handle.name)
        self._files.append(source)
        message_id = await alice.send_file(alice_contact["id"], source)
        offer = await bob_events.wait(
            lambda event: event.get("type") == "file_offer_received",
            timeout=30,
        )
        self.assertEqual(bob.vault.list_messages(bob_contact["id"]), [])
        await bob.respond_file_offer(offer["offer"]["file_id"], False)
        declined = await alice_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "declined",
            timeout=30,
        )
        self.assertIn("拒绝", declined["message"].get("error", ""))
        self.assertEqual(bob.vault.list_messages(bob_contact["id"]), [])
        leftovers = list((bob.vault.data_dir / "attachments").glob("*.v6f"))
        self.assertEqual(leftovers, [])

    async def test_sender_cancel_during_consent_deletes_no_receiver_file(self):
        alice, alice_events, alice_contact, bob, bob_events, bob_contact = await self._new_pair()
        with tempfile.NamedTemporaryFile(prefix="v6link-cancel-", delete=False) as handle:
            handle.write(os.urandom(1024 * 1024))
            source = Path(handle.name)
        self._files.append(source)
        message_id = await alice.send_file(alice_contact["id"], source)
        await bob_events.wait(
            lambda event: event.get("type") == "file_offer_received",
            timeout=30,
        )
        await alice.cancel_transfer(message_id)
        closed = await bob_events.wait(
            lambda event: event.get("type") == "file_offer_closed",
            timeout=10,
        )
        self.assertEqual(closed.get("reason"), "sender_canceled")
        canceled = await alice_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "canceled",
            timeout=20,
        )
        self.assertIn("不会保留", canceled["message"].get("error", ""))
        self.assertEqual(bob.vault.list_messages(bob_contact["id"]), [])

    async def test_interrupted_transfer_pauses_and_resumes_after_restart(self):
        alice, alice_events, alice_contact, bob, bob_events, bob_contact = await self._new_pair()
        payload = os.urandom(16 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(prefix="v6link-resume-", delete=False) as handle:
            handle.write(payload)
            source = Path(handle.name)
        self._files.append(source)

        original_chunk = EncryptedSession.send_file_chunk

        async def slow_chunk(session, offset, data):
            await asyncio.sleep(0.08)
            return await original_chunk(session, offset, data)

        with patch.object(EncryptedSession, "send_file_chunk", slow_chunk):
            message_id = await alice.send_file(alice_contact["id"], source)
            offer = await bob_events.wait(
                lambda event: event.get("type") == "file_offer_received",
                timeout=30,
            )
            await bob.respond_file_offer(offer["offer"]["file_id"], True)
            incoming = await bob_events.wait(
                lambda event: event.get("type") == "message_new"
                and event["message"].get("kind") == "file",
                timeout=30,
            )
            incoming_id = incoming["message"]["id"]
            await bob_events.wait(
                lambda event: event.get("type") == "transfer_progress"
                and int(event.get("transferred", 0) or 0) > 0,
                timeout=30,
            )
            await alice.stop()

        paused = await bob_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == incoming_id
            and event["message"].get("status") == "paused",
            timeout=30,
        )
        self.assertIn("暂停", paused["message"].get("error", ""))
        partials = list((bob.vault.data_dir / "attachments").glob("*.v6f"))
        self.assertEqual(len(partials), 1)
        self.assertGreater(partials[0].stat().st_size, 0)
        sender_paused = alice.vault.get_message(message_id)
        self.assertIsNotNone(sender_paused)
        self.assertEqual(sender_paused["status"], "paused")

        alice_events_2 = EventCollector()
        alice_2 = ChatEngine(
            alice.vault,
            alice_events_2,
            listen_port=0,
            downloads_dir=Path(alice.vault.data_dir) / "downloads",
        )
        await alice_2.start()
        self._engines.append(alice_2)
        await alice_2.retry_message(message_id)

        await bob_events.wait(
            lambda event: event.get("type") == "file_resume_started",
            timeout=30,
        )
        ready = await bob_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == incoming_id
            and event["message"].get("status") == "ready",
            timeout=60,
        )
        await alice_events_2.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "sent",
            timeout=30,
        )
        plaintext = await asyncio.to_thread(
            lambda: b"".join(
                iter_plaintext(bob.vault.data_dir, ready["message"]["file"])
            )
        )
        self.assertEqual(hashlib.sha256(plaintext).digest(), hashlib.sha256(payload).digest())
        self.assertEqual(
            list((bob.vault.data_dir / "attachments").glob("*.v6f")).__len__(),
            1,
        )

    async def _start_slow_transfer_for_cancel(self):
        alice, alice_events, alice_contact, bob, bob_events, bob_contact = await self._new_pair()
        payload = os.urandom(16 * 1024 * 1024)
        with tempfile.NamedTemporaryFile(prefix="v6link-cancel-active-", delete=False) as handle:
            handle.write(payload)
            source = Path(handle.name)
        self._files.append(source)
        original_chunk = EncryptedSession.send_file_chunk

        async def slow_chunk(session, offset, data):
            await asyncio.sleep(0.08)
            return await original_chunk(session, offset, data)

        patcher = patch.object(EncryptedSession, "send_file_chunk", slow_chunk)
        patcher.start()
        try:
            message_id = await alice.send_file(alice_contact["id"], source)
            offer = await bob_events.wait(
                lambda event: event.get("type") == "file_offer_received",
                timeout=30,
            )
            await bob.respond_file_offer(offer["offer"]["file_id"], True)
            incoming = await bob_events.wait(
                lambda event: event.get("type") == "message_new"
                and event["message"].get("kind") == "file",
                timeout=30,
            )
            incoming_id = incoming["message"]["id"]
            await bob_events.wait(
                lambda event: event.get("type") == "transfer_progress"
                and int(event.get("transferred", 0) or 0) > 0,
                timeout=30,
            )
        except Exception:
            patcher.stop()
            raise
        return (
            alice,
            alice_events,
            bob,
            bob_events,
            bob_contact,
            message_id,
            incoming_id,
            patcher,
        )

    async def test_receiver_cancel_deletes_partial_and_cancels_sender(self):
        (
            alice,
            alice_events,
            bob,
            bob_events,
            bob_contact,
            message_id,
            incoming_id,
            patcher,
        ) = await self._start_slow_transfer_for_cancel()
        try:
            await bob.cancel_transfer(incoming_id)
        finally:
            patcher.stop()
        receiver_canceled = await bob_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == incoming_id
            and event["message"].get("status") == "canceled",
            timeout=20,
        )
        self.assertIn("不会保留", receiver_canceled["message"].get("error", ""))
        sender_canceled = await alice_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "canceled",
            timeout=20,
        )
        self.assertIn("不会保留", sender_canceled["message"].get("error", ""))
        self.assertEqual(list((bob.vault.data_dir / "attachments").glob("*.v6f")), [])

    async def test_sender_cancel_while_receiving_deletes_receiver_partial(self):
        (
            alice,
            alice_events,
            bob,
            bob_events,
            bob_contact,
            message_id,
            incoming_id,
            patcher,
        ) = await self._start_slow_transfer_for_cancel()
        try:
            await alice.cancel_transfer(message_id)
        finally:
            patcher.stop()
        sender_canceled = await alice_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "canceled",
            timeout=20,
        )
        self.assertIn("不会保留", sender_canceled["message"].get("error", ""))
        receiver_canceled = await bob_events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == incoming_id
            and event["message"].get("status") == "canceled",
            timeout=20,
        )
        self.assertIn("不会保留", receiver_canceled["message"].get("error", ""))
        self.assertEqual(list((bob.vault.data_dir / "attachments").glob("*.v6f")), [])

    async def test_incoming_ip_change_and_hello_addresses_are_learned(self):
        local, _events = await self._new_engine("Local")
        peer_id = "b" * 64
        contact = await local.add_contact(
            "Remote",
            peer_id=peer_id,
            addresses=[{"host": "2001:db8::1", "port": 4455}],
        )
        updated, accepted, _reason, request = local._match_incoming_peer(
            peer_id,
            "2001:db8::2",
            4455,
            "Remote",
            ["2001:db8::3", "::1", "fe80::1"],
        )
        self.assertTrue(accepted)
        self.assertIsNone(request)
        self.assertIsNotNone(updated)
        endpoints = {(entry["host"], entry["port"]) for entry in updated["addresses"]}
        self.assertIn(("2001:db8::2", 4455), endpoints)
        self.assertIn(("2001:db8::3", 4455), endpoints)
        self.assertNotIn(("::1", 4455), endpoints)
        self.assertNotIn(("fe80::1", 4455), endpoints)
        stored = local.vault.get_contact(contact["id"])
        self.assertIsNotNone(stored)
        stored_endpoints = {(entry["host"], entry["port"]) for entry in stored["addresses"]}
        self.assertIn(("2001:db8::2", 4455), stored_endpoints)
        self.assertIn(("2001:db8::3", 4455), stored_endpoints)
        self.assertEqual(
            _valid_announced_hosts(["::1", "fe80::1", "2001:db8::4", "2001:db8::4"]),
            ["2001:db8::4"],
        )

    async def test_download_directory_setting_is_persisted(self):
        engine, _events = await self._new_engine("Downloads")
        target = Path(engine.vault.data_dir) / "custom-downloads"
        await engine.apply_settings(
            engine.vault.profile()["display_name"],
            engine.port,
            4,
            target,
        )
        self.assertEqual(engine._downloads_dir.resolve(), target.resolve())
        self.assertTrue(target.is_dir())
        self.assertEqual(
            Path(engine.vault.settings()["downloads_dir"]).resolve(),
            target.resolve(),
        )
        probe = ChatEngine(engine.vault, listen_port=0)
        self.assertEqual(probe._downloads_dir.resolve(), target.resolve())

    async def test_loopback_self_connection_is_not_reported_as_identity_mismatch(self):
        solo, events = await self._new_engine("Solo")
        contact = await solo.add_contact(
            "Myself",
            peer_id="f" * 64,
            addresses=[{"host": "::1", "port": solo.port}],
        )
        message_id = await solo.send_text(contact["id"], "self test")
        failed = await events.wait(
            lambda event: event.get("type") == "message_updated"
            and event["message"].get("id") == message_id
            and event["message"].get("status") == "failed",
            timeout=20,
        )
        await asyncio.sleep(0.2)
        self.assertFalse(any(event.get("type") == "security_alert" for event in events.events))
        self.assertIn("指向本机", failed["message"].get("error", ""))
        self.assertEqual(solo.vault.list_pending_requests(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)


